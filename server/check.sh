#!/usr/bin/env bash
# Read-only check on the server: broker status and a Telegram test message. Places no orders.
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PYTHON:-python3}"
$PY -m robot status > /tmp/robot-status.json
$PY -c 'import json,sys; s=json.load(open(sys.argv[1])); print("Брокер отвечает: счёт", s["account"], "режим", s["mode"])' /tmp/robot-status.json
: > /tmp/robot-empty.log
$PY -m robot.telegram --run /tmp/robot-empty.log --status /tmp/robot-status.json --summary
