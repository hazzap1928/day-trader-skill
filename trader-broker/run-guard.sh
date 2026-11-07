#!/bin/zsh
# Start the Trading 212 stop guardian for the day (demo only, never buys):
#   t212.py guard --watch  waits for the 9:30 New York open, checks every 5 minutes, stops at 16:00.
# For launchd, which does not read your shell profile: if the keys are not already in the
# environment, only the two "export T212_API_KEY=" / "export T212_API_SECRET=" lines are
# read from T212_ENV_FILE (default ~/.zshrc). Nothing else in that file is run.
set -eu
cd "${0:A:h}/.."
if [[ -z "${T212_API_KEY:-}" || -z "${T212_API_SECRET:-}" ]]; then
  envfile="${T212_ENV_FILE:-$HOME/.zshrc}"
  eval "$(grep -E '^[[:space:]]*export[[:space:]]+T212_API_(KEY|SECRET)=' "$envfile")"
fi
unset T212_ENV            # demo only: the guard refuses live anyway
mkdir -p trader/logs
echo "---- $(date -u +%Y-%m-%dT%H:%M:%SZ) guard start"
exec /usr/bin/python3 trader-broker/t212.py guard --watch "$@"
