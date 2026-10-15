"""Микроструктурные сигналы: дисбаланс стакана, microprice, поток сделок, волатильность."""
from __future__ import annotations

import math

from .types import OrderBook, Side, Trade


def book_imbalance(book: OrderBook, levels: int = 3, decay: float = 0.5) -> float:
    """Взвешенный дисбаланс объёмов в [-1, 1]; > 0 — давление покупателей."""
    bid = ask = 0.0
    w = 1.0
    for i in range(levels):
        if i < len(book.bids):
            bid += w * book.bids[i][1]
        if i < len(book.asks):
            ask += w * book.asks[i][1]
        w *= decay
    total = bid + ask
    return 0.0 if total <= 0 else (bid - ask) / total


def microprice(book: OrderBook) -> float:
    """Цена, взвешенная объёмами лучших уровней (Stoikov)."""
    (bp, bq), (ap, aq) = book.bids[0], book.asks[0]
    total = bq + aq
    return book.mid if total <= 0 else (bp * aq + ap * bq) / total


class TimeDecayEwma:
    """EWMA с затуханием по реальному времени: tau — характерное время в секундах."""

    def __init__(self, tau_sec: float):
        self.tau_us = tau_sec * 1e6
        self.value = 0.0
        self.ts: int | None = None

    def decay_to(self, ts: int) -> None:
        if self.ts is not None and ts > self.ts:
            self.value *= math.exp(-(ts - self.ts) / self.tau_us)
        self.ts = ts if self.ts is None else max(self.ts, ts)

    def add(self, ts: int, x: float) -> None:
        self.decay_to(ts)
        self.value += x

    def get(self, ts: int) -> float:
        self.decay_to(ts)
        return self.value


class TradeFlow:
    """Нормированный дисбаланс агрессивного потока сделок в [-1, 1]."""

    def __init__(self, tau_sec: float = 2.0):
        self.signed = TimeDecayEwma(tau_sec)
        self.total = TimeDecayEwma(tau_sec)

    def on_trade(self, trade: Trade) -> None:
        self.signed.add(trade.ts, trade.aggressor.sign * trade.qty)
        self.total.add(trade.ts, trade.qty)

    def value(self, ts: int) -> float:
        tot = self.total.get(ts)
        return 0.0 if tot <= 1e-12 else self.signed.get(ts) / tot


class MidVolatility:
    """Оценка волатильности mid в пунктах за `horizon_sec` (EWMA квадратов приращений)."""

    def __init__(self, tau_sec: float = 30.0, horizon_sec: float = 1.0):
        self.tau_sec = tau_sec
        self.horizon_sec = horizon_sec
        self.var_rate = 0.0  # дисперсия в пунктах^2 за секунду
        self.last_mid: float | None = None
        self.last_ts: int | None = None

    def on_mid(self, ts: int, mid: float) -> None:
        if self.last_mid is not None and self.last_ts is not None and ts > self.last_ts:
            dt = (ts - self.last_ts) / 1e6
            a = 1.0 - math.exp(-dt / self.tau_sec)
            self.var_rate += a * ((mid - self.last_mid) ** 2 / max(dt, 1e-3) - self.var_rate)
        self.last_mid, self.last_ts = mid, ts

    @property
    def value(self) -> float:
        return math.sqrt(max(self.var_rate, 0.0) * self.horizon_sec)


def side_of(x: float) -> Side | None:
    if x > 0:
        return Side.BUY
    if x < 0:
        return Side.SELL
    return None
