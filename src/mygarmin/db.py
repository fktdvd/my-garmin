"""Raw store -> SQLite (issue #6).

The DB is fully derived from data/raw and can be rebuilt any time with
`mygarmin ingest --rebuild`. Everything not extracted here stays queryable
in the raw JSON until it's needed. No pandas here: sync-only installs
(without the `analysis` extra) must be able to ingest.

Timestamps: `*_ms` columns are Unix epoch milliseconds (UTC), `*_gmt` text
columns are ISO UTC, `*_local` text columns are the watch's local time.
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

-- Napon belüli idősorok hosszú formában: metric = heart_rate | stress | body_battery | respiration.
-- Érvénytelen (negatív) Garmin-értékek (pl. stressz -1/-2 = nincs mérés / mozgás) kimaradnak.
CREATE TABLE IF NOT EXISTS intraday (
    metric      TEXT NOT NULL,
    ts_ms       INTEGER NOT NULL,
    date        TEXT NOT NULL,
    value       REAL NOT NULL,
    PRIMARY KEY (metric, ts_ms)
);
CREATE INDEX IF NOT EXISTS intraday_date ON intraday (date, metric);

-- Edzéskészség: naponta több mérés (ébredés után, aktivitások után, ...).
CREATE TABLE IF NOT EXISTS training_readiness (
    date                TEXT NOT NULL,
    timestamp_local     TEXT NOT NULL,
    is_morning          INTEGER,
    score               INTEGER,
    level               TEXT,
    feedback            TEXT,
    recovery_time_min   INTEGER,
    acute_load          INTEGER,
    hrv_weekly_avg      INTEGER,
    sleep_score         INTEGER,
    sleep_pct           INTEGER,
    sleep_history_pct   INTEGER,
    recovery_time_pct   INTEGER,
    acwr_pct            INTEGER,
    stress_history_pct  INTEGER,
    hrv_pct             INTEGER,
    PRIMARY KEY (date, timestamp_local)
);

CREATE TABLE IF NOT EXISTS training_status (
    date                TEXT PRIMARY KEY,
    status_code         INTEGER,
    status_phrase       TEXT,
    acute_load          REAL,
    chronic_load        REAL,
    chronic_load_min    REAL,
    chronic_load_max    REAL,
    acwr                REAL,
    acwr_status         TEXT,
    vo2max              REAL,
    vo2max_date         TEXT,
    fitness_age         INTEGER,
    monthly_load_aerobic_low  REAL,
    monthly_load_aerobic_high REAL,
    monthly_load_anaerobic    REAL,
    load_balance_phrase TEXT
);

CREATE TABLE IF NOT EXISTS activity_laps (
    activity_id     INTEGER NOT NULL,
    lap             INTEGER NOT NULL,
    start_gmt       TEXT,
    duration_s      REAL,
    distance_m      REAL,
    avg_hr          REAL,
    max_hr          REAL,
    calories        REAL,
    intensity       TEXT,
    PRIMARY KEY (activity_id, lap)
);

-- Aktivitás idősor a Garmin feldolgozott adataiból (get_activity_details), valós mértékegységben.
-- A nyers FIT rekordok: mygarmin.analysis.Activity.fit_messages().
CREATE TABLE IF NOT EXISTS activity_samples (
    activity_id         INTEGER NOT NULL,
    ts_ms               INTEGER NOT NULL,
    hr                  REAL,
    speed_kmh           REAL,
    elevation_m         REAL,
    vertical_speed_ms   REAL,
    cadence_spm         REAL,
    body_battery        REAL,
    kcal_per_min        REAL,
    lat                 REAL,
    lon                 REAL,
    grit                REAL,
    flow                REAL,
    distance_m          REAL,
    PRIMARY KEY (activity_id, ts_ms)
);
"""

TABLES = (
    "daily_summary", "sleep", "hrv", "activities", "intraday",
    "training_readiness", "training_status", "activity_laps", "activity_samples",
)

# Garmin activity metric key -> (column name, conversion factor). Shared with analysis.py.
ACTIVITY_METRICS: dict[str, tuple[str, float]] = {
    "directHeartRate": ("hr", 1),
    "directSpeed": ("speed_kmh", 3.6),
    "directElevation": ("elevation_m", 1),
    "directVerticalSpeed": ("vertical_speed_ms", 1),
    "directDoubleCadence": ("cadence_spm", 1),
    "directBodyBattery": ("body_battery", 1),
    "directCaloriesBurnRate": ("kcal_per_min", 1),
    "directLatitude": ("lat", 1),
    "directLongitude": ("lon", 1),
    "directGrit": ("grit", 1),
    "directFlow": ("flow", 1),
    "sumDistance": ("distance_m", 1),
}

