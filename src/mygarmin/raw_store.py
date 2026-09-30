"""Raw (unaltered) Garmin responses on disk. The DB can always be rebuilt from here."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

ORIGINAL_FILE = "original.zip"


def daily_path(raw_dir: Path, endpoint: str, day: str) -> Path:
    return raw_dir / "daily" / endpoint / f"{day}.json"


def profile_path(raw_dir: Path, endpoint: str, day: str) -> Path:
    return raw_dir / "profile" / endpoint / f"{day}.json"


def activity_dir(raw_dir: Path, activity_id: int | str) -> Path:
    return raw_dir / "activities" / str(activity_id)


def is_activity_complete(raw_dir: Path, activity_id: int | str) -> bool:
    return (activity_dir(raw_dir, activity_id) / ORIGINAL_FILE).is_file()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def write_json(path: Path, data: Any) -> None:
    _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8"))


def write_bytes(path: Path, data: bytes) -> None:
    _atomic_write(path, data)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))
