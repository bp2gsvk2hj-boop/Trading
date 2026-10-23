"""Bar-by-bar backtest using the live strategy and risk code.

Execution model (deliberately pessimistic):
  * decisions at bar close, fills at the next bar's open +/- slippage;
  * stop orders fill at the stop price, or at the open if the bar gaps through it;
  * fees on both sides; intraday positions closed at the session end.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

from .market import Candle, Instrument
from .risk import RiskConfig, Sessions, fee, size
from .strategies import MSK, Position, Strategy


@dataclass
class Trade:
    side: int
    qty: int
    entry_time: datetime
    entry: float
    exit_time: datetime
    exit: float
    pnl: float
    reason: str


@dataclass
class Result:
    trades: list[Trade] = field(default_factory=list)
    equity: list[tuple[datetime, float]] = field(default_factory=list)

    def stats(self, capital: float, since: datetime | None = None) -> dict:
        tr = [t for t in self.trades if since is None or t.entry_time >= since]
        eq = [(t, e) for t, e in self.equity if since is None or t >= since]
        pnl = sum(t.pnl for t in tr)
        wins = [t.pnl for t in tr if t.pnl > 0]
        losses = [t.pnl for t in tr if t.pnl <= 0]
        peak, mdd = -math.inf, 0.0
        for _, e in eq:
            peak = max(peak, e)
            mdd = max(mdd, peak - e)
        # daily returns for Sharpe
        daily: dict = {}
        for t, e in eq:
            daily[t.astimezone(MSK).date()] = e
        vals = list(daily.values())
        rets = [(b - a) / capital for a, b in zip(vals, vals[1:])]
        mean = sum(rets) / len(rets) if rets else 0.0
        sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / len(rets)) if rets else 0.0
        days = (eq[-1][0] - eq[0][0]).days if len(eq) > 1 else 0
        return {
            "trades": len(tr),
            "pnl_rub": round(pnl),
            "return_pct": round(100 * pnl / capital, 1),
            "annual_pct": round(100 * pnl / capital * 365 / days, 1) if days else 0.0,
            "win_rate": round(100 * len(wins) / len(tr)) if tr else 0,
            "profit_factor": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else (math.inf if wins else 0.0),
            "avg_trade_rub": round(pnl / len(tr), 1) if tr else 0.0,
            "max_dd_rub": round(mdd),
            "max_dd_pct": round(100 * mdd / capital, 1),
            "sharpe": round(mean / sd * math.sqrt(252), 2) if sd else 0.0,
        }


def run(strategy: Strategy, inst: Instrument, bars: list[Candle], risk: RiskConfig,
        sessions: Sessions | None = None) -> Result:
    sessions = sessions or Sessions()
    ind = strategy.prepare(bars)
    res = Result()
    pos = Position()
    st: dict = {}
    realized = 0.0
    slip = risk.slippage_ticks * inst.step
    pv = inst.point_value
    pending: tuple | None = None   # action to execute at the next bar's open

    def close(price: float, t: datetime, reason: str):
        nonlocal pos, realized
        pnl = (price - pos.entry) * pos.side * pos.qty * pv - fee(inst, price, pos.qty, risk.fee_rate, risk.fee_per_contract)
        entry_fee = fee(inst, pos.entry, pos.qty, risk.fee_rate, risk.fee_per_contract)
        realized += pnl
        res.trades.append(Trade(pos.side, pos.qty, pos.entry_time, pos.entry, t, price, pnl - entry_fee, reason))
        pos = Position()

    for i in range(len(bars)):
        b = bars[i]
        # 1. execute what was decided at the previous close
        if pending is not None:
            kind, side, stop, reason = pending
            pending = None
            if kind == "exit" and pos.side:
                close(b.o - slip * pos.side, b.t, reason)
            elif kind == "enter" and not pos.side:
                price = b.o + slip * side
                qty, _ = size(inst, price, stop, risk)
                if qty:
                    pos = Position(side, qty, price, stop, b.t, 0)
                    realized -= fee(inst, price, qty, risk.fee_rate, risk.fee_per_contract)
        # 2. protective stop inside the bar
        if pos.side == 1 and pos.stop is not None and b.l <= pos.stop:
            close(min(b.o, pos.stop) - slip, b.t, "stop")
        elif pos.side == -1 and pos.stop is not None and b.h >= pos.stop:
            close(max(b.o, pos.stop) + slip, b.t, "stop")
        # 3. decision at the close
        if i >= strategy.warmup and i + 1 < len(bars):
            now = strategy.close_time(b)
            nxt = bars[i + 1]
            if pos.side:
                pos.bars_held += 1
            if pos.side and strategy.intraday and (
                    sessions.must_flatten(now) or nxt.t.astimezone(MSK).date() != b.t.astimezone(MSK).date()):
                if nxt.t.astimezone(MSK).date() != b.t.astimezone(MSK).date():
                    close(b.c - slip * pos.side, now, "session end")
                else:
                    pending = ("exit", 0, None, "session end")
            else:
                a = strategy.decide(i, ind, pos, st)
                if a.kind == "exit" and pos.side:
                    pending = ("exit", 0, None, a.reason)
                elif a.kind == "stop" and pos.side and a.stop is not None:
                    pos.stop = inst.round_price(a.stop)
                elif a.kind == "enter" and not pos.side and sessions.can_enter(now) and not (
                        strategy.intraday and sessions.must_flatten(now)):
                    pending = ("enter", a.side, inst.round_price(a.stop), a.reason)
        mtm = realized + ((b.c - pos.entry) * pos.side * pos.qty * pv if pos.side else 0.0)
        res.equity.append((b.t, mtm))
    if pos.side:
        close(bars[-1].c, bars[-1].t, "end of data")
        res.equity.append((bars[-1].t, realized))
    return res
