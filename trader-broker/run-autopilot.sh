#!/bin/zsh
# The autopilot, always on (DEMO ONLY): it answers /status, /pause and /resume at any hour and runs
# each trading day. launchd starts it at login and restarts it if it stops; see trader-broker/PHONE.md.
# Like run-guard.sh it reads only these export lines from T212_ENV_FILE (default ~/.zshrc):
# T212_API_KEY, T212_API_SECRET, T212_ENV, TELEGRAM_BOT_TOKEN and (optional) HEALTHCHECK_URL. Nothing else
# in that file is run.
# With T212_ENV=live it refuses to start: there is no live path in the autopilot.
set -eu
cd "${0:A:h}/.."
envfile="${T212_ENV_FILE:-$HOME/.zshrc}"
if [[ -z "${T212_API_KEY:-}" || -z "${T212_API_SECRET:-}" ]]; then
  eval "$(grep -E '^[[:space:]]*export[[:space:]]+T212_(API_KEY|API_SECRET|ENV)=' "$envfile")"
fi
if [[ -z "${TELEGRAM_BOT_TOKEN:-}" ]]; then
  eval "$(grep -E '^[[:space:]]*export[[:space:]]+TELEGRAM_BOT_TOKEN=' "$envfile" || true)"
fi
# optional: the outside "is this Mac alive?" check (trader-broker/heartbeat.py). Handed over under another
# name so that only this launchd autopilot pings it, never a shell or a test run.
hc="$(grep -E '^[[:space:]]*export[[:space:]]+HEALTHCHECK_URL=' "$envfile" | tail -1 || true)"
if [[ -n "$hc" ]]; then eval "$hc"; export AUTOPILOT_HEARTBEAT_URL="${HEALTHCHECK_URL:-}"; unset HEALTHCHECK_URL; fi
unset hc
# optional: the desktop wallpaper's status (trader-broker/wallpaper.py). Only WALLPAPER_URL and
# WALLPAPER_WRITE_KEY are read from trader/.env (git-ignored, owner only), never run as shell, and handed over under
# other names so that only this launchd autopilot sends, never a shell or a test run.
if [[ -f trader/.env ]]; then
  wpu="$(grep -E '^[[:space:]]*(export[[:space:]]+)?WALLPAPER_URL=' trader/.env | tail -1 | cut -d= -f2- | tr -d "\"' \r" || true)"
  wpk="$(grep -E '^[[:space:]]*(export[[:space:]]+)?WALLPAPER_WRITE_KEY=' trader/.env | tail -1 | cut -d= -f2- | tr -d "\"' \r" || true)"
  if [[ -n "$wpu" && -n "$wpk" ]]; then export AUTOPILOT_WALLPAPER_URL="$wpu" AUTOPILOT_WALLPAPER_KEY="$wpk"; fi
  unset wpu wpk
fi
if [[ "${T212_ENV:-demo}" != "demo" ]]; then
  echo "autopilot: T212_ENV is ${T212_ENV}; the autopilot runs on demo only. Not starting." >&2
  exit 2
fi
unset T212_ENV
mkdir -p trader/logs
echo "---- $(date -u +%Y-%m-%dT%H:%M:%SZ) autopilot start (demo)"
if [[ $# -eq 0 ]]; then set -- serve; fi        # always on: launchd keeps it running (KeepAlive)
# The evening Kronos opinions and stop shadows (trader-broker/shadow.py) start only from here, never from a test.
export AUTOPILOT_SHADOW=1
exec /usr/bin/python3 trader-broker/autopilot.py "$@"
