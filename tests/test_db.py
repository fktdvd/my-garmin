from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mygarmin import db, raw_store
from mygarmin.config import Config

DAY = "2026-09-20"
T0 = 1789855200000

HEART_RATES = {
    "heartRateValueDescriptors": [{"index": 0, "key": "timestamp"}, {"index": 1, "key": "heartrate"}],
    "heartRateValues": [[T0, None], [T0 + 120_000, 70], [T0 + 240_000, 66]],
}
STRESS = {
    "stressValueDescriptorsDTOList": [{"index": 0, "key": "timestamp"}, {"index": 1, "key": "stressLevel"}],
    "stressValuesArray": [[T0, -1], [T0 + 180_000, 25], [T0 + 360_000, -2]],
    "bodyBatteryValueDescriptorsDTOList": [
        {"bodyBatteryValueDescriptorIndex": 0, "bodyBatteryValueDescriptorKey": "timestamp"},
        {"bodyBatteryValueDescriptorIndex": 1, "bodyBatteryValueDescriptorKey": "bodyBatteryStatus"},
        {"bodyBatteryValueDescriptorIndex": 2, "bodyBatteryValueDescriptorKey": "bodyBatteryLevel"},
    ],
    "bodyBatteryValuesArray": [[T0, "MEASURED", 17, 3.0], [T0 + 180_000, "MEASURED", 18, 3.0]],
}
READINESS = [
    {"calendarDate": DAY, "timestampLocal": "2026-09-20T19:37:05.0", "score": 75, "level": "HIGH",
     "inputContext": "UPDATE_REALTIME_VARIABLES", "recoveryTime": 949},
    {"calendarDate": DAY, "timestampLocal": "2026-09-20T06:50:18.0", "score": 84, "level": "HIGH",
     "inputContext": "AFTER_WAKEUP_RESET", "hrvFactorPercent": 100},
]
TRAINING_STATUS = {
    "mostRecentVO2Max": {"generic": {"calendarDate": "2026-08-22", "vo2MaxPreciseValue": 45.0, "fitnessAge": 20}},
    "mostRecentTrainingLoadBalance": {"metricsTrainingLoadBalanceDTOMap": {
        "1": {"monthlyLoadAnaerobic": 1.0, "primaryTrainingDevice": False},
        "2": {"monthlyLoadAnaerobic": 207.25, "trainingBalanceFeedbackPhrase": "AEROBIC_LOW_FOCUS",
              "primaryTrainingDevice": True},
    }},
    "mostRecentTrainingStatus": {"latestTrainingStatusData": {"2": {
        "trainingStatus": 5, "trainingStatusFeedbackPhrase": "RECOVERY_1", "primaryTrainingDevice": True,
        "acuteTrainingLoadDTO": {"dailyTrainingLoadAcute": 188, "dailyTrainingLoadChronic": 251,
                                 "dailyAcuteChronicWorkloadRatio": 0.7, "acwrStatus": "LOW"},
    }}},
}
SPLITS = {"lapDTOs": [
    {"lapIndex": 1, "startTimeGMT": "2026-09-20T10:00:00.0", "duration": 60.0, "averageHR": 120,
     "maxHR": 140, "intensityType": "ACTIVE"},
    {"lapIndex": 2, "startTimeGMT": "2026-09-20T10:01:00.0", "duration": 60.0, "averageHR": 110,
     "maxHR": 145, "intensityType": "REST"},
]}
DETAILS = {
    "metricDescriptors": [
        {"metricsIndex": 0, "key": "directTimestamp"},
        {"metricsIndex": 1, "key": "directHeartRate"},
        {"metricsIndex": 2, "key": "directSpeed"},
    ],
    "activityDetailMetrics": [
        {"metrics": [T0 + 1000, 101.0, 2.0]},
        {"metrics": [T0, 100.0, None]},
        {"metrics": [None, 99.0, 1.0]},
    ],
}


def test_parse_intraday_skips_invalid_and_uses_descriptors():
    rows = db.parse_intraday(DAY, STRESS, db.INTRADAY_SOURCES["get_stress_data"])
    assert [(r["metric"], r["value"]) for r in rows] == [
        ("stress", 25), ("body_battery", 17), ("body_battery", 18)]
    hr = db.parse_intraday(DAY, HEART_RATES, db.INTRADAY_SOURCES["get_heart_rates"])
    assert [r["value"] for r in hr] == [70, 66]


def test_parse_training_readiness_marks_morning():
    rows = db.parse_training_readiness(DAY, READINESS)
    assert [(r["score"], r["is_morning"]) for r in rows] == [(75, 0), (84, 1)]


def test_parse_training_status_prefers_primary_device():
    row = db.parse_training_status(DAY, TRAINING_STATUS)
    assert row["monthly_load_anaerobic"] == 207.25
    assert row["acute_load"] == 188 and row["acwr"] == 0.7
    assert row["vo2max"] == 45.0 and row["status_phrase"] == "RECOVERY_1"
    assert db.parse_training_status(DAY, {}) is None


def test_parse_activity_samples_sorted_and_converted():
    rows = db.parse_activity_samples(7, DETAILS)
    assert [r["ts_ms"] for r in rows] == [T0, T0 + 1000]
    assert rows[1]["speed_kmh"] == pytest.approx(7.2)
    assert rows[0]["speed_kmh"] is None


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    c = Config(tmp_path / "data")
    for endpoint, data in [("get_heart_rates", HEART_RATES), ("get_stress_data", STRESS),
                           ("get_training_readiness", READINESS), ("get_training_status", TRAINING_STATUS)]:
        raw_store.write_json(raw_store.daily_path(c.raw_dir, endpoint, DAY), data)
    folder = raw_store.activity_dir(c.raw_dir, 7)
    raw_store.write_json(folder / "summary.json", {"activityId": 7, "activityName": "x"})
    raw_store.write_json(folder / "get_activity_splits.json", SPLITS)
    raw_store.write_json(folder / "get_activity_details.json", DETAILS)
    return c


def test_ingest_new_tables_and_is_idempotent(cfg: Config):
    first = db.ingest(cfg)
    second = db.ingest(cfg)
    assert first == second
    assert first["intraday"] == 5
    assert first["training_readiness"] == 2
    assert first["training_status"] == 1
    assert first["activity_laps"] == 2
    assert first["activity_samples"] == 2
    with sqlite3.connect(cfg.db_path) as conn:
        counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in db.TABLES}
    assert counts["intraday"] == 5 and counts["activity_samples"] == 2 and counts["training_readiness"] == 2


def test_ingest_replaces_day_when_raw_changes(cfg: Config):
    db.ingest(cfg)
    raw_store.write_json(raw_store.daily_path(cfg.raw_dir, "get_heart_rates", DAY),
                         {**HEART_RATES, "heartRateValues": [[T0, 80]]})
    db.ingest(cfg)
    with sqlite3.connect(cfg.db_path) as conn:
        hr = conn.execute("SELECT value FROM intraday WHERE metric = 'heart_rate'").fetchall()
    assert hr == [(80,)]
