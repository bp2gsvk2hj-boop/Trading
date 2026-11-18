#!/usr/bin/env bash
# One robot pass on the server, the same steps as .github/workflows/robot.yml.
#   pass.sh pass        trading pass (systemd timer every 15 min, 10:02-18:47 MSK)
#   pass.sh collateral  LQDT rebalance only (18:45 MSK; fallbacks 19:35 and 22:35)
#   pass.sh check       read-only: broker status + Telegram test message
# Secrets come from /etc/trading-robot.env (systemd EnvironmentFile).
set -uo pipefail
MODE="${1:-pass}"
cd "$(dirname "$0")/.."
# One pass at a time: the 18:45 rebalance and the 18:47 trading pass must not overlap.
exec 9>/tmp/trading-robot.lock
flock 9
git pull -q --ff-only origin main || echo "[warn] git pull failed, running the current code"

[ "$MODE" = check ] && exec server/check.sh
PY="${PYTHON:-python3}"
LOG=$(mktemp); STATUS=$(mktemp); trap 'rm -f "$LOG" "$STATUS"' EXIT
if [ "$MODE" = collateral ]; then
  $PY -m robot collateral 2>&1 | tee "$LOG"; rc=${PIPESTATUS[0]}
else
  $PY -m robot run 2>&1 | tee "$LOG"; rc=${PIPESTATUS[0]}
fi
$PY -m robot status > "$STATUS" 2>&1 || true

args=(--run "$LOG" --status "$STATUS" --trades)
[ "$rc" -eq 0 ] || args+=(--exit-code "$rc")
# Last trading pass of the day (18:47 MSK): daily summary.
if [ "$MODE" = pass ] && [ "$(TZ=Europe/Moscow date +%H%M)" -ge 1845 ]; then
  args+=(--summary)
fi
$PY -m robot.telegram "${args[@]}" || echo "[warn] telegram notification failed"
exit "$rc"
