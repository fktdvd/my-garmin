from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    data_dir: Path

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "garmin.db"

    @property
    def token_dir(self) -> Path:
        return self.data_dir / ".garminconnect"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def state_path(self) -> Path:
        return self.data_dir / "state.json"


def load_config() -> Config:
    data_dir = os.getenv("MYGARMIN_DATA_DIR") or str(Path.cwd() / "data")
    return Config(Path(data_dir).expanduser().resolve())
