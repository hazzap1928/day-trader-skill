#!/bin/zsh
# Start the Trading 212 stop guardian for the day (exits only, never buys):
#   t212.py guard --watch  waits for the 9:30 New York open, checks every 5 minutes
#   (every minute with stop_mode "market"), stops at 16:00.
# For launchd, which does not read your shell profile: if the keys are not already in the
# environment, only the "export T212_API_KEY=", "export T212_API_SECRET=" and
# "export T212_ENV=" lines are read from T212_ENV_FILE (default ~/.zshrc). Nothing else
# in that file is run. With T212_ENV=live (set by go-live.sh) it passes --live; t212.py
# still refuses live unless trader/config.json has "live_enabled": true and "paper": false.
set -eu
cd "${0:A:h}/.."
if [[ -z "${T212_API_KEY:-}" || -z "${T212_API_SECRET:-}" ]]; then
  envfile="${T212_ENV_FILE:-$HOME/.zshrc}"
  eval "$(grep -E '^[[:space:]]*export[[:space:]]+T212_(API_KEY|API_SECRET|ENV)=' "$envfile")"
fi
live=()
if [[ "${T212_ENV:-}" == "live" ]]; then
  live=(--live)
else
  unset T212_ENV
fi
mkdir -p trader/logs
echo "---- $(date -u +%Y-%m-%dT%H:%M:%SZ) guard start (${T212_ENV:-demo})"
exec /usr/bin/python3 trader-broker/t212.py guard --watch "${live[@]}" "$@"
