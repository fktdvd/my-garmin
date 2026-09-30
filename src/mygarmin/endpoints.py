"""Registry of Garmin Connect endpoints to export.

Three kinds:
- DAILY:    called once per calendar day  -> raw/daily/<name>/<YYYY-MM-DD>.json
- PROFILE:  called once per sync run      -> raw/profile/<name>/<YYYY-MM-DD>.json
- ACTIVITY: called once per activity      -> raw/activities/<id>/<name>.json

Set `enabled=False` to skip an endpoint without deleting it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class Endpoint:
    name: str
    call: Callable[..., Any]
    enabled: bool = True


def _by_date(method: str) -> Callable[[Any, str], Any]:
    return lambda client, day: getattr(client, method)(day)


def _by_date_range(method: str) -> Callable[[Any, str], Any]:
    return lambda client, day: getattr(client, method)(day, day)


def _no_args(method: str) -> Callable[[Any], Any]:
    return lambda client: getattr(client, method)()


def _by_id(method: str) -> Callable[[Any, str], Any]:
    return lambda client, activity_id: getattr(client, method)(activity_id)


def _profile_number(client: Any) -> Any:
    return client.get_device_last_used()["userProfileNumber"]


DAILY: list[Endpoint] = [
    *(
        Endpoint(m, _by_date(m))
        for m in (
            "get_user_summary",
            "get_stats_and_body",
            "get_heart_rates",
            "get_rhr_day",
            "get_sleep_data",
            "get_stress_data",
            "get_all_day_stress",
            "get_body_battery_events",
            "get_respiration_data",
            "get_spo2_data",
            "get_hrv_data",
            "get_steps_data",
            "get_floors",
            "get_intensity_minutes_data",
            "get_hydration_data",
            "get_training_readiness",
            "get_morning_training_readiness",
            "get_training_status",
            "get_daily_training_status",
            "get_training_four_week_load_balance",
            "get_max_metrics",
            "get_fitnessage_data",
            "get_all_day_events",
            "get_daily_weigh_ins",
            "get_lifestyle_logging_data",
            "get_nutrition_daily_food_log",
            "get_nutrition_daily_meals",
        )
    ),
    Endpoint("get_body_battery", _by_date_range("get_body_battery")),
    Endpoint("get_blood_pressure", _by_date_range("get_blood_pressure")),
    Endpoint("get_body_composition", _by_date_range("get_body_composition")),
    Endpoint("get_endurance_score", _by_date_range("get_endurance_score")),
    Endpoint("get_hill_score", _by_date_range("get_hill_score")),
    Endpoint("get_menstrual_data_for_date", _by_date("get_menstrual_data_for_date"), enabled=False),
]

PROFILE: list[Endpoint] = [
    *(
        Endpoint(m, _no_args(m))
        for m in (
            "get_user_profile",
            "get_userprofile_settings",
            "get_devices",
            "get_device_last_used",
            "get_primary_training_device",
            "get_personal_record",
            "get_earned_badges",
            "get_heart_rate_zones",
            "get_power_zones",
            "get_race_predictions",
            "get_lactate_threshold",
            "get_goals",
            "get_workouts",
            "get_training_plans",
            "get_activity_types",
        )
    ),
    Endpoint("get_gear", lambda c: c.get_gear(_profile_number(c))),
    Endpoint("get_gear_defaults", lambda c: c.get_gear_defaults(_profile_number(c))),
]

ACTIVITY: list[Endpoint] = [
    Endpoint(m, _by_id(m))
    for m in (
        "get_activity",
        "get_activity_details",
        "get_activity_splits",
        "get_activity_typed_splits",
        "get_activity_split_summaries",
        "get_activity_hr_in_timezones",
        "get_activity_power_in_timezones",
        "get_activity_exercise_sets",
        "get_activity_weather",
        "get_activity_gear",
    )
]


def enabled(endpoints: list[Endpoint]) -> list[Endpoint]:
    return [e for e in endpoints if e.enabled]
