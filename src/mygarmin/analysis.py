"""Load one activity from the raw store into pandas structures (issue #7).

Uses Garmin's own processed time series (get_activity_details) as the main
source because it is already in real units; the original FIT file is exposed
separately for anything Garmin doesn't provide there.
"""

from __future__ import annotations

import io
import sqlite3
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from mygarmin import raw_store
from mygarmin.db import ACTIVITY_METRICS as METRICS


@dataclass
class Activity:
    activity_id: int
    folder: Path
    summary: dict[str, Any]
    series: pd.DataFrame
    laps: pd.DataFrame
    hr_zones: pd.DataFrame
    weather: dict[str, Any] = field(default_factory=dict)
    # get_activity.json summaryDTO: training effect, load, Body Battery change, ...
    details_summary: dict[str, Any] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.summary.get("activityName") or str(self.activity_id)

    @property
    def type(self) -> str:
        return (self.summary.get("activityType") or {}).get("typeKey", "?")

    @property
    def has_gps(self) -> bool:
        return {"lat", "lon"} <= set(self.series.columns) and self.series["lat"].notna().any()

    def fit_messages(self) -> dict[str, list[dict[str, Any]]]:
        """All decodable FIT messages grouped by type (lazy: FIT parsing is slow)."""
        from fitparse import FitFile

        with zipfile.ZipFile(self.folder / raw_store.ORIGINAL_FILE) as z:
            name = next(n for n in z.namelist() if n.lower().endswith(".fit"))
            fit = FitFile(io.BytesIO(z.read(name)))
        out: dict[str, list[dict[str, Any]]] = {}
        for msg in fit.get_messages():
            if not msg.name.startswith("unknown"):
                out.setdefault(msg.name, []).append(
                    {f.name: f.value for f in msg if not f.name.startswith("unknown")}
                )
        return out


def _read(folder: Path, name: str, default: Any) -> Any:
    p = folder / f"{name}.json"
    return raw_store.read_json(p) if p.is_file() else default


def parse_series(details: dict[str, Any]) -> pd.DataFrame:
    desc = details.get("metricDescriptors") or []
    rows = [m.get("metrics") or [] for m in details.get("activityDetailMetrics") or []]
    if not desc or not rows:
        return pd.DataFrame()
    raw = pd.DataFrame(rows).reindex(columns=range(max(d["metricsIndex"] for d in desc) + 1))
    raw = raw.rename(columns={d["metricsIndex"]: d["key"] for d in desc})
    if "directTimestamp" not in raw:
        return pd.DataFrame()
    df = pd.DataFrame({"time": pd.to_datetime(raw["directTimestamp"], unit="ms", utc=True)})
    for key, (col, factor) in METRICS.items():
        if key in raw:
            df[col] = pd.to_numeric(raw[key], errors="coerce") * factor
    df = df.dropna(subset=["time"]).sort_values("time").reset_index(drop=True)
    df["elapsed_min"] = (df["time"] - df["time"].iloc[0]).dt.total_seconds() / 60
    return df


def parse_laps(splits: dict[str, Any]) -> pd.DataFrame:
    laps = splits.get("lapDTOs") or []
    if not laps:
        return pd.DataFrame()
    df = pd.DataFrame(laps)
    out = pd.DataFrame({"lap": df.get("lapIndex", pd.Series(range(1, len(df) + 1)))})
    out["start"] = pd.to_datetime(df.get("startTimeGMT"), utc=True, errors="coerce")
    out["duration_s"] = df.get("duration")
    out["distance_m"] = df.get("distance")
    out["avg_hr"] = df.get("averageHR")
    out["max_hr"] = df.get("maxHR")
    out["calories"] = df.get("calories")
    out["intensity"] = df.get("intensityType")
    return out


def parse_hr_zones(zones: Any) -> pd.DataFrame:
    if not isinstance(zones, list) or not zones:
        return pd.DataFrame()
    df = pd.DataFrame(zones).sort_values("zoneNumber")
    return pd.DataFrame({
        "zone": df["zoneNumber"],
        "from_bpm": df["zoneLowBoundary"],
        "minutes": df["secsInZone"].fillna(0) / 60,
    }).reset_index(drop=True)


