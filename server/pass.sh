#!/usr/bin/env bash
# One robot pass on the server, the same steps as .github/workflows/robot.yml.
#   pass.sh pass        trading pass (systemd timer every 15 min, 10:02-18:47 MSK)
#   pass.sh collateral  LQDT rebalance only (18:45 MSK; fallbacks 19:35 and 22:35)
#   pass.sh check       read-only: broker status + Telegram test message
#   pass.sh trader      rebuild the «Помощник трейдера» data only (00:23 MSK, after the clearing)
#   pass.sh news        broker market digest to Telegram + page /news/ (09:00 and 18:00 MSK)
# Secrets come from /etc/trading-robot.env (systemd EnvironmentFile).
set -uo pipefail
MODE="${1:-pass}"
cd "$(dirname "$0")/.."
# Broker market digest: its own lock, so trading passes never wait for it (it takes a few minutes).
if [ "$MODE" = news ]; then
  exec 8>/tmp/brokernews.lock
  flock -n 8 || { echo "[warn] digest already running"; exit 0; }
  git pull -q --ff-only origin main || echo "[warn] git pull failed, running the current code"
  exec "${PYTHON:-python3}" -m brokernews run ${TRADER_WEB:+--web "$TRADER_WEB/news"}
fi
# One pass at a time: the 18:45 rebalance and the 18:47 trading pass must not overlap.
exec 9>/tmp/trading-robot.lock
flock 9
git pull -q --ff-only origin main || echo "[warn] git pull failed, running the current code"

[ "$MODE" = check ] && exec server/check.sh
PY="${PYTHON:-python3}"

# «Помощник трейдера» (served by nginx from $TRADER_WEB): page from the repo, data from the account.
trader() {
  [ -n "${TRADER_WEB:-}" ] && [ -d "$TRADER_WEB" ] || return 0
  cp trader/index.html "$TRADER_WEB/index.html"
  $PY trader/build_data.py --out "$TRADER_WEB/data.json.new" >/dev/null \
    && mv "$TRADER_WEB/data.json.new" "$TRADER_WEB/data.json" || echo "[warn] trader data not rebuilt"
}
[ "$MODE" = trader ] && { trader; exit 0; }
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
trader
exit "$rc"
