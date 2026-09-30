"""Personal goals and context for the reports: data/profile.toml over built-in defaults."""

from __future__ import annotations

import copy
import tomllib
from pathlib import Path
from typing import Any

from mygarmin.config import Config

DEFAULT_PROFILE: dict[str, Any] = {
    "goals": {
        "sleep_hours": 7.5,
        "bedtime": "23:00",
        "hydration_ml": 2000,
        "aerobic_sessions_per_week": 3,
        "aerobic_minutes_per_week": 150,
    },
    "context": {"text": ""},
    "report": {
        "language": "magyar",
        "max_words": {"daily": 250, "weekly": 450, "monthly": 650},
    },
}


def profile_path(cfg: Config) -> Path:
    return cfg.data_dir / "profile.toml"


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in override.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_profile(cfg: Config) -> dict[str, Any]:
    p = profile_path(cfg)
    if not p.is_file():
        return copy.deepcopy(DEFAULT_PROFILE)
    with p.open("rb") as f:
        return _merge(DEFAULT_PROFILE, tomllib.load(f))