# endpoint -> [(metric, values key, descriptors key, value descriptor name, fallback index)]
INTRADAY_SOURCES: dict[str, list[tuple[str, str, str, str, int]]] = {
    "get_heart_rates": [
        ("heart_rate", "heartRateValues", "heartRateValueDescriptors", "heartrate", 1),
    ],
    "get_stress_data": [
        ("stress", "stressValuesArray", "stressValueDescriptorsDTOList", "stressLevel", 1),
        ("body_battery", "bodyBatteryValuesArray", "bodyBatteryValueDescriptorsDTOList",
         "bodyBatteryLevel", 2),
    ],
    "get_respiration_data": [
        ("respiration", "respirationValuesArray", "respirationValueDescriptorsDTOList",
         "respiration", 1),
    ],
}


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


def _descriptor_index(descriptors: Any, name: str, default: int) -> int:
    """Garmin descriptors come as {index, key} or {...DescriptorIndex, ...DescriptorKey}."""
    for d in descriptors if isinstance(descriptors, list) else []:
        if not isinstance(d, dict):
            continue
        key = next((v for k, v in d.items() if k.lower().endswith("key")), None)
        idx = next((v for k, v in d.items() if k.lower().endswith("index")), None)
        if key == name and isinstance(idx, int):
            return idx
    return default


def parse_intraday(day: str, d: dict[str, Any], sources: list[tuple[str, str, str, str, int]]) -> list[dict[str, Any]]:
    rows = []
    for metric, values_key, desc_key, value_name, fallback in sources:
        descriptors = d.get(desc_key)
        ts_i = _descriptor_index(descriptors, "timestamp", 0)
        val_i = _descriptor_index(descriptors, value_name, fallback)
        for point in d.get(values_key) or []:
            if not isinstance(point, list) or len(point) <= max(ts_i, val_i):
                continue
            ts, value = point[ts_i], point[val_i]
            if isinstance(ts, (int, float)) and isinstance(value, (int, float)) and value >= 0:
                rows.append({"metric": metric, "ts_ms": int(ts), "date": day, "value": value})
    return rows


def parse_training_readiness(day: str, d: Any) -> list[dict[str, Any]]:
    rows = []
    for r in d if isinstance(d, list) else [d]:
        if not isinstance(r, dict) or not r.get("timestampLocal"):
            continue
        rows.append({
            "date": r.get("calendarDate") or day,
            "timestamp_local": r["timestampLocal"],
            "is_morning": int(r.get("inputContext") == "AFTER_WAKEUP_RESET"),
            "score": r.get("score"),
            "level": r.get("level"),
            "feedback": r.get("feedbackShort"),
            "recovery_time_min": r.get("recoveryTime"),
            "acute_load": r.get("acuteLoad"),
            "hrv_weekly_avg": r.get("hrvWeeklyAverage"),
            "sleep_score": r.get("sleepScore"),
            "sleep_pct": r.get("sleepScoreFactorPercent"),
            "sleep_history_pct": r.get("sleepHistoryFactorPercent"),
            "recovery_time_pct": r.get("recoveryTimeFactorPercent"),
            "acwr_pct": r.get("acwrFactorPercent"),
            "stress_history_pct": r.get("stressHistoryFactorPercent"),
            "hrv_pct": r.get("hrvFactorPercent"),
        })
    return rows


def _primary_device_entry(by_device: Any) -> dict[str, Any]:
    """Garmin keys these by device id; prefer the primary training device."""
    entries = [v for v in (by_device or {}).values() if isinstance(v, dict)] if isinstance(by_device, dict) else []
    return next((e for e in entries if e.get("primaryTrainingDevice")), entries[0] if entries else {})


def parse_training_status(day: str, d: dict[str, Any]) -> dict[str, Any] | None:
    status = _primary_device_entry(_dig(d, "mostRecentTrainingStatus", "latestTrainingStatusData"))
    balance = _primary_device_entry(
        _dig(d, "mostRecentTrainingLoadBalance", "metricsTrainingLoadBalanceDTOMap"))
    vo2 = _dig(d, "mostRecentVO2Max", "generic") or {}
    if not (status or balance or vo2):
        return None
    acute = status.get("acuteTrainingLoadDTO") or {}
    return {
        "date": day,
        "status_code": status.get("trainingStatus"),
        "status_phrase": status.get("trainingStatusFeedbackPhrase"),
        "acute_load": acute.get("dailyTrainingLoadAcute"),
        "chronic_load": acute.get("dailyTrainingLoadChronic"),
        "chronic_load_min": acute.get("minTrainingLoadChronic"),
        "chronic_load_max": acute.get("maxTrainingLoadChronic"),
        "acwr": acute.get("dailyAcuteChronicWorkloadRatio"),
        "acwr_status": acute.get("acwrStatus"),
        "vo2max": vo2.get("vo2MaxPreciseValue") or vo2.get("vo2MaxValue"),
        "vo2max_date": vo2.get("calendarDate"),
        "fitness_age": vo2.get("fitnessAge"),
        "monthly_load_aerobic_low": balance.get("monthlyLoadAerobicLow"),
        "monthly_load_aerobic_high": balance.get("monthlyLoadAerobicHigh"),
        "monthly_load_anaerobic": balance.get("monthlyLoadAnaerobic"),
        "load_balance_phrase": balance.get("trainingBalanceFeedbackPhrase"),
    }


