from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from logging.handlers import RotatingFileHandler

from mygarmin.config import Config, load_config


def _setup_logging(cfg: Config, verbose: bool) -> None:
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = RotatingFileHandler(cfg.log_dir / "mygarmin.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(fmt)
    handlers: list[logging.Handler] = [file_handler]
    # pythonw (scheduled run) has no console.
    if sys.stderr is not None:
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        handlers.append(console)
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, handlers=handlers, force=True)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mygarmin", description="Garmin Connect -> lokális adat")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("login", help="Interaktív bejelentkezés, tokenek mentése")

    s = sub.add_parser("sync", help="Letöltés Garmin Connectből (inkrementális), majd ingest")
    s.add_argument("--since", type=date.fromisoformat, help="Kezdőnap (YYYY-MM-DD), pl. teljes visszatöltéshez")
    s.add_argument("--backfill-days", type=int, default=30, help="Első futáskor ennyi napra visszamenőleg (alap: 30)")
    s.add_argument("--no-ingest", action="store_true", help="Ne töltse be SQLite-ba")

    i = sub.add_parser("ingest", help="Raw fájlok betöltése SQLite-ba")
    i.add_argument("--rebuild", action="store_true", help="Táblák ürítése és teljes újratöltés")
    return p


def _utf8_console() -> None:
    # Windows consoles default to cp1252, which can't print Hungarian text.
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    _utf8_console()
    args = _build_parser().parse_args(argv)
    cfg = load_config()
    _setup_logging(cfg, args.verbose)
    log = logging.getLogger("mygarmin")

    try:
        if args.command == "login":
            from mygarmin.auth import interactive_login

            interactive_login(cfg)
            return 0

        if args.command == "sync":
            from mygarmin.auth import token_login
            from mygarmin.sync import run_sync

            client = token_login(cfg)
            report = run_sync(client, cfg, since=args.since, backfill_days=args.backfill_days)
            if not args.no_ingest:
                from mygarmin.db import ingest

                ingest(cfg)
            return 2 if report.rate_limited else 0

        if args.command == "ingest":
            from mygarmin.db import ingest

            ingest(cfg, rebuild=args.rebuild)
            return 0
    except Exception as e:
        from mygarmin.auth import NotLoggedInError

        if isinstance(e, NotLoggedInError):
            log.error("%s", e)
        else:
            log.exception("Sikertelen futás: %s", args.command)
        return 1
    return 1
