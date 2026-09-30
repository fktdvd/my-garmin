"""Garmin Connect -> raw store (issue #3).

Incremental: each run fetches from the last synced day (re-fetching it, since a
day's data keeps changing until it is over) up to today. Endpoint failures are
logged and retried on the next run; "not found" answers are treated as
"no data for this device/day" and are not retried. A rate-limit error stops
the run without advancing the sync state.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from garminconnect import (
    Garmin,
    GarminConnectNotFoundError,
    GarminConnectTooManyRequestsError,
)

from mygarmin import endpoints as ep
from mygarmin import raw_store
from mygarmin.config import Config

log = logging.getLogger(__name__)

DEFAULT_BACKFILL_DAYS = 30
ACTIVITY_PAGE_DAYS = 90


class RateLimited(RuntimeError):
    pass


@dataclass
class SyncReport:
    start: date
    end: date
    ok: int = 0
    empty: int = 0
    failed: list[dict[str, str]] = field(default_factory=list)
    activities_new: int = 0
    activities_skipped: int = 0
    rate_limited: bool = False

    def summary(self) -> str:
        return (
            f"{self.start}..{self.end}: ok={self.ok} nincs_adat={self.empty} "
            f"hiba={len(self.failed)} új_aktivitás={self.activities_new} "
            f"meglévő_aktivitás={self.activities_skipped}"
            + (" [RATE LIMIT - megszakítva]" if self.rate_limited else "")
        )


def load_state(path: Path) -> dict[str, Any]:
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    raw_store.write_json(path, state)


def compute_range(
    state: dict[str, Any],
    today: date,
    since: date | None = None,
    backfill_days: int = DEFAULT_BACKFILL_DAYS,
) -> tuple[date, date]:
    if since is not None:
        start = since
    elif state.get("last_synced_date"):
        start = date.fromisoformat(state["last_synced_date"])
    else:
        start = today - timedelta(days=backfill_days - 1)
    return min(start, today), today


def _days(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


class Syncer:
    def __init__(
        self,
        client: Garmin,
        cfg: Config,
        pause_s: float = 0.3,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client = client
        self.cfg = cfg
        self.pause_s = pause_s
        self.sleep = sleep

    def _fetch(self, report: SyncReport, kind: str, name: str, key: str, fn: Callable[[], Any]) -> tuple[bool, Any]:
        """Returns (success, data). Raises RateLimited to abort the whole run."""
        try:
            data = fn()
        except GarminConnectTooManyRequestsError as e:
            raise RateLimited(str(e)) from e
        except GarminConnectNotFoundError:
            report.empty += 1
            return True, None
        except Exception as e:  # noqa: BLE001 - one bad endpoint must not stop the export
            log.warning("%s %s %s: %s", kind, name, key, e)
            report.failed.append({"kind": kind, "endpoint": name, "key": key, "error": str(e)[:300]})
            return False, None
        finally:
            if self.pause_s:
                self.sleep(self.pause_s)
        report.ok += 1
        return True, data

    def sync_daily(self, report: SyncReport, name: str, day: str) -> None:
        endpoint = next((e for e in ep.DAILY if e.name == name), None)
        if endpoint is None:
            return
        ok, data = self._fetch(report, "daily", name, day, lambda: endpoint.call(self.client, day))
        if ok and data is not None:
            raw_store.write_json(raw_store.daily_path(self.cfg.raw_dir, name, day), data)

    def sync_profile(self, report: SyncReport) -> None:
        day = report.end.isoformat()
        for e in ep.enabled(ep.PROFILE):
            ok, data = self._fetch(report, "profile", e.name, day, lambda e=e: e.call(self.client))
            if ok and data is not None:
                raw_store.write_json(raw_store.profile_path(self.cfg.raw_dir, e.name, day), data)

    def sync_activity(self, report: SyncReport, summary: dict[str, Any]) -> None:
        activity_id = str(summary["activityId"])
        if raw_store.is_activity_complete(self.cfg.raw_dir, activity_id):
            report.activities_skipped += 1
            return
        folder = raw_store.activity_dir(self.cfg.raw_dir, activity_id)
        raw_store.write_json(folder / "summary.json", summary)
        for e in ep.enabled(ep.ACTIVITY):
            ok, data = self._fetch(report, "activity", e.name, activity_id, lambda e=e: e.call(self.client, activity_id))
            if ok and data is not None:
                raw_store.write_json(folder / f"{e.name}.json", data)
        ok, blob = self._fetch(
            report,
            "activity",
            "download_original",
            activity_id,
            lambda: self.client.download_activity(activity_id, dl_fmt=Garmin.ActivityDownloadFormat.ORIGINAL),
        )
        # original.zip is written last: its presence marks the activity as complete.
        if ok and blob:
            raw_store.write_bytes(folder / raw_store.ORIGINAL_FILE, blob)
            report.activities_new += 1

    def sync_activities(self, report: SyncReport) -> None:
        chunk_start = report.start
        while chunk_start <= report.end:
            chunk_end = min(chunk_start + timedelta(days=ACTIVITY_PAGE_DAYS - 1), report.end)
            ok, items = self._fetch(
                report,
                "activity_list",
                "get_activities_by_date",
                f"{chunk_start}..{chunk_end}",
                lambda: self.client.get_activities_by_date(chunk_start.isoformat(), chunk_end.isoformat()),
            )
            for item in (items or []) if ok else []:
                self.sync_activity(report, item)
            chunk_start = chunk_end + timedelta(days=1)

    def run(self, start: date, end: date, retry: list[dict[str, str]] | None = None) -> SyncReport:
        report = SyncReport(start=start, end=end)
        try:
            for item in retry or []:
                if item.get("kind") == "daily" and not (start.isoformat() <= item["key"] <= end.isoformat()):
                    self.sync_daily(report, item["endpoint"], item["key"])
            self.sync_profile(report)
            daily = ep.enabled(ep.DAILY)
            for d in _days(start, end):
                day = d.isoformat()
                log.info("Nap: %s", day)
                for e in daily:
                    self.sync_daily(report, e.name, day)
            self.sync_activities(report)
        except RateLimited as e:
            log.error("Garmin rate limit, a futás megszakadt: %s", e)
            report.rate_limited = True
        return report


def run_sync(
    client: Garmin,
    cfg: Config,
    today: date | None = None,
    since: date | None = None,
    backfill_days: int = DEFAULT_BACKFILL_DAYS,
    syncer: Syncer | None = None,
) -> SyncReport:
    today = today or date.today()
    state = load_state(cfg.state_path)
    start, end = compute_range(state, today, since, backfill_days)
    log.info("Szinkron: %s .. %s", start, end)
    syncer = syncer or Syncer(client, cfg)
    report = syncer.run(start, end, retry=state.get("pending", []))

    if not report.rate_limited:
        # Only daily items can be retried individually; activities are retried
        # automatically because original.zip is missing until they succeed.
        pending = [f for f in report.failed if f["kind"] == "daily"]
        prev = state.get("last_synced_date")
        state["last_synced_date"] = max(prev, end.isoformat()) if prev else end.isoformat()
        state["pending"] = pending
        state["last_run"] = {"at": datetime.now().isoformat(timespec="seconds"), "summary": report.summary()}
        save_state(cfg.state_path, state)
    log.info(report.summary())
    return report
