from __future__ import annotations

from pathlib import Path

import pytest

from mygarmin import raw_store
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
