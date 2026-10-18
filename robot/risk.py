"""Trading-hours rules and position sizing, shared by backtest and live engine."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, time

from .market import Instrument
from .strategies import MSK, parse_hhmm


@dataclass
class RiskConfig:
    capital_rub: float = 5000.0
    risk_per_trade: float = 0.01        # target risk per trade, share of capital
    max_risk_per_trade: float = 0.03    # hard cap: skip the trade if even 1 contract risks more
    max_margin_share: float = 0.6       # GO of all robot positions <= this share of capital
    max_contracts: int = 5
    max_stopouts_per_day: int = 2       # no new entries after this many stop-loss exits today
    fee_rate: float = 0.0005            # broker+exchange fee per side, share of contract value
    slippage_ticks: int = 1             # per side, in backtests


@dataclass
class Sessions:
    entry_start: str = "10:05"
    entry_end: str = "18:25"
    no_entry: list = field(default_factory=lambda: [["13:50", "14:10"]])
    flatten_at: str = "18:30"           # intraday strategies are closed from here on
    weekdays_only: bool = True

    def can_enter(self, now: datetime) -> bool:
        m = now.astimezone(MSK)
        if self.weekdays_only and m.weekday() >= 5:
            return False
        t = m.time()
        if not (parse_hhmm(self.entry_start) <= t <= parse_hhmm(self.entry_end)):
            return False
        return not any(parse_hhmm(a) <= t < parse_hhmm(b) for a, b in self.no_entry)

    def must_flatten(self, now: datetime) -> bool:
        m = now.astimezone(MSK)
        return m.time() >= parse_hhmm(self.flatten_at) or m.time() < time(9, 0)


def fee(inst: Instrument, price: float, qty: int, rate: float) -> float:
    return abs(price) * inst.point_value * abs(qty) * rate


def size(inst: Instrument, entry: float, stop: float, cfg: RiskConfig, margin_in_use: float = 0.0) -> tuple[int, str]:
    """Contracts to trade and a note. 0 means skip."""
    per_contract = abs(entry - stop) * inst.point_value + 2 * fee(inst, entry, 1, cfg.fee_rate)
    if per_contract <= 0:
        return 0, "zero stop distance"
    if per_contract > cfg.capital_rub * cfg.max_risk_per_trade:
        return 0, f"1 contract risks {per_contract:.0f} RUB > cap {cfg.capital_rub * cfg.max_risk_per_trade:.0f}"
    by_risk = max(1, math.floor(cfg.capital_rub * cfg.risk_per_trade / per_contract))
    margin_left = cfg.capital_rub * cfg.max_margin_share - margin_in_use
    by_margin = math.floor(margin_left / inst.go) if inst.go > 0 else 0
    qty = min(by_risk, by_margin, cfg.max_contracts)
    if qty <= 0:
        return 0, f"not enough margin: GO {inst.go:.0f}, free {margin_left:.0f}"
    return qty, f"risk {per_contract * qty:.0f} RUB, GO {inst.go * qty:.0f} RUB"
