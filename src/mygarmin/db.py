"""Raw store -> SQLite (first step of issue #4).

The DB is fully derived from data/raw and can be rebuilt any time with
`mygarmin ingest --rebuild`. Only a few core tables are extracted here;
everything else stays queryable in the raw JSON until it's needed.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from mygarmin import raw_store
from mygarmin.config import Config

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_summary (
    date                TEXT PRIMARY KEY,
    steps               INTEGER,
    distance_m          REAL,
    total_kcal          REAL,
    active_kcal         REAL,
    resting_hr          INTEGER,
    min_hr              INTEGER,
    max_hr              INTEGER,
    avg_stress          INTEGER,
    max_stress          INTEGER,
    body_battery_high   INTEGER,
    body_battery_low    INTEGER,
    sleeping_s          INTEGER,
    floors_ascended     REAL,
    intensity_min_moderate INTEGER,
    intensity_min_vigorous INTEGER
);

CREATE TABLE IF NOT EXISTS sleep (
    date            TEXT PRIMARY KEY,
    start_gmt       INTEGER,
    end_gmt         INTEGER,
    total_s         INTEGER,
    deep_s          INTEGER,
    light_s         INTEGER,
    rem_s           INTEGER,
    awake_s         INTEGER,
    score           INTEGER,
    avg_respiration REAL,
    avg_spo2        REAL
);

CREATE TABLE IF NOT EXISTS hrv (
    date            TEXT PRIMARY KEY,
    last_night_avg  REAL,
    weekly_avg      REAL,
    status          TEXT
);

CREATE TABLE IF NOT EXISTS activities (
    activity_id     INTEGER PRIMARY KEY,
    start_local     TEXT,
    name            TEXT,
    type            TEXT,
    duration_s      REAL,
    distance_m      REAL,
    elevation_gain_m REAL,
    avg_hr          REAL,
    max_hr          REAL,
    calories        REAL,
    has_original    INTEGER,
    raw_dir         TEXT
);
"""

TABLES = ("daily_summary", "sleep", "hrv", "activities")


def connect(cfg: Config) -> sqlite3.Connection:
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(cfg.db_path)
    conn.executescript(SCHEMA)
    return conn


def _dig(d: Any, *keys: str) -> Any:
    for k in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def _upsert(conn: sqlite3.Connection, table: str, row: dict[str, Any]) -> None:
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    conn.execute(f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({marks})", list(row.values()))


def parse_user_summary(day: str, d: dict[str, Any]) -> dict[str, Any]:
    return {
        "date": day,
        "steps": d.get("totalSteps"),
        "distance_m": d.get("totalDistanceMeters"),
        "total_kcal": d.get("totalKilocalories"),
        "active_kcal": d.get("activeKilocalories"),
        "resting_hr": d.get("restingHeartRate"),
        "min_hr": d.get("minHeartRate"),
        "max_hr": d.get("maxHeartRate"),
        "avg_stress": d.get("averageStressLevel"),
        "max_stress": d.get("maxStressLevel"),
        "body_battery_high": d.get("bodyBatteryHighestValue"),
        "body_battery_low": d.get("bodyBatteryLowestValue"),
        "sleeping_s": d.get("sleepingSeconds"),
        "floors_ascended": d.get("floorsAscended"),
        "intensity_min_moderate": d.get("moderateIntensityMinutes"),
        "intensity_min_vigorous": d.get("vigorousIntensityMinutes"),
    }


def parse_sleep(day: str, d: dict[str, Any]) -> dict[str, Any] | None:
    s = d.get("dailySleepDTO") or {}
    if not s.get("sleepTimeSeconds"):
        return None
    return {
        "date": day,
        "start_gmt": s.get("sleepStartTimestampGMT"),
        "end_gmt": s.get("sleepEndTimestampGMT"),
        "total_s": s.get("sleepTimeSeconds"),
        "deep_s": s.get("deepSleepSeconds"),
        "light_s": s.get("lightSleepSeconds"),
        "rem_s": s.get("remSleepSeconds"),
        "awake_s": s.get("awakeSleepSeconds"),
        "score": _dig(s, "sleepScores", "overall", "value"),
        "avg_respiration": s.get("averageRespirationValue"),
        "avg_spo2": s.get("averageSpO2Value"),
    }


def parse_hrv(day: str, d: dict[str, Any]) -> dict[str, Any] | None:
    s = d.get("hrvSummary") or {}
    if not s:
        return None
    return {
        "date": day,
        "last_night_avg": s.get("lastNightAvg"),
        "weekly_avg": s.get("weeklyAvg"),
        "status": s.get("status"),
    }


def parse_activity(folder: Path, d: dict[str, Any]) -> dict[str, Any]:
    return {
        "activity_id": d.get("activityId"),
        "start_local": d.get("startTimeLocal"),
        "name": d.get("activityName"),
        "type": _dig(d, "activityType", "typeKey"),
        "duration_s": d.get("duration"),
        "distance_m": d.get("distance"),
        "elevation_gain_m": d.get("elevationGain"),
        "avg_hr": d.get("averageHR"),
        "max_hr": d.get("maxHR"),
        "calories": d.get("calories"),
        "has_original": int((folder / raw_store.ORIGINAL_FILE).is_file()),
        "raw_dir": str(folder),
    }


DAILY_PARSERS = {
    "get_user_summary": ("daily_summary", parse_user_summary),
    "get_sleep_data": ("sleep", parse_sleep),
    "get_hrv_data": ("hrv", parse_hrv),
}


def ingest(cfg: Config, rebuild: bool = False) -> dict[str, int]:
    counts = {t: 0 for t in TABLES}
    conn = connect(cfg)
    try:
        with conn:
            if rebuild:
                for t in TABLES:
                    conn.execute(f"DELETE FROM {t}")
            for endpoint, (table, parser) in DAILY_PARSERS.items():
                folder = cfg.raw_dir / "daily" / endpoint
                for f in sorted(folder.glob("*.json")):
                    try:
                        data = raw_store.read_json(f)
                        row = parser(f.stem, data) if isinstance(data, dict) else None
                    except (json.JSONDecodeError, OSError) as e:
                        log.warning("Hibás raw fájl %s: %s", f, e)
                        continue
                    if row:
                        _upsert(conn, table, row)
                        counts[table] += 1
            for folder in sorted((cfg.raw_dir / "activities").glob("*")):
                summary = folder / "summary.json"
                if not summary.is_file():
                    continue
                _upsert(conn, "activities", parse_activity(folder, raw_store.read_json(summary)))
                counts["activities"] += 1
    finally:
        conn.close()
    log.info("Ingest kész: %s", counts)
    return counts
