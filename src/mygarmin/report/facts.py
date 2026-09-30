"""Deterministic numbers for the reports (stdlib only: runs on sync-only installs).

Date convention: sleep / HRV / morning readiness on date D belong to the night that
ended on the morning of D; everything else (hydration, steps, activities, stress) to day D.
"""

from __future__ import annotations

import sqlite3
import statistics
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

READINESS_FACTORS = {
    "sleep_pct": "alvás (előző éj)",
    "sleep_history_pct": "alvás előzmény",
    "recovery_time_pct": "regenerációs idő",
    "acwr_pct": "terhelés arány (ACWR)",
    "stress_history_pct": "stressz előzmény",
    "hrv_pct": "HRV",
}
WEEKDAYS = ["hétfő", "kedd", "szerda", "csütörtök", "péntek", "szombat", "vasárnap"]


@dataclass(frozen=True)
class Period:
    kind: str  # daily | weekly | monthly
    start: date
    end: date  # inclusive
    label: str

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1


def period_for(kind: str, today: date) -> Period:
    """daily: yesterday (+ last night); weekly: last full Mon-Sun week; monthly: last full month."""
    if kind == "daily":
        y = today - timedelta(days=1)
        return Period("daily", y, y, today.isoformat())
    if kind == "weekly":
        end = today - timedelta(days=today.weekday() + 1)
        start = end - timedelta(days=6)
        iso = start.isocalendar()
        return Period("weekly", start, end, f"{iso.year}-W{iso.week:02d}")
    if kind == "monthly":
        end = today.replace(day=1) - timedelta(days=1)
        return Period("monthly", end.replace(day=1), end, end.strftime("%Y-%m"))
    raise ValueError(kind)


def previous(p: Period) -> Period:
    if p.kind == "monthly":
        end = p.start - timedelta(days=1)
        return Period("monthly", end.replace(day=1), end, end.strftime("%Y-%m"))
    return period_for(p.kind, p.start)


# --- helpers ---------------------------------------------------------------------


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    cur = conn.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _mean(values: list[Any], ndigits: int = 1) -> float | None:
    v = [x for x in values if isinstance(x, (int, float))]
    return round(statistics.fmean(v), ndigits) if v else None


def _sd(values: list[Any], ndigits: int = 0) -> float | None:
    v = [x for x in values if isinstance(x, (int, float))]
    return round(statistics.stdev(v), ndigits) if len(v) >= 2 else None


def _clock_min(ts: str | None) -> int | None:
    """Minutes on a noon-to-noon axis, so 23:30 and 00:30 average to ~00:00."""
    if not ts:
        return None
    t = datetime.fromisoformat(ts)
    m = t.hour * 60 + t.minute
    return m + 24 * 60 if m < 12 * 60 else m


def _clock_text(minutes: float | None) -> str | None:
    if minutes is None:
        return None
    m = int(round(minutes)) % (24 * 60)
    return f"{m // 60:02d}:{m % 60:02d}"


def _h(seconds: Any) -> float | None:
    return round(seconds / 3600, 2) if isinstance(seconds, (int, float)) else None


def _dates(start: date, end: date) -> list[str]:
    return [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]


def _spearman(pairs: list[tuple[Any, Any]]) -> dict[str, Any] | None:
    xy = [(x, y) for x, y in pairs if isinstance(x, (int, float)) and isinstance(y, (int, float))]
    if len(xy) < 8:
        return None
    xs, ys = zip(*xy)
    try:
        rho = statistics.correlation(xs, ys, method="ranked")
    except statistics.StatisticsError:
        return None
    return {"rho": round(rho, 2), "n": len(xy)}


# --- building blocks -------------------------------------------------------------


