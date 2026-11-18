"""Trend 1h: what the own server can improve in execution and schedule (research only).

python3 research/trend_exec.py [days]      # default 730; uses the cached history in data/

The live robot decides only on weekdays 10:00-18:50 MSK (it used to run from GitHub Actions).
On an own server it can also run in the evening session (19:05-23:50) and enter on the
breakout itself with an exchange stop order instead of waiting for the hourly close.
Variants, all with the live slots' parameters, fees and 1 step of slippage:
  day            decisions weekdays 10:00-18:50, entries 10:05-18:25 (current live robot)
  +evening trail the trailing stop and exits also run in the evening session
  +evening in    ... and new entries until 23:40
  stop entry     entry by a stop order at the channel level inside the next bar (day schedule)
Each also with the exit channel 7 (live) and 10 bars.
"""
from __future__ import annotations

import sys
from dataclasses import replace
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from robot.backtest import Result, Trade  # noqa: E402
from robot.config import load  # noqa: E402
from robot.continuous import build  # noqa: E402
from robot.engine import Engine  # noqa: E402
from robot.market import load_history, resolve  # noqa: E402
from robot.risk import fee, size  # noqa: E402
from robot.strategies import Position, make  # noqa: E402

MSK = timezone(timedelta(hours=3))
DAY = (time(10, 0), time(18, 50))
EVENING = (time(19, 5), time(23, 50))


def in_window(t: datetime, *wins) -> bool:
    m = t.astimezone(MSK)
    return m.weekday() < 5 and any(a <= m.time() <= b for a, b in wins)


