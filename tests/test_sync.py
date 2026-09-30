from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from garminconnect import GarminConnectNotFoundError, GarminConnectTooManyRequestsError

from mygarmin import db, endpoints, raw_store
from mygarmin.config import Config
from mygarmin.sync import Syncer, compute_range, load_state, run_sync


class FakeClient:
    """Answers every get_* call with a small dict; configurable failures."""

    def __init__(self, fail: dict[str, Exception] | None = None, activities: list[dict] | None = None):
        self.fail = fail or {}
        self.activities = activities or []
        self.calls: list[tuple[str, tuple]] = []

    def __getattr__(self, name: str):
        if not name.startswith(("get_", "download_")):
            raise AttributeError(name)

        def method(*args, **kwargs):
            self.calls.append((name, args))
            if name in self.fail:
                raise self.fail[name]
            if name == "get_activities_by_date":
                return self.activities
            if name == "download_activity":
                return b"PK-fake-zip"
            if name == "get_device_last_used":
                return {"userProfileNumber": 1}
            if name == "get_user_summary":
                return {"totalSteps": 1234, "restingHeartRate": 50}
            if name == "get_sleep_data":
                return {"dailySleepDTO": {"sleepTimeSeconds": 28000, "deepSleepSeconds": 5000,
                                          "sleepScores": {"overall": {"value": 81}}}}
            return {"endpoint": name, "args": list(args)}

        return method


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(tmp_path / "data")


def _sync(client, cfg, today, **kw):
    return run_sync(client, cfg, today=today, syncer=Syncer(client, cfg, pause_s=0), **kw)


def test_compute_range_first_run_backfills():
    assert compute_range({}, date(2026, 9, 30), backfill_days=7) == (date(2026, 9, 24), date(2026, 9, 30))


def test_compute_range_incremental_refetches_last_day():
    state = {"last_synced_date": "2026-09-28"}
    assert compute_range(state, date(2026, 9, 30)) == (date(2026, 9, 28), date(2026, 9, 30))


def test_compute_range_since_overrides_state():
    state = {"last_synced_date": "2026-09-28"}
    assert compute_range(state, date(2026, 9, 30), since=date(2026, 1, 1))[0] == date(2026, 1, 1)


def test_sync_writes_raw_files_and_state(cfg):
    client = FakeClient(activities=[{"activityId": 42, "activityName": "Boulder", "activityType": {"typeKey": "bouldering"}}])
    report = _sync(client, cfg, date(2026, 9, 30), backfill_days=2)

    daily = endpoints.enabled(endpoints.DAILY)
    assert report.failed == []
    for day in ("2026-09-29", "2026-09-30"):
        for e in daily:
            assert raw_store.daily_path(cfg.raw_dir, e.name, day).is_file()
    for e in endpoints.enabled(endpoints.PROFILE):
        assert raw_store.profile_path(cfg.raw_dir, e.name, "2026-09-30").is_file()
    folder = raw_store.activity_dir(cfg.raw_dir, 42)
    assert (folder / "original.zip").read_bytes() == b"PK-fake-zip"
    assert (folder / "get_activity_details.json").is_file()
    assert load_state(cfg.state_path)["last_synced_date"] == "2026-09-30"
    assert not any(n == "get_menstrual_data_for_date" for n, _ in client.calls)


def test_existing_activity_is_not_downloaded_again(cfg):
    acts = [{"activityId": 7}]
    _sync(FakeClient(activities=acts), cfg, date(2026, 9, 30), backfill_days=1)
    client = FakeClient(activities=acts)
    report = _sync(client, cfg, date(2026, 9, 30))
    assert report.activities_skipped == 1
    assert not any(n == "download_activity" for n, _ in client.calls)


def test_not_found_is_empty_not_failure(cfg):
    client = FakeClient(fail={"get_hrv_data": GarminConnectNotFoundError("404")})
    report = _sync(client, cfg, date(2026, 9, 30), backfill_days=1)
    assert report.failed == []
    assert report.empty == 1
    assert not raw_store.daily_path(cfg.raw_dir, "get_hrv_data", "2026-09-30").exists()


def test_failures_are_retried_next_run(cfg):
    _sync(FakeClient(fail={"get_spo2_data": RuntimeError("boom")}), cfg, date(2026, 9, 28), backfill_days=1)
    assert load_state(cfg.state_path)["pending"] == [
        {"kind": "daily", "endpoint": "get_spo2_data", "key": "2026-09-28", "error": "boom"}
    ]
    client = FakeClient()
    _sync(client, cfg, date(2026, 9, 30))  # range 09-28..09-30 covers it again
    assert raw_store.daily_path(cfg.raw_dir, "get_spo2_data", "2026-09-28").is_file()
    assert load_state(cfg.state_path)["pending"] == []


def test_old_pending_outside_range_is_retried(cfg):
    cfg.data_dir.mkdir(parents=True)
    raw_store.write_json(cfg.state_path, {
        "last_synced_date": "2026-09-30",
        "pending": [{"kind": "daily", "endpoint": "get_spo2_data", "key": "2026-09-01", "error": "x"}],
    })
    _sync(FakeClient(), cfg, date(2026, 9, 30))
    assert raw_store.daily_path(cfg.raw_dir, "get_spo2_data", "2026-09-01").is_file()


def test_rate_limit_aborts_without_advancing_state(cfg):
    client = FakeClient(fail={"get_heart_rates": GarminConnectTooManyRequestsError("429")})
    report = _sync(client, cfg, date(2026, 9, 30), backfill_days=3)
    assert report.rate_limited
    assert load_state(cfg.state_path) == {}


def test_ingest_builds_sqlite(cfg):
    _sync(FakeClient(activities=[{"activityId": 42, "activityName": "Boulder",
                                  "activityType": {"typeKey": "bouldering"}, "duration": 3600}]),
          cfg, date(2026, 9, 30), backfill_days=2)
    counts = db.ingest(cfg)
    assert counts["daily_summary"] == 2
    assert counts["sleep"] == 2
    assert counts["activities"] == 1

    conn = db.connect(cfg)
    try:
        assert conn.execute("SELECT steps, resting_hr FROM daily_summary WHERE date='2026-09-30'").fetchone() == (1234, 50)
        assert conn.execute("SELECT score FROM sleep WHERE date='2026-09-30'").fetchone() == (81,)
        assert conn.execute("SELECT type, has_original FROM activities").fetchone() == ("bouldering", 1)
    finally:
        conn.close()
    assert db.ingest(cfg, rebuild=True)["activities"] == 1