def sleep_summary(conn: sqlite3.Connection, start: date, end: date, goal_h: float) -> dict[str, Any]:
    rows = _rows(conn, "SELECT * FROM sleep WHERE date BETWEEN ? AND ? ORDER BY date",
                 (start.isoformat(), end.isoformat()))
    hours = [_h(r["total_s"]) for r in rows]
    bed = [_clock_min(r["bedtime_local"]) for r in rows]
    wake = [_clock_min(r["wake_local"]) for r in rows]
    worst = min(rows, key=lambda r: r["score"] or 999, default=None)
    return {
        "nights": len(rows),
        "avg_h": _mean(hours, 2),
        "min_h": min((h for h in hours if h), default=None),
        "avg_score": _mean([r["score"] for r in rows]),
        "avg_deep_pct": _mean([100 * r["deep_s"] / r["total_s"] for r in rows if r["deep_s"] is not None]),
        "avg_rem_pct": _mean([100 * r["rem_s"] / r["total_s"] for r in rows if r["rem_s"] is not None]),
        "avg_awake_min": _mean([r["awake_s"] / 60 for r in rows if r["awake_s"] is not None]),
        "bedtime_avg": _clock_text(_mean(bed, 0)),
        "bedtime_sd_min": _sd(bed),
        "wake_avg": _clock_text(_mean(wake, 0)),
        "wake_sd_min": _sd(wake),
        "nights_below_goal": sum(1 for h in hours if h is not None and h < goal_h),
        "worst_night": {"date": worst["date"], "score": worst["score"], "h": _h(worst["total_s"])} if worst else None,
    }


def hydration_summary(conn: sqlite3.Connection, start: date, end: date, goal_ml: float) -> dict[str, Any]:
    rows = _rows(conn, "SELECT * FROM hydration WHERE date BETWEEN ? AND ? ORDER BY date",
                 (start.isoformat(), end.isoformat()))
    intake = [r["intake_ml"] or 0 for r in rows]
    return {
        "days": len(rows),
        "logged_days": sum(1 for v in intake if v > 0),
        "avg_ml": _mean(intake, 0),
        "avg_ml_logged_days": _mean([v for v in intake if v > 0], 0),
        "goal_ml": goal_ml,
        "days_reached_goal": sum(1 for v in intake if v >= goal_ml),
        "days_reached_garmin_goal": sum(1 for r in rows if (r["intake_ml"] or 0) >= (r["goal_ml"] or 1e9)),
        "avg_garmin_goal_ml": _mean([r["goal_ml"] for r in rows], 0),
        "avg_sweat_loss_ml": _mean([r["sweat_loss_ml"] for r in rows], 0),
    }


def recovery_summary(conn: sqlite3.Connection, start: date, end: date) -> dict[str, Any]:
    p = (start.isoformat(), end.isoformat())
    d = _rows(conn, "SELECT * FROM daily_summary WHERE date BETWEEN ? AND ?", p)
    hrv = _rows(conn, "SELECT * FROM hrv WHERE date BETWEEN ? AND ? ORDER BY date", p)
    ready = _rows(conn, "SELECT score FROM training_readiness r WHERE is_morning = 1 AND date BETWEEN ? AND ? "
                        "AND timestamp_local = (SELECT MIN(timestamp_local) FROM training_readiness "
                        "WHERE date = r.date AND is_morning = 1)", p)
    return {
        "avg_resting_hr": _mean([r["resting_hr"] for r in d]),
        "avg_hrv": _mean([r["last_night_avg"] for r in hrv]),
        "hrv_weekly_avg_last": hrv[-1]["weekly_avg"] if hrv else None,
        "hrv_status_last": hrv[-1]["status"] if hrv else None,
        "avg_readiness_morning": _mean([r["score"] for r in ready]),
        "avg_stress": _mean([r["avg_stress"] for r in d]),
        "avg_body_battery_high": _mean([r["body_battery_high"] for r in d]),
        "avg_body_battery_low": _mean([r["body_battery_low"] for r in d]),
        "avg_steps": _mean([r["steps"] for r in d], 0),
    }


