---
name: trading-robot
description: Operate the futures trading robot in this repo (python -m robot) — run a trading pass, report status and P&L, backtest strategy changes, halt or flatten. Use when the user asks to run/check/stop the robot, review its trades, or change its strategies or risk settings.
---

# Trading robot operations

The robot lives in `robot/`, config in `robot.toml`, journal in `state/journal.jsonl`.
Read `README.md` for the strategy rationale and backtest results.

## Routine pass (scheduled or on request)

1. `python -m robot run` — one idempotent pass. Safe to repeat.
2. `python -m robot status` — positions, protective stops, free money.
3. Report in a few lines: mode, what happened (entries/exits/stop moves/skips/errors from the
   run output), open positions with unrealized P&L. If nothing happened, say so in one line.

Any `[error]`, `[warn]`, `closed_outside`, or a position without a stop in `status` must be
reported to the user explicitly, never just logged.

## Signal-only robot

The live robot is «Торговый робот Тбанк» (`robot.toml`); the signal robot is «Торговые сигналы Сбер».
`robot-signals.toml` runs the same strategy on a virtual 6 million RUB account (paper mode) and
only sends Telegram signals. It is tuned for its size (risk 1.25%, margin cap 80%, plus USDRUBF);
when changing the strategy params or sessions in `robot.toml`, make the same change there. Never switch it to sandbox or live mode.

## Hard rules

- **Never switch to live trading on your own.** `mode = "live"` and `ROBOT_LIVE=yes` are set only
  when the user explicitly asks for real-money trading in this conversation. Changing
  `account_id`, `capital_rub`, risk limits or adding instruments also needs the user's explicit OK.
- Do not place, modify or cancel orders on the robot's instruments by hand (MCP t-invest tools or
  API calls) while the robot runs, except `flatten` / `halt` when the user asks or an emergency
  below applies. Manual trades on robot instruments break its reconciliation.
- Emergency (an error repeats on consecutive passes, a position has no stop and one cannot be
  placed, or losses exceed the configured limits): run `python -m robot halt "<reason>"`, then tell
  the user. Flatten only if the user agrees or a position is unprotected.
- Strategy or parameter changes: backtest first (`python -m robot backtest`, compare the
  "all" and "last 240d" rows before/after), run `python -m unittest discover -s tests -t .`, and show
  the numbers to the user before changing `robot.toml`. Do not tune parameters to the test
  period; prefer settings that work across neighboring values.

## Cloud scheduled runs

`.claude/settings.json` pre-approves exactly these commands, so run them verbatim as a single
Bash call each — no `cd … &&`, pipes or extra flags, or the approval does not match:
`ROBOT_LIVE=yes python3 -m robot run`, `ROBOT_LIVE=yes python3 -m robot flatten`,
`python3 -m robot status`. If a command is still refused, report it; never work around it.

Each scheduled run is a fresh container: `state/` (journal, HALT file) does not survive between
runs. The broker is the source of truth for positions and stops, so trading is unaffected, but:
the run report in the session transcript is the journal, and stopping the robot means disabling
its scheduled task (plus `python -m robot flatten` if positions must be closed), not `halt`.

## Useful commands

```bash
python -m robot run --dry-run      # decisions without orders
python -m robot journal -n 50
python -m robot halt "reason" / resume / flatten
python -m robot backtest --ticker CNYRUBF --strategy trend --interval 1h --params '{"entry_n": 40}' --trades 10
```

`TINVEST_TOKEN` must be set. The API host uses the Russian Trusted Root CA (bundled in
`certs/`); in the cloud environment outbound HTTPS goes through the proxy automatically.
