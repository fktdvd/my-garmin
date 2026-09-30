from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from mygarmin import analysis, raw_store
from mygarmin.analysis import lap_stats, load_activity

T0 = 1_790_000_000_000  # ms


def _details(hr: list[float], lat: list[float] | None = None) -> dict:
    desc = [
        {"metricsIndex": 0, "key": "directTimestamp"},
        {"metricsIndex": 1, "key": "directHeartRate"},
        {"metricsIndex": 2, "key": "directSpeed"},
    ]
    if lat:
        desc += [{"metricsIndex": 3, "key": "directLatitude"}, {"metricsIndex": 4, "key": "directLongitude"}]
    rows = []
    for i, h in enumerate(hr):
        m = [T0 + i * 10_000, h, 2.5]
        if lat:
            m += [lat[i], 19.0]
        rows.append({"metrics": m})
    return {"metricDescriptors": desc, "activityDetailMetrics": rows}


def _lap(i: int, start_s: int, dur_s: int, intensity: str) -> dict:
    from datetime import datetime, timezone

    start = datetime.fromtimestamp(T0 / 1000 + start_s, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.0")
    return {"lapIndex": i, "startTimeGMT": start, "duration": dur_s, "distance": 0, "averageHR": 100,
            "maxHR": 120, "intensityType": intensity}


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    d = tmp_path / "123"
    # 60 s active (HR climbs), then 60 s rest: HR peaks 20 s into the rest (lag), then drops
    hr = [100, 105, 110, 115, 120, 125, 130, 140, 145, 135, 120, 110, 100]
    raw_store.write_json(d / "summary.json", {"activityId": 123, "activityName": "Spray", "activityType": {"typeKey": "other"}})
    raw_store.write_json(d / "get_activity_details.json", _details(hr))
    raw_store.write_json(d / "get_activity_splits.json", {"lapDTOs": [_lap(1, 0, 60, "ACTIVE"), _lap(2, 60, 60, "RECOVERY")]})
    raw_store.write_json(d / "get_activity_hr_in_timezones.json", [
        {"zoneNumber": 2, "secsInZone": 60, "zoneLowBoundary": 109},
        {"zoneNumber": 1, "secsInZone": 30, "zoneLowBoundary": 91},
    ])
    raw_store.write_json(d / "get_activity.json", {"summaryDTO": {"trainingEffect": 2.1}})
    return d


def test_load_activity_parses_series_and_units(folder):
    a = load_activity(folder)
    assert (a.activity_id, a.name, a.type) == (123, "Spray", "other")
    assert list(a.series["hr"])[:3] == [100, 105, 110]
    assert a.series["speed_kmh"].iloc[0] == pytest.approx(9.0)  # 2.5 m/s
    assert a.series["elapsed_min"].iloc[-1] == pytest.approx(2.0)
    assert not a.has_gps
    assert list(a.hr_zones["zone"]) == [1, 2]
    assert a.hr_zones["minutes"].sum() == pytest.approx(1.5)
    assert a.details_summary["trainingEffect"] == 2.1


def test_lap_stats_measures_recovery_from_peak(folder):
    laps = lap_stats(load_activity(folder)).set_index("intensity")
    rest = laps.loc["RECOVERY"]
    assert rest["hr_peak"] == 145
    assert rest["hr_end"] == 100
    assert rest["hr_drop"] == 45
    assert rest["start_min"] == pytest.approx(1.0)


def test_gps_detected(tmp_path):
    d = tmp_path / "9"
    raw_store.write_json(d / "summary.json", {"activityId": 9})
    raw_store.write_json(d / "get_activity_details.json", _details([100, 110], lat=[47.9, 47.91]))
    assert load_activity(d).has_gps


def test_missing_files_give_empty_frames(tmp_path):
    d = tmp_path / "5"
    raw_store.write_json(d / "summary.json", {"activityId": 5})
    a = load_activity(d)
    assert a.series.empty and a.laps.empty and a.hr_zones.empty
    assert lap_stats(a).empty


def _day_db(tmp_path: Path) -> Path:
    from mygarmin import db as dbmod
    from mygarmin.config import Config

    cfg = Config(tmp_path / "d")
    conn = dbmod.connect(cfg)
    t0 = int(pd.Timestamp("2026-09-20", tz=analysis.TZ).timestamp() * 1000)
    rows = [("stress", t0 + i * 180_000, "2026-09-20", v) for i, v in enumerate([10, 30, 60, 90])]
    rows += [("heart_rate", t0, "2026-09-20", 60), ("heart_rate", t0 + 120_000, "2026-09-20", 62),
             ("heart_rate", t0 + 3_600_000, "2026-09-20", 70), ("heart_rate", t0 + 86_400_000, "2026-09-21", 55)]
    conn.executemany("INSERT INTO intraday VALUES (?, ?, ?, ?)", rows)
    conn.execute("INSERT INTO daily_summary (date, resting_hr) VALUES ('2026-09-20', 48)")
    conn.execute("INSERT INTO sleep (date, start_gmt, end_gmt, total_s) VALUES ('2026-09-20', ?, ?, 21600)",
                 (t0 - 3_600_000, t0 + 18_000_000))
    conn.execute("INSERT INTO training_readiness (date, timestamp_local, is_morning, score) "
                 "VALUES ('2026-09-20', '2026-09-20T06:00:00.0', 1, 80)")
    conn.commit()
    conn.close()
    return cfg.db_path


def test_load_day_defaults_to_last_full_day(tmp_path: Path):
    day = analysis.load_day(_day_db(tmp_path))
    assert day.date == "2026-09-20"
    assert day.summary["resting_hr"] == 48
    assert day.metric("heart_rate")["time"].iloc[0].hour == 0  # helyi idő
    assert len(day.sleeps) == 1 and day.sleeps["end"].iloc[0].hour == 5
    assert day.readiness["score"].tolist() == [80]


def test_day_stress_minutes_and_gap_breaks(tmp_path: Path):
    day = analysis.load_day(_day_db(tmp_path), "2026-09-20")
    assert day.stress_minutes().tolist() == [3.0, 3.0, 3.0, 3.0]
    hr = day.metric("heart_rate", break_gaps="10min")
    assert len(hr) == 4 and hr["value"].isna().sum() == 1


def test_daily_trends_joins_tables(tmp_path: Path):
    tr = analysis.daily_trends(_day_db(tmp_path))
    assert tr.loc[0, "resting_hr"] == 48 and tr.loc[0, "readiness_morning"] == 80