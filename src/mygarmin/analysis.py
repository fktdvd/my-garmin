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

# Garmin metric key -> (column name, conversion factor)
METRICS: dict[str, tuple[str, float]] = {
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