def activities(conn: sqlite3.Connection, start: date, end: date) -> list[dict[str, Any]]:
    rows = _rows(conn, "SELECT * FROM activities WHERE substr(start_local, 1, 10) BETWEEN ? AND ? "
                       "ORDER BY start_local", (start.isoformat(), end.isoformat()))
    return [{
        "date": r["start_local"][:10],
        "time": r["start_local"][11:16],
        "name": r["name"],
        "type": r["type"],
        "min": round((r["duration_s"] or 0) / 60),
        "avg_hr": r["avg_hr"],
        "max_hr": r["max_hr"],
        "aerobic_te": round(r["aerobic_te"], 1) if r["aerobic_te"] is not None else None,
        "anaerobic_te": round(r["anaerobic_te"], 1) if r["anaerobic_te"] is not None else None,
        "load": round(r["training_load"]) if r["training_load"] is not None else None,
        "label": r["te_label"],
        "zone_min": [round((r[f"hr_z{i}_s"] or 0) / 60) for i in range(1, 6)],
    } for r in rows]


def training_summary(acts: list[dict[str, Any]]) -> dict[str, Any]:
    zones = [sum(a["zone_min"][i] for a in acts) for i in range(5)]
    return {
        "sessions": len(acts),
        "total_min": sum(a["min"] for a in acts),
        "zone_min": dict(zip(["z1", "z2", "z3", "z4", "z5"], zones)),
        "aerobic_zone_min": zones[1] + zones[2],
        "high_zone_min": zones[3] + zones[4],
        "aerobic_sessions": sum(1 for a in acts if (a["aerobic_te"] or 0) >= 2.0),
        "total_load": sum(a["load"] or 0 for a in acts),
    }


def training_status(conn: sqlite3.Connection, on: date) -> dict[str, Any] | None:
    rows = _rows(conn, "SELECT * FROM training_status WHERE date <= ? ORDER BY date DESC LIMIT 1", (on.isoformat(),))
    vo2 = _rows(conn, "SELECT vo2max, vo2max_date FROM training_status WHERE date <= ? AND vo2max IS NOT NULL "
                      "ORDER BY date DESC LIMIT 1", (on.isoformat(),))
    if not rows:
        return None
    r = rows[0]
    return {
        "date": r["date"],
        "status": r["status_phrase"],
        "acute_load": r["acute_load"],
        "chronic_load": r["chronic_load"],
        "chronic_optimal_range": [r["chronic_load_min"], r["chronic_load_max"]],
        "acwr": r["acwr"],
        "acwr_status": r["acwr_status"],
        "monthly_load_balance": {
            "aerobic_low": _round(r["monthly_load_aerobic_low"]),
            "aerobic_low_target": [r["aerobic_low_target_min"], r["aerobic_low_target_max"]],
            "aerobic_high": _round(r["monthly_load_aerobic_high"]),
            "aerobic_high_target": [r["aerobic_high_target_min"], r["aerobic_high_target_max"]],
            "anaerobic": _round(r["monthly_load_anaerobic"]),
            "anaerobic_target": [r["anaerobic_target_min"], r["anaerobic_target_max"]],
            "feedback": r["load_balance_phrase"],
        },
        "vo2max": vo2[0]["vo2max"] if vo2 else None,
        "vo2max_date": vo2[0]["vo2max_date"] if vo2 else None,
    }


def _round(v: Any) -> Any:
    return round(v) if isinstance(v, float) else v


