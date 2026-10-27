"""CLI: python -m robot <command>. See README.md."""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone

from . import backtest as bt
from .api import Client
from .config import load
from .engine import Engine, Halted
from .market import INTERVALS, load_history, resolve
from .strategies import STRATEGIES, make


def cmd_backtest(a, cfg):
    c = Client()
    if a.ticker:
        slots = [(a.ticker, a.strategy, a.interval, json.loads(a.params) if a.params else {}, None)]
    else:
        slots = [(s.ticker, s.strategy, s.interval, s.params, s.fee_rate) for s in cfg.slots if s.enabled]
    split = datetime.now(timezone.utc) - timedelta(days=a.test_days)
    total = 0.0
    for ticker, strat_name, interval, params, fee_rate in slots:
        inst = resolve(c, ticker)
        bars = load_history(c, inst.ticker, interval, a.days, refresh=a.refresh)
        risk = cfg.risk if fee_rate is None else replace(cfg.risk, fee_rate=fee_rate)
        res = bt.run(make(strat_name, interval, params), inst, bars, risk, cfg.sessions)
        full = res.stats(cfg.risk.capital_rub)
        test = res.stats(cfg.risk.capital_rub, since=split)
        total += full["pnl_rub"]
        print(f"\n{inst.ticker} {strat_name} {interval} {params}")
        print(f"  bars {len(bars)} from {bars[0].t:%Y-%m-%d}; GO {inst.go:.0f} RUB; 1 point = {inst.point_value:g} RUB")
        print(f"  all : {full}")
        print(f"  last {a.test_days}d: {test}")
        if a.trades:
            for t in res.trades[-a.trades:]:
                print(f"    {t.entry_time:%Y-%m-%d %H:%M} {'L' if t.side > 0 else 'S'}x{t.qty} {t.entry:g} -> "
                      f"{t.exit_time:%Y-%m-%d %H:%M} {t.exit:g}  {t.pnl:+.0f} RUB  {t.reason}")
    print(f"\nTotal P&L, all slots: {total:+.0f} RUB on capital {cfg.risk.capital_rub:.0f} RUB")


def cmd_run(a, cfg):
    for line in Engine(cfg, dry_run=a.dry_run).run_once():
        print(line)


def cmd_loop(a, cfg):
    step = min(INTERVALS[s.interval][2] for s in cfg.slots if s.enabled)
    print(f"loop: mode={cfg.mode}, every bar close of {step}, Ctrl+C to stop", flush=True)
    while True:
        try:
            for line in Engine(cfg, dry_run=a.dry_run).run_once():
                print(datetime.now().strftime("%H:%M:%S"), line, flush=True)
        except Halted as e:
            print("halted:", e, flush=True)
            return
        except Exception as e:  # keep looping on transient errors
            print("error:", type(e).__name__, e, flush=True)
        now = datetime.now(timezone.utc)
        secs = step.total_seconds()
        wait = secs - (now.timestamp() % secs) + 20  # 20 s after the bar closes
        time.sleep(max(60.0, min(wait, a.max_sleep)))


def cmd_status(a, cfg):
    print(json.dumps(Engine(cfg, read_only=True).status(), indent=1, ensure_ascii=False))


def cmd_flatten(a, cfg):
    for line in Engine(cfg, dry_run=a.dry_run).flatten():
        print(line)


def cmd_resize(a, cfg):
    for line in Engine(cfg, dry_run=a.dry_run).resize():
        print(line)


def cmd_halt(a, cfg):
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    (cfg.state_dir / "HALT").write_text(a.reason + "\n")
    print(f"halted: {a.reason}. Open positions and their exchange stops are left as they are; "
          f"use `flatten` to close them.")


def cmd_resume(a, cfg):
    (cfg.state_dir / "HALT").unlink(missing_ok=True)
    print("resumed")


def cmd_journal(a, cfg):
    p = cfg.state_dir / "journal.jsonl"
    lines = p.read_text().splitlines()[-a.n:] if p.exists() else []
    print("\n".join(lines) or "journal is empty")


def cmd_sandbox_init(a, cfg):
    c = Client()
    acc = c.call("SandboxService/OpenSandboxAccount", {"name": "robot"})["accountId"]
    c.call("SandboxService/SandboxPayIn", {"accountId": acc, "amount": {
        "currency": "rub", "units": str(int(cfg.risk.capital_rub)), "nano": 0}})
    text = cfg.path.read_text()
    text = re.sub(r'(?m)^mode\s*=.*$', 'mode = "sandbox"', text, count=1)
    text = re.sub(r'(?m)^account_id\s*=.*$', f'account_id = "{acc}"', text, count=1)
    cfg.path.write_text(text)
    print(f"sandbox account {acc} funded with {cfg.risk.capital_rub:.0f} RUB; {cfg.path.name} switched to sandbox")


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m robot", description="Futures trading robot for T-Invest")
    p.add_argument("--config", default=None, help="path to robot.toml")
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("backtest", help="backtest configured slots or one strategy")
    b.add_argument("--ticker"); b.add_argument("--strategy", choices=sorted(STRATEGIES), default="trend")
    b.add_argument("--interval", choices=sorted(INTERVALS), default="1h")
    b.add_argument("--params", help='JSON, e.g. {"entry_n": 40}')
    b.add_argument("--days", type=int, default=730); b.add_argument("--test-days", type=int, default=240)
    b.add_argument("--trades", type=int, default=0, help="print the last N trades")
    b.add_argument("--refresh", action="store_true", help="re-download candles")
    b.set_defaults(fn=cmd_backtest)

    r = sub.add_parser("run", help="one trading pass (for cron / scheduled tasks)")
    r.add_argument("--dry-run", action="store_true", help="decide but send no orders")
    r.set_defaults(fn=cmd_run)

    lp = sub.add_parser("loop", help="run a pass after every bar close until stopped")
    lp.add_argument("--dry-run", action="store_true"); lp.add_argument("--max-sleep", type=float, default=900)
    lp.set_defaults(fn=cmd_loop)

    sub.add_parser("status", help="positions, stops, money").set_defaults(fn=cmd_status)
    f = sub.add_parser("flatten", help="cancel robot stops and close robot positions")
    f.add_argument("--dry-run", action="store_true"); f.set_defaults(fn=cmd_flatten)
    r = sub.add_parser("resize", help="cut positions to the risk limits of the current capital_rub")
    r.add_argument("--dry-run", action="store_true"); r.set_defaults(fn=cmd_resize)
    h = sub.add_parser("halt", help="stop opening new trades"); h.add_argument("reason", nargs="?", default="manual halt")
    h.set_defaults(fn=cmd_halt)
    sub.add_parser("resume", help="remove the halt").set_defaults(fn=cmd_resume)
    j = sub.add_parser("journal", help="last journal entries"); j.add_argument("-n", type=int, default=30)
    j.set_defaults(fn=cmd_journal)
    sub.add_parser("sandbox-init", help="open and fund a sandbox account, switch config to it").set_defaults(fn=cmd_sandbox_init)

    a = p.parse_args(argv)
    cfg = load(a.config) if a.config else load()
    try:
        a.fn(a, cfg)
    except Halted as e:
        print("refused:", e, file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