def load_activity(folder: Path) -> Activity:
    folder = Path(folder)
    summary = _read(folder, "summary", {})
    return Activity(
        activity_id=int(summary.get("activityId") or folder.name),
        folder=folder,
        summary=summary,
        series=parse_series(_read(folder, "get_activity_details", {})),
        laps=parse_laps(_read(folder, "get_activity_splits", {})),
        hr_zones=parse_hr_zones(_read(folder, "get_activity_hr_in_timezones", [])),
        weather=_read(folder, "get_activity_weather", {}) or {},
        details_summary=(_read(folder, "get_activity", {}) or {}).get("summaryDTO") or {},
    )


def lap_stats(activity: Activity) -> pd.DataFrame:
    """Laps with HR at lap start, peak and end.

    `hr_drop` = in-lap peak - lap end. The measured peak of a hard effort often
    lands in the following rest lap (HR kinetics + wrist optical sensor lag, which
    is worse with tensed forearms, e.g. climbing), so measuring from the peak
    (not from the lap start) gives the actual recovery during the rest.
    """
    laps, s = activity.laps.copy(), activity.series
    if laps.empty or s.empty or "hr" not in s:
        return laps
    ends = laps["start"] + pd.to_timedelta(laps["duration_s"], unit="s")
    rows = []
    for start, end in zip(laps["start"], ends):
        hr = s.loc[(s["time"] >= start) & (s["time"] <= end), "hr"].dropna()
        rows.append((hr.iloc[0], hr.max(), hr.iloc[-1]) if len(hr) else (None, None, None))
    stats = pd.DataFrame(rows, columns=["hr_start", "hr_peak", "hr_end"], index=laps.index).apply(pd.to_numeric)
    laps = laps.join(stats)
    laps["hr_drop"] = laps["hr_peak"] - laps["hr_end"]
    laps["start_min"] = (laps["start"] - s["time"].iloc[0]).dt.total_seconds() / 60
    return laps


def list_activities(db_path: Path, limit: int = 30) -> pd.DataFrame:
    with sqlite3.connect(db_path) as conn:
        return pd.read_sql(
            "SELECT activity_id, start_local, name, type, round(duration_s/60, 1) AS perc, "
            "round(distance_m/1000, 2) AS km, avg_hr, raw_dir FROM activities "
            "ORDER BY start_local DESC LIMIT ?",
            conn,
            params=(limit,),
        )


def pick_activity(db_path: Path, activity_id: int | None = None) -> Activity:
    acts = list_activities(db_path, limit=100_000)
    if acts.empty:
        raise LookupError("Nincs aktivitás a DB-ben - futtasd: mygarmin sync")
    row = acts.iloc[0] if activity_id is None else acts[acts["activity_id"] == int(activity_id)].iloc[0]
    return load_activity(Path(row["raw_dir"]))


# --- Napi nézet (03_daily) -------------------------------------------------

TZ = "Europe/Budapest"

# Garmin stressz kategóriák (0-25 pihenés, 26-50 alacsony, 51-75 közepes, 76-100 magas)
STRESS_BINS = [0, 25, 50, 75, 100]
STRESS_LABELS = ["pihenés", "alacsony", "közepes", "magas"]


@dataclass
class Day:
    date: str
    intraday: pd.DataFrame  # time (helyi), metric, value
    summary: dict[str, Any]
    sleeps: pd.DataFrame  # start, end (helyi): a napot érintő alvások
    activities: pd.DataFrame  # start, end (helyi), name, type
    readiness: pd.DataFrame  # a nap összes edzéskészség mérése

    def metric(self, name: str, break_gaps: str | None = None) -> pd.DataFrame:
        """Egy metrika idősora. `break_gaps` (pl. "10min"): ennél nagyobb szünetbe NaN kerül,
        így a vonalas ábra nem köti össze a hiányzó szakaszokat."""
        m = self.intraday[self.intraday["metric"] == name][["time", "value"]].reset_index(drop=True)
        if break_gaps is None or m.empty:
            return m
        gap = m["time"].diff() > pd.Timedelta(break_gaps)
        breaks = pd.DataFrame({"time": m["time"][gap] - pd.Timedelta(seconds=1), "value": float("nan")})
        return pd.concat([m, breaks]).sort_values("time").reset_index(drop=True)

    def stress_minutes(self) -> pd.Series:
        """Percek stressz kategóriánként (egy mérés = a következő mérésig eltelt idő, max. 10 perc)."""
        s = self.metric("stress")
        if s.empty:
            return pd.Series(0.0, index=STRESS_LABELS)
        step = s["time"].diff().shift(-1).dt.total_seconds().div(60).clip(upper=10).fillna(3)
        cat = pd.cut(s["value"], STRESS_BINS, labels=STRESS_LABELS, include_lowest=True)
        return step.groupby(cat, observed=False).sum().reindex(STRESS_LABELS, fill_value=0)


