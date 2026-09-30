"""Login handling.

`mygarmin login` is interactive (email/password/MFA) and stores tokens in
data/.garminconnect. Scheduled runs only use the stored tokens, so the
password never has to be saved on disk.
"""

from __future__ import annotations

import contextlib
import getpass
import logging
import os

from garminconnect import Garmin

from mygarmin.config import Config

log = logging.getLogger(__name__)


class NotLoggedInError(RuntimeError):
    pass


def _has_tokens(cfg: Config) -> bool:
    return cfg.token_dir.is_dir() and any(cfg.token_dir.iterdir())


def interactive_login(cfg: Config) -> Garmin:
    email = os.getenv("GARMIN_EMAIL") or input("Garmin e-mail: ").strip()
    password = os.getenv("GARMIN_PASSWORD") or getpass.getpass("Garmin jelszó: ")
    cfg.token_dir.mkdir(parents=True, exist_ok=True)
    client = Garmin(email, password, prompt_mfa=lambda: input("MFA kód: ").strip())
    client.login(str(cfg.token_dir))
    log.info("Bejelentkezve, tokenek mentve: %s", cfg.token_dir)
    return client


def token_login(cfg: Config) -> Garmin:
    if not _has_tokens(cfg):
        raise NotLoggedInError("Nincs mentett token. Futtasd: mygarmin login")
    # Credentials from env are optional; they only allow automatic re-login.
    client = Garmin(os.getenv("GARMIN_EMAIL"), os.getenv("GARMIN_PASSWORD"))
    client.login(str(cfg.token_dir))
    # Persist refreshed tokens so the next scheduled run can reuse them.
    with contextlib.suppress(Exception):
        client.client.dump(str(cfg.token_dir))
    return client
