#!/bin/zsh
# The two news runs of each US trading day (demo). launchd starts this:
#   - "run --kind main"   at 06:00 UK on weekdays: the main review of overnight news, results and filings.
#                         Its ideas go into the 07:30 briefing, not a message of their own.
#   - "run --kind update" at 12:30 AND 13:30 UK on weekdays: the shorter update at 08:30 New York. Only the
#                         start that falls at or after 08:30 New York does anything (13:30 UK most of the
#                         year, 12:30 UK in the few weeks when the US and UK clocks are an hour closer).
# news_ideas.py itself skips US market holidays, a run already done, and a run that can no longer finish in
# time. If the Mac was asleep, the always-on autopilot starts the missed run when it wakes.
#
# It asks Claude Code (claude -p, your own plan: no API key is used or needed here), checks each idea with
# this pack's rules and logs it. It never places an order: with "news": {"buy_enabled": true} the autopilot
# does the buying later, at 09:35 New York, and only from an update run that finished.
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
echo "---- $(date -u +%Y-%m-%dT%H:%M:%SZ) news ${*:-run}"
exec /usr/bin/python3 trader-broker/news_ideas.py "${@:-run}"