def _ms_to_local(ms: pd.Series) -> pd.Series:
    return pd.to_datetime(ms, unit="ms", utc=True).dt.tz_convert(TZ)


def available_days(db_path: Path) -> list[str]:
    with sqlite3.connect(db_path) as conn:
        return [r[0] for r in conn.execute("SELECT DISTINCT date FROM intraday ORDER BY date")]


def load_day(db_path: Path, day: str | None = None) -> Day:
    """Egy nap napon belüli adatai; alapból az utolsó teljes nap (tegnap, ha van adat)."""
    days = available_days(db_path)
    if not days:
        raise LookupError("Nincs napon belüli adat - futtasd: mygarmin sync")
    if day is None:
        day = days[-2] if len(days) > 1 else days[-1]
    next_day = (pd.Timestamp(day) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    with sqlite3.connect(db_path) as conn:
        intraday = pd.read_sql(
            "SELECT metric, ts_ms, value FROM intraday WHERE date = ? ORDER BY ts_ms", conn, params=(day,))
        summary = pd.read_sql(
            "SELECT d.*, s.total_s AS sleep_s, s.score AS sleep_score, h.last_night_avg AS hrv "
            "FROM daily_summary d LEFT JOIN sleep s USING(date) LEFT JOIN hrv h USING(date) WHERE d.date = ?",
            conn, params=(day,))
        sleeps = pd.read_sql(
            "SELECT date, start_gmt, end_gmt FROM sleep WHERE date IN (?, ?)", conn, params=(day, next_day))
        acts = pd.read_sql(
            "SELECT activity_id, start_local, duration_s, name, type FROM activities "
            "WHERE substr(start_local, 1, 10) = ? ORDER BY start_local", conn, params=(day,))
        readiness = pd.read_sql(
            "SELECT * FROM training_readiness WHERE date = ? ORDER BY timestamp_local", conn, params=(day,))
    intraday["time"] = _ms_to_local(intraday["ts_ms"])
    sleeps = pd.DataFrame({"start": _ms_to_local(sleeps["start_gmt"]), "end": _ms_to_local(sleeps["end_gmt"])})
    start = pd.to_datetime(acts["start_local"]).dt.tz_localize(TZ)
    activities = pd.DataFrame({
        "activity_id": acts["activity_id"], "start": start,
        "end": start + pd.to_timedelta(acts["duration_s"], unit="s"),
        "name": acts["name"], "type": acts["type"],
    })
    return Day(
        date=day,
        intraday=intraday[["time", "metric", "value"]],
        summary=summary.iloc[0].to_dict() if not summary.empty else {},
        sleeps=sleeps,
        activities=activities,
        readiness=readiness,
    )


def daily_trends(db_path: Path) -> pd.DataFrame:
    """Naponként egy sor: nyugalmi pulzus, HRV, alvás, reggeli edzéskészség, terhelés."""
    with sqlite3.connect(db_path) as conn:
        df = pd.read_sql(
            """
            SELECT d.date, d.resting_hr, h.last_night_avg AS hrv, h.weekly_avg AS hrv_weekly,
                   s.score AS sleep_score, round(s.total_s / 3600.0, 2) AS sleep_h,
                   d.avg_stress, d.body_battery_high, d.body_battery_low,
                   (SELECT score FROM training_readiness r WHERE r.date = d.date AND r.is_morning = 1
                    ORDER BY r.timestamp_local LIMIT 1) AS readiness_morning,
                   t.acute_load, t.chronic_load, t.chronic_load_min, t.chronic_load_max, t.acwr,
                   t.status_phrase, t.vo2max
            FROM daily_summary d
            LEFT JOIN hrv h USING(date) LEFT JOIN sleep s USING(date) LEFT JOIN training_status t USING(date)
            ORDER BY d.date
            """,
            conn,
        )
    df["date"] = pd.to_datetime(df["date"])
    return df
