#!/bin/zsh
# Start the autopilot for the day (DEMO ONLY): tickets every 15 minutes to your phone,
# placed only when you tap Approve. launchd runs this on weekdays; see trader-broker/PHONE.md.
# Like run-guard.sh it reads only these export lines from T212_ENV_FILE (default ~/.zshrc):
# T212_API_KEY, T212_API_SECRET, T212_ENV and TELEGRAM_BOT_TOKEN. Nothing else in that file is run.
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
if [[ "${T212_ENV:-demo}" != "demo" ]]; then
  echo "autopilot: T212_ENV is ${T212_ENV}; the autopilot runs on demo only. Not starting." >&2
  exit 2
fi
unset T212_ENV
mkdir -p trader/logs
echo "---- $(date -u +%Y-%m-%dT%H:%M:%SZ) autopilot start (demo)"
exec /usr/bin/python3 trader-broker/autopilot.py run "$@"