def parse_activity_laps(activity_id: int, splits: Any) -> list[dict[str, Any]]:
    laps = splits.get("lapDTOs") if isinstance(splits, dict) else None
    rows = []
    for i, lap in enumerate(laps or [], start=1):
        rows.append({
            "activity_id": activity_id,
            "lap": lap.get("lapIndex") or i,
            "start_gmt": lap.get("startTimeGMT"),
            "duration_s": lap.get("duration"),
            "distance_m": lap.get("distance"),
            "avg_hr": lap.get("averageHR"),
            "max_hr": lap.get("maxHR"),
            "calories": lap.get("calories"),
            "intensity": lap.get("intensityType"),
        })
    return rows


def parse_activity_samples(activity_id: int, details: Any) -> list[dict[str, Any]]:
    if not isinstance(details, dict):
        return []
    index = {m.get("key"): m.get("metricsIndex") for m in details.get("metricDescriptors") or []}
    ts_i = index.get("directTimestamp")
    if ts_i is None:
        return []
    cols = [(col, index[key], factor) for key, (col, factor) in ACTIVITY_METRICS.items() if key in index]
    rows: dict[int, dict[str, Any]] = {}
    for m in details.get("activityDetailMetrics") or []:
        values = m.get("metrics") or []
        ts = values[ts_i] if ts_i < len(values) else None
        if not isinstance(ts, (int, float)):
            continue
        row: dict[str, Any] = {"activity_id": activity_id, "ts_ms": int(ts)}
        for col, i, factor in cols:
            v = values[i] if i < len(values) else None
            row[col] = v * factor if isinstance(v, (int, float)) else None
        rows[int(ts)] = row
    return [rows[k] for k in sorted(rows)]


# endpoint -> (table, parser(day, data) -> row | rows | None)
DAILY_PARSERS = {
    "get_user_summary": ("daily_summary", parse_user_summary),
    "get_sleep_data": ("sleep", parse_sleep),
    "get_hrv_data": ("hrv", parse_hrv),
    "get_training_readiness": ("training_readiness", parse_training_readiness),
    "get_training_status": ("training_status", parse_training_status),
}


def _upsert_many(conn: sqlite3.Connection, table: str, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    cols = list(rows[0])
    sql = f"INSERT OR REPLACE INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})"
    conn.executemany(sql, [[r.get(c) for c in cols] for r in rows])


def _load(f: Path) -> Any:
    try:
        return raw_store.read_json(f)
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Hibás raw fájl %s: %s", f, e)
        return None


def _as_rows(result: Any) -> list[dict[str, Any]]:
    if not result:
        return []
    return result if isinstance(result, list) else [result]


def ingest(cfg: Config, rebuild: bool = False) -> dict[str, int]:
    counts = {t: 0 for t in TABLES}
    conn = connect(cfg)
    daily = cfg.raw_dir / "daily"
    try:
        with conn:
            if rebuild:
                for t in TABLES:
                    conn.execute(f"DELETE FROM {t}")
            for endpoint, (table, parser) in DAILY_PARSERS.items():
                for f in sorted((daily / endpoint).glob("*.json")):
                    data = _load(f)
                    if data is None or (table != "training_readiness" and not isinstance(data, dict)):
                        continue
                    rows = _as_rows(parser(f.stem, data))
                    if table == "training_readiness":
                        conn.execute("DELETE FROM training_readiness WHERE date = ?", (f.stem,))
                    _upsert_many(conn, table, rows)
                    counts[table] += len(rows)
            for endpoint, sources in INTRADAY_SOURCES.items():
                metrics = [s[0] for s in sources]
                for f in sorted((daily / endpoint).glob("*.json")):
                    data = _load(f)
                    if not isinstance(data, dict):
                        continue
                    conn.execute(
                        f"DELETE FROM intraday WHERE date = ? AND metric IN ({', '.join('?' for _ in metrics)})",
                        (f.stem, *metrics),
                    )
                    rows = parse_intraday(f.stem, data, sources)
                    _upsert_many(conn, "intraday", rows)
                    counts["intraday"] += len(rows)
            for folder in sorted((cfg.raw_dir / "activities").glob("*")):
                summary = folder / "summary.json"
                if not summary.is_file():
                    continue
                s = _load(summary)
                if not isinstance(s, dict):
                    continue
                row = parse_activity(folder, s)
                _upsert(conn, "activities", row)
                counts["activities"] += 1
                aid = row["activity_id"]
                for table, name, parser in (
                    ("activity_laps", "get_activity_splits", parse_activity_laps),
                    ("activity_samples", "get_activity_details", parse_activity_samples),
                ):
                    f = folder / f"{name}.json"
                    if not f.is_file():
                        continue
                    conn.execute(f"DELETE FROM {table} WHERE activity_id = ?", (aid,))
                    rows = parser(aid, _load(f))
                    _upsert_many(conn, table, rows)
                    counts[table] += len(rows)
    finally:
        conn.close()
    log.info("Ingest kész: %s", counts)
    return counts