def per_day(conn: sqlite3.Connection, start: date, end: date) -> list[dict[str, Any]]:
    out = []
    for d in _dates(start, end):
        row = _rows(conn, """
            SELECT
              (SELECT round(total_s / 3600.0, 2) FROM sleep WHERE date = :d) AS sleep_h,
              (SELECT score FROM sleep WHERE date = :d) AS sleep_score,
              (SELECT substr(bedtime_local, 12, 5) FROM sleep WHERE date = :d) AS bedtime,
              (SELECT last_night_avg FROM hrv WHERE date = :d) AS hrv,
              (SELECT score FROM training_readiness WHERE date = :d AND is_morning = 1
                 ORDER BY timestamp_local LIMIT 1) AS readiness,
              (SELECT resting_hr FROM daily_summary WHERE date = :d) AS rhr,
              (SELECT avg_stress FROM daily_summary WHERE date = :d) AS stress,
              (SELECT steps FROM daily_summary WHERE date = :d) AS steps,
              (SELECT intake_ml FROM hydration WHERE date = :d) AS water_ml,
              (SELECT round(sum(training_load)) FROM activities WHERE substr(start_local, 1, 10) = :d) AS load,
              (SELECT round(sum(coalesce(hr_z2_s, 0) + coalesce(hr_z3_s, 0)) / 60)
                 FROM activities WHERE substr(start_local, 1, 10) = :d) AS aerobic_min,
              (SELECT max(substr(start_local, 12, 5)) FROM activities WHERE substr(start_local, 1, 10) = :d)
                 AS last_activity_time
            """, {"d": d})[0]
        out.append({"date": d, **row})
    return out


def correlations(days: list[dict[str, Any]]) -> dict[str, Any]:
    """Day D metric vs. the following night / morning (D+1)."""
    nxt = days[1:]
    pairs = {
        "water_ml_vs_next_sleep_score": [(a["water_ml"], b["sleep_score"]) for a, b in zip(days, nxt)],
        "stress_vs_next_sleep_score": [(a["stress"], b["sleep_score"]) for a, b in zip(days, nxt)],
        "load_vs_next_hrv": [(a["load"] or 0, b["hrv"]) for a, b in zip(days, nxt)],
        "sleep_h_vs_readiness_same_morning": [(d["sleep_h"], d["readiness"]) for d in days],
        "bedtime_vs_sleep_score": [(_clock_min(f"2000-01-01T{d['bedtime']}") if d["bedtime"] else None,
                                    d["sleep_score"]) for d in days],
    }
    return {k: r for k, v in pairs.items() if (r := _spearman(v))}


def by_weekday(days: list[dict[str, Any]]) -> dict[str, Any]:
    out = {}
    for i, name in enumerate(WEEKDAYS):
        ds = [d for d in days if date.fromisoformat(d["date"]).weekday() == i]
        out[name] = {"sleep_h": _mean([d["sleep_h"] for d in ds], 2),
                     "water_ml_logged": _mean([d["water_ml"] for d in ds if d["water_ml"]], 0),
                     "stress": _mean([d["stress"] for d in ds])}
    return out


def summarize(conn: sqlite3.Connection, start: date, end: date, goals: dict[str, Any]) -> dict[str, Any]:
    acts = activities(conn, start, end)
    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "sleep": sleep_summary(conn, start, end, goals["sleep_hours"]),
        "hydration": hydration_summary(conn, start, end, goals["hydration_ml"]),
        "recovery": recovery_summary(conn, start, end),
        "training": training_summary(acts),
        "activities": acts,
    }


def _weakest_factors(r: dict[str, Any]) -> list[dict[str, Any]]:
    f = [{"factor": name, "pct": r[k]} for k, name in READINESS_FACTORS.items() if r.get(k) is not None]
    return sorted(f, key=lambda x: x["pct"])[:2]


# --- per report ------------------------------------------------------------------


