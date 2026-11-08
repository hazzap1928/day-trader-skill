#!/bin/zsh
# The morning briefing (read-only): launchd runs this at 07:30 UK time on weekdays.
# briefing.py itself skips US market holidays. Like run-guard.sh it reads only these export
# lines from T212_ENV_FILE (default ~/.zshrc): T212_API_KEY, T212_API_SECRET, TELEGRAM_BOT_TOKEN.
# It always reads the demo account: T212_ENV is not passed on.
set -eu
cd "${0:A:h}/.."
envfile="${T212_ENV_FILE:-$HOME/.zshrc}"
if [[ -z "${T212_API_KEY:-}" || -z "${T212_API_SECRET:-}" ]]; then
  eval "$(grep -E '^[[:space:]]*export[[:space:]]+T212_(API_KEY|API_SECRET)=' "$envfile")"
fi
if [[ -z "${TELEGRAM_BOT_TOKEN:-}" ]]; then
  eval "$(grep -E '^[[:space:]]*export[[:space:]]+TELEGRAM_BOT_TOKEN=' "$envfile" || true)"
fi
unset T212_ENV
mkdir -p trader/logs
echo "---- $(date -u +%Y-%m-%dT%H:%M:%SZ) briefing"
exec /usr/bin/python3 trader-broker/briefing.py "$@"
