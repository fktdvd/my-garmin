#!/usr/bin/env bash
# Registers a cron job that runs `mygarmin sync` daily at 01:00 and 13:00 for the
# current user (Linux / macOS). If the machine is off at that time the run is simply
# skipped (no catch-up) - run `uv run mygarmin sync` manually instead.
#
# Usage:   ./scripts/install-cron.sh              # install / update
#          ./scripts/install-cron.sh --remove     # remove
#          SCHEDULE="0 6 * * *" ./scripts/install-cron.sh   # custom schedule
set -euo pipefail

MARKER="# my-garmin-sync"
SCHEDULE="${SCHEDULE:-0 1,13 * * *}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

current="$(crontab -l 2>/dev/null | grep -vF "$MARKER" || true)"

if [[ "${1:-}" == "--remove" ]]; then
    printf '%s\n' "$current" | sed '/^$/d' | crontab -
    echo "Cron job eltávolítva."
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
line="$SCHEDULE cd $(printf '%q' "$REPO") && ${env_prefix}$(printf '%q' "$UV") run --frozen mygarmin sync >/dev/null 2>&1 $MARKER"

{ printf '%s\n' "$current" | sed '/^$/d'; echo "$line"; } | crontab -
echo "Cron job telepítve:"
crontab -l | grep -F "$MARKER"