def run(strategy, inst, bars, risk, sessions, decide_wins, entry_end: time, stop_entry: bool) -> Result:
    ind = strategy.prepare(bars)
    res, pos, st, realized = Result(), Position(), {}, 0.0
    slip, pv = risk.slippage_ticks * inst.step, inst.point_value
    pending = None          # ("exit"/"enter", side, stop) at the next open
    orders = None           # stop-entry orders for the next bar: [(side, level, atr)]

    def close(price, t, reason):
        nonlocal pos, realized
        pnl = (price - pos.entry) * pos.side * pos.qty * pv - fee(inst, price, pos.qty, risk.fee_rate)
        realized += pnl
        res.trades.append(Trade(pos.side, pos.qty, pos.entry_time, pos.entry, t, price,
                                pnl - fee(inst, pos.entry, pos.qty, risk.fee_rate), reason))
        pos = Position()

    def open_(side, price, stop, t):
        nonlocal pos, realized
        qty, _ = size(inst, price, stop, risk)
        if qty:
            pos = Position(side, qty, price, inst.round_price(stop), t, 0)
            realized -= fee(inst, price, qty, risk.fee_rate)

    for i, b in enumerate(bars):
        if pending is not None:
            kind, side, stop = pending
            pending = None
            if kind == "exit" and pos.side:
                close(b.o - slip * pos.side, b.t, "exit")
            elif kind == "enter" and not pos.side:
                open_(side, b.o + slip * side, stop, b.t)
        if orders and not pos.side:
            for side, level, a in orders:
                hit = b.h >= level if side == 1 else b.l <= level
                if hit:
                    px = (max(b.o, level) if side == 1 else min(b.o, level)) + slip * side
                    open_(side, px, px - side * strategy.p["stop_atr"] * a, b.t)
                    break
        orders = None
        if pos.side == 1 and pos.stop is not None and b.l <= pos.stop:
            close(min(b.o, pos.stop) - slip, b.t, "stop")
        elif pos.side == -1 and pos.stop is not None and b.h >= pos.stop:
            close(max(b.o, pos.stop) + slip, b.t, "stop")
        if i >= strategy.warmup and i + 1 < len(bars):
            now = strategy.close_time(b)
            if pos.side:
                pos.bars_held += 1
            if in_window(now, *decide_wins):
                can_in = sessions.can_enter(now) or (entry_end > DAY[1] and in_window(now, (EVENING[0], entry_end)))
                if stop_entry and not pos.side:
                    # resting stop orders at the channel, in the EMA direction, for the next bar
                    c, a, e = ind["c"][i], ind["atr"][i], (ind["ema"][i] if ind["ema"] else None)
                    hh = max(ind["hh"][i], b.h) if ind["hh"][i] is not None else None
                    ll = min(ind["ll"][i], b.l) if ind["ll"][i] is not None else None
                    if can_in and a and hh is not None:
                        orders = [(s, lvl + s * inst.step, a) for s, lvl in ((1, hh), (-1, ll))
                                  if e is None or (c > e if s == 1 else c < e)]
                else:
                    act = strategy.decide(i, ind, pos, st)
                    if act.kind == "exit" and pos.side:
                        pending = ("exit", 0, None)
                    elif act.kind == "stop" and pos.side and act.stop is not None:
                        pos.stop = inst.round_price(act.stop)
                    elif act.kind == "enter" and not pos.side and can_in:
                        pending = ("enter", act.side, inst.round_price(act.stop))
                if stop_entry and pos.side:
                    act = strategy.decide(i, ind, pos, st)
                    if act.kind == "exit":
                        pending = ("exit", 0, None)
                    elif act.kind == "stop" and act.stop is not None:
                        pos.stop = inst.round_price(act.stop)
        res.equity.append((b.t, realized + ((b.c - pos.entry) * pos.side * pos.qty * pv if pos.side else 0.0)))
    if pos.side:
        close(bars[-1].c, bars[-1].t, "end")
    return res


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 730
    cfg = load()
    c = Engine(cfg, read_only=True).c
    slots = [s for s in cfg.slots if s.enabled]
    data = {}
    for s in slots:
        if s.ticker.startswith("front:"):
            _, asset, prefix = s.ticker.split(":")
            inst = resolve(c, s.ticker)
            bars = build(c, asset, prefix, s.interval, days)[0]
        else:
            inst = resolve(c, s.ticker)
            bars = load_history(c, s.ticker, s.interval, days)
        data[s.ticker] = (s, inst, bars)
    split = datetime.now(timezone.utc) - timedelta(days=days // 2)
    variants = [
        ("day (live)", [DAY], DAY[1], False),
        ("+evening trail", [DAY, EVENING], DAY[1], False),
        ("+evening in", [DAY, EVENING], time(23, 40), False),
        ("stop entry", [DAY], DAY[1], True),
        ("stop entry+eve", [DAY, EVENING], DAY[1], True),
    ]
    cap = cfg.risk.capital_rub
    print(f"capital {cap:.0f} RUB, {days} days; P&L in RUB (1st half / 2nd half), max drawdown")
    for exit_n in (7, 10):
        print(f"\nexit channel {exit_n} bars")
        for name, wins, entry_end, stop_entry in variants:
            tot = h1 = h2 = 0.0
            parts = []
            curves = []
            for tk, (s, inst, bars) in data.items():
                strat = make(s.strategy, s.interval, {**s.params, "exit_n": exit_n})
                r = run(strat, inst, bars, cfg.risk, cfg.sessions, wins, entry_end, stop_entry)
                p = sum(t.pnl for t in r.trades)
                tot += p
                h1 += sum(t.pnl for t in r.trades if t.entry_time < split)
                h2 += sum(t.pnl for t in r.trades if t.entry_time >= split)
                parts.append(f"{tk.split(':')[-1]} {p:+.0f} ({len(r.trades)})")
                curves.append(dict(r.equity))
            # portfolio equity: sum of the slots' curves, each carried forward between its bars
            last = [0.0] * len(curves)
            peak = mdd = 0.0
            for t in sorted(set().union(*curves)):
                for j, cv in enumerate(curves):
                    if t in cv:
                        last[j] = cv[t]
                e = sum(last)
                peak = max(peak, e)
                mdd = max(mdd, peak - e)
            print(f"  {name:16} {tot:+9.0f} ({tot / cap * 100:+.1f}%)  {h1:+8.0f} / {h2:+8.0f}  DD {mdd:7.0f}   " + ", ".join(parts))


if __name__ == "__main__":
    main()
