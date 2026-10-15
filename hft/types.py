"""Базовые типы: стакан, сделки, заявки, исполнения.

Время везде — целые микросекунды от эпохи (UTC).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Side(Enum):
    BUY = 1
    SELL = -1

    @property
    def sign(self) -> int:
        return self.value

    @property
    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY


Level = tuple[float, int]  # (цена, количество контрактов)


@dataclass(slots=True)
class OrderBook:
    ts: int
    bids: list[Level]  # лучшая цена первой
    asks: list[Level]

    @property
    def valid(self) -> bool:
        return bool(self.bids) and bool(self.asks) and self.bids[0][0] < self.asks[0][0]

    @property
    def best_bid(self) -> float:
        return self.bids[0][0]

    @property
    def best_ask(self) -> float:
        return self.asks[0][0]

    @property
    def mid(self) -> float:
        return (self.bids[0][0] + self.asks[0][0]) / 2

    @property
    def spread(self) -> float:
        return self.asks[0][0] - self.bids[0][0]

    def qty_at(self, side: Side, price: float, eps: float = 1e-9) -> int:
        levels = self.bids if side is Side.BUY else self.asks
        for p, q in levels:
            if abs(p - price) < eps:
                return q
        return 0


@dataclass(slots=True)
class Trade:
    ts: int
    price: float
    qty: int
    aggressor: Side  # сторона, отправившая рыночную (агрессивную) заявку


class OrderType(Enum):
    LIMIT = "limit"
    IOC = "ioc"  # лимитная «исполнить или снять» — используется для закрытия позиции


class OrderStatus(Enum):
    PENDING_NEW = "pending_new"
    LIVE = "live"
    PENDING_CANCEL = "pending_cancel"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"

    @property
    def is_active(self) -> bool:
        return self in (OrderStatus.PENDING_NEW, OrderStatus.LIVE, OrderStatus.PENDING_CANCEL)


@dataclass(slots=True)
class Order:
    client_id: str
    side: Side
    price: float
    qty: int
    type: OrderType = OrderType.LIMIT
    filled: int = 0
    status: OrderStatus = OrderStatus.PENDING_NEW
    created_ts: int = 0

    @property
    def remaining(self) -> int:
        return self.qty - self.filled


@dataclass(slots=True)
class Fill:
    ts: int
    client_id: str
    side: Side
    price: float
    qty: int
    fee_rub: float
    maker: bool


# Команды от трейдера к площадке (бирже/брокеру/симулятору)

@dataclass(slots=True)
class PlaceOrder:
    client_id: str
    side: Side
    price: float
    qty: int
    type: OrderType = OrderType.LIMIT


@dataclass(slots=True)
class CancelOrder:
    client_id: str


Command = PlaceOrder | CancelOrder


@dataclass(slots=True)
class Quotes:
    """Желаемое состояние котировок, которое возвращает стратегия."""

    bid_px: float | None = None
    bid_qty: int = 0
    ask_px: float | None = None
    ask_qty: int = 0
    take_side: Side | None = None  # агрессивная сделка по сильному сигналу
    take_qty: int = 0
    flatten: bool = False  # снять всё и закрыть позицию
    meta: dict = field(default_factory=dict)

    @staticmethod
    def none() -> "Quotes":
        return Quotes()
