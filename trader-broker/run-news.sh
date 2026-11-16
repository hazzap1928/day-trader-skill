#!/bin/zsh
# The morning news run (demo): launchd runs this at 12:00 UK time on weekdays, hours before the
# 09:30 New York open. news_ideas.py itself skips US market holidays.
#
# It asks Claude Code (claude -p, your own plan: no API key is used or needed here) for forward-looking
# ideas, checks each one with this pack's rules, logs them and sends one message. It never places an
# order: with "news": {"buy_enabled": true} the autopilot does the buying later, at 09:35 New York.
#
# Like run-briefing.sh it reads only these export lines from T212_ENV_FILE (default ~/.zshrc):
# T212_API_KEY, T212_API_SECRET (read-only use: the account, for sizing) and TELEGRAM_BOT_TOKEN.
# The Claude run itself is started WITHOUT them in its environment.
set -eu
cd "${0:A:h}/.."
envfile="${T212_ENV_FILE:-$HOME/.zshrc}"
if [[ -z "${T212_API_KEY:-}" || -z "${T212_API_SECRET:-}" ]]; then
  eval "$(grep -E '^[[:space:]]*export[[:space:]]+T212_(API_KEY|API_SECRET)=' "$envfile" || true)"
fi
if [[ -z "${TELEGRAM_BOT_TOKEN:-}" ]]; then
  eval "$(grep -E '^[[:space:]]*export[[:space:]]+TELEGRAM_BOT_TOKEN=' "$envfile" || true)"
fi
unset T212_ENV
mkdir -p trader/logs
echo "---- $(date -u +%Y-%m-%dT%H:%M:%SZ) news run"
exec /usr/bin/python3 trader-broker/news_ideas.py "${@:-run}"
