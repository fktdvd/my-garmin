#!/usr/bin/env bash
# Registers a cron job that runs `mygarmin sync` daily at 01:00 and 13:00 for the
# current user (Linux / macOS). If the machine is off at that time the run is simply
# skipped (no catch-up) - run `uv run mygarmin sync` manually instead.
# With --with-report a second job syncs and sends the AI reports every morning
# (`mygarmin report auto`: daily + missed weekly / monthly), see README.
#
# Usage:   ./scripts/install-cron.sh                # install / update (sync only)
#          ./scripts/install-cron.sh --with-report  # sync + AI reports
#          ./scripts/install-cron.sh --remove       # remove all my-garmin jobs
#          SCHEDULE="0 6 * * *" REPORT_SCHEDULE="0 8 * * *" ./scripts/install-cron.sh --with-report
set -euo pipefail

MARKER="# my-garmin-sync"
REPORT_MARKER="# my-garmin-report"
SCHEDULE="${SCHEDULE:-0 1,13 * * *}"
REPORT_SCHEDULE="${REPORT_SCHEDULE:-30 7 * * *}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

current="$(crontab -l 2>/dev/null | grep -vF -e "$MARKER" -e "$REPORT_MARKER" || true)"

if [[ "${1:-}" == "--remove" ]]; then
    printf '%s\n' "$current" | sed '/^$/d' | crontab -
    echo "Cron jobok eltávolítva."
    exit 0
fi

# cron runs with a minimal PATH, so use the absolute path of uv.
UV="$(command -v uv || true)"
if [[ -z "$UV" ]]; then
    echo "Nem található a uv. Telepítés: https://docs.astral.sh/uv/" >&2
    exit 1
fi
if [[ ! -d "$REPO/data/.garminconnect" && -z "${MYGARMIN_DATA_DIR:-}" ]]; then
    echo "Figyelem: nincs mentett token. Futtasd előbb: uv run mygarmin login" >&2
fi

env_prefix=""
if [[ -n "${MYGARMIN_DATA_DIR:-}" ]]; then
    env_prefix="MYGARMIN_DATA_DIR=$(printf '%q' "$MYGARMIN_DATA_DIR") "
fi
# Output goes to data/logs/mygarmin.log via the app itself; discard cron mail.
cd_uv="cd $(printf '%q' "$REPO") && ${env_prefix}$(printf '%q' "$UV") run --frozen mygarmin"
lines=("$SCHEDULE $cd_uv sync >/dev/null 2>&1 $MARKER")

if [[ "${1:-}" == "--with-report" ]]; then
    if [[ ! -f "$REPO/.env" ]]; then
        echo "Figyelem: nincs $REPO/.env (API-kulcs, SMTP). Lásd .env.example" >&2
    fi
    # Sync first so last night's sleep is in; `report` ingests before generating.
    lines+=("$REPORT_SCHEDULE $cd_uv sync --no-ingest >/dev/null 2>&1; $cd_uv report auto >/dev/null 2>&1 $REPORT_MARKER")
fi

{ printf '%s\n' "$current" | sed '/^$/d'; printf '%s\n' "${lines[@]}"; } | crontab -
echo "Cron job(ok) telepítve:"
crontab -l | grep -F -e "$MARKER" -e "$REPORT_MARKER"