def daily_facts(conn: sqlite3.Connection, p: Period, goals: dict[str, Any]) -> dict[str, Any]:
    yesterday, today = p.end, p.end + timedelta(days=1)
    t = today.isoformat()
    night = _rows(conn, "SELECT * FROM sleep WHERE date = ?", (t,))
    hrv = _rows(conn, "SELECT last_night_avg, weekly_avg, status FROM hrv WHERE date = ?", (t,))
    ready = _rows(conn, "SELECT * FROM training_readiness WHERE date = ? AND is_morning = 1 "
                        "ORDER BY timestamp_local LIMIT 1", (t,))
    day = _rows(conn, "SELECT * FROM daily_summary WHERE date = ?", (yesterday.isoformat(),))
    water = _rows(conn, "SELECT intake_ml, goal_ml, sweat_loss_ml FROM hydration WHERE date = ?",
                  (yesterday.isoformat(),))
    n = night[0] if night else None
    return {
        "report": "daily",
        "date": t,
        "goals": goals,
        "last_night": None if not n else {
            "sleep_h": _h(n["total_s"]), "score": n["score"],
            "bedtime": (n["bedtime_local"] or "")[11:16], "wake": (n["wake_local"] or "")[11:16],
            "deep_pct": round(100 * n["deep_s"] / n["total_s"]) if n["deep_s"] is not None else None,
            "rem_pct": round(100 * n["rem_s"] / n["total_s"]) if n["rem_s"] is not None else None,
            "awake_min": round(n["awake_s"] / 60) if n["awake_s"] is not None else None,
            "avg_respiration": n["avg_respiration"], "avg_spo2": n["avg_spo2"],
            "hrv": hrv[0] if hrv else None,
        },
        "morning_readiness": None if not ready else {
            "score": ready[0]["score"], "level": ready[0]["level"], "feedback": ready[0]["feedback"],
            "recovery_time_h": round(ready[0]["recovery_time_min"] / 60, 1)
            if ready[0]["recovery_time_min"] is not None else None,
            "weakest_factors": _weakest_factors(ready[0]),
        },
        "yesterday": {
            "date": yesterday.isoformat(),
            "steps": day[0]["steps"] if day else None,
            "resting_hr": day[0]["resting_hr"] if day else None,
            "avg_stress": day[0]["avg_stress"] if day else None,
            "body_battery_high_low": [day[0]["body_battery_high"], day[0]["body_battery_low"]] if day else None,
            "hydration": water[0] if water else None,
            "activities": activities(conn, yesterday, yesterday),
        },
        "baseline_prev_7_days": summarize(conn, yesterday - timedelta(days=6), yesterday, goals) | {"activities": None},
        "training_status": training_status(conn, today),
        "last_7_days": per_day(conn, yesterday - timedelta(days=6), yesterday),
    }


def weekly_facts(conn: sqlite3.Connection, p: Period, goals: dict[str, Any]) -> dict[str, Any]:
    prev = previous(p)
    this = summarize(conn, p.start, p.end, goals)
    return {
        "report": "weekly",
        "week": p.label,
        "goals": goals,
        "this_week": this,
        "previous_week": summarize(conn, prev.start, prev.end, goals) | {"activities": None},
        "per_day": per_day(conn, p.start, p.end),
        "training_status_end_of_week": training_status(conn, p.end),
    }


def monthly_facts(conn: sqlite3.Connection, p: Period, goals: dict[str, Any]) -> dict[str, Any]:
    prev = previous(p)
    days = per_day(conn, p.start, p.end)
    status_start = training_status(conn, p.start)
    status_end = training_status(conn, p.end)
    this = summarize(conn, p.start, p.end, goals)
    weeks = p.days / 7
    return {
        "report": "monthly",
        "month": p.label,
        "goals": goals,
        "this_month": this,
        "previous_month": summarize(conn, prev.start, prev.end, goals) | {"activities": None},
        "weekly_averages": {
            "aerobic_sessions": round(this["training"]["aerobic_sessions"] / weeks, 1),
            "aerobic_zone_min": round(this["training"]["aerobic_zone_min"] / weeks),
        },
        "per_day": days,
        "by_weekday": by_weekday(days),
        "correlations_spearman": correlations(days),
        "training_status_start": status_start,
        "training_status_end": status_end,
    }


def build_facts(db_path: Path, p: Period, goals: dict[str, Any]) -> dict[str, Any]:
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        return {"daily": daily_facts, "weekly": weekly_facts, "monthly": monthly_facts}[p.kind](conn, p, goals)
    finally:
        conn.close()


def has_data(facts: dict[str, Any]) -> bool:
    if facts["report"] == "daily":
        return bool(facts["last_night"] or facts["yesterday"]["steps"] is not None)
    key = "this_week" if facts["report"] == "weekly" else "this_month"
    s = facts[key]
    return bool(s["sleep"]["nights"] or s["hydration"]["days"] or s["training"]["sessions"])
