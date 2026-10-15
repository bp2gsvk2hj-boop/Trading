"""Трейдер: связывает стратегию, риск-менеджмент и площадку исполнения.

Один и тот же класс работает и в бэктесте (площадка — симулятор биржи),
и вживую (площадка — коннектор к брокеру). Площадка сообщает о подтверждениях,
снятиях и исполнениях через методы `on_ack`, `on_cancelled`, `on_reject`, `on_fill`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .instruments import Instrument
from .risk import RiskManager
from .sessions import SessionState, TradingSchedule
from .strategy import OBIMarketMaker
from .types import (
    CancelOrder,
    Command,
    Fill,
    Order,
    OrderBook,
    OrderStatus,
    OrderType,
    PlaceOrder,
    Quotes,
    Side,
    Trade,
)

EPS = 1e-9


class Venue(Protocol):
    def send(self, cmd: Command) -> None: ...


@dataclass
class TraderConfig:
    # Минимальный интервал между перестановками заявки «внутрь» по одной стороне.
    # Отодвинуть заявку от рынка можно всегда сразу — это защита от неблагоприятного исполнения.
    requote_min_interval_ms: float = 50.0
    flatten_slippage_ticks: int = 5
    client_id_prefix: str = "mm"


class Trader:
    def __init__(
        self,
        instrument: Instrument,
        strategy: OBIMarketMaker,
        risk: RiskManager,
        venue: Venue,
        schedule: TradingSchedule | None = None,
        config: TraderConfig | None = None,
    ):
        self.inst = instrument
        self.strategy = strategy
        self.risk = risk
        self.venue = venue
        self.schedule = schedule or TradingSchedule()
        self.cfg = config or TraderConfig()

        self.position = 0
        self.cash_rub = 0.0
        self.fees_rub = 0.0
        self.orders: dict[str, Order] = {}  # только активные заявки
        self.fills: list[Fill] = []
        self.last_book: OrderBook | None = None
        self.n_commands = 0
        self.n_orders = 0
        self._seq = 0
        self._last_side_action: dict[Side, int] = {Side.BUY: -(10**18), Side.SELL: -(10**18)}
        self.last_quotes: Quotes | None = None

    # ------------------------------------------------------------------ учёт
    def equity_rub(self, mark: float | None = None) -> float:
        if mark is None:
            mark = self.last_book.mid if self.last_book and self.last_book.valid else 0.0
        return self.cash_rub + self.position * mark * self.inst.rub_per_point

    # ------------------------------------------------------- рыночные данные
    def on_book(self, book: OrderBook) -> None:
        self.last_book = book
        self.strategy.on_book(book)
        self._act(book.ts)

    def on_trade(self, trade: Trade) -> None:
        self.strategy.on_trade(trade)
        self._act(trade.ts)

    # ---------------------------------------------------- события площадки
    def on_ack(self, client_id: str, ts: int) -> None:
        o = self.orders.get(client_id)
        if o and o.status is OrderStatus.PENDING_NEW:
            o.status = OrderStatus.LIVE

    def on_cancelled(self, client_id: str, ts: int) -> None:
        o = self.orders.get(client_id)
        if o and o.status.is_active:
            o.status = OrderStatus.CANCELLED
            self._finalize(o)
        self._act(ts)

    def on_reject(self, client_id: str, ts: int, reason: str = "") -> None:
        o = self.orders.get(client_id)
        if o and o.status.is_active:
            # Отказ в снятии (заявка уже исполнена) приходит раньше/позже исполнения —
            # статус финализирует исполнение; отказ в постановке закрывает заявку.
            if o.status is OrderStatus.PENDING_CANCEL and o.remaining > 0:
                o.status = OrderStatus.LIVE
            elif o.status is OrderStatus.PENDING_NEW:
                o.status = OrderStatus.REJECTED
                self._finalize(o)

    def on_fill(self, fill: Fill) -> None:
        o = self.orders.get(fill.client_id)
        if o is not None:
            o.filled += fill.qty
            if o.remaining <= 0:
                o.status = OrderStatus.FILLED
                self._finalize(o)
        self.position += fill.side.sign * fill.qty
        self.cash_rub -= fill.side.sign * fill.qty * fill.price * self.inst.rub_per_point
        self.cash_rub -= fill.fee_rub
        self.fees_rub += fill.fee_rub
        self.fills.append(fill)
        self.strategy.on_fill(fill)
        self._act(fill.ts)

    # ------------------------------------------------------------- логика
    def _finalize(self, o: Order) -> None:
        # в self.orders держим только активные заявки, чтобы обход оставался дешёвым
        self.orders.pop(o.client_id, None)

    def _active(self, side: Side | None = None, type_: OrderType | None = None) -> list[Order]:
        return [
            o
            for o in self.orders.values()
            if o.status.is_active
            and (side is None or o.side is side)
            and (type_ is None or o.type is type_)
        ]

    def _exposure_after(self, side: Side, qty: int) -> int:
        pending = sum(o.remaining for o in self._active(side))
        return self.position + side.sign * (pending + qty)

    def _send(self, cmd: Command, ts: int) -> None:
        self.risk.record_command(ts)
        self.n_commands += 1
        self.venue.send(cmd)

    def _cancel(self, o: Order, ts: int) -> None:
        if o.status in (OrderStatus.PENDING_NEW, OrderStatus.LIVE):
            o.status = OrderStatus.PENDING_CANCEL
            self._send(CancelOrder(o.client_id), ts)

    def _place(self, side: Side, price: float, qty: int, ts: int, type_: OrderType = OrderType.LIMIT) -> bool:
        if not self.risk.qty_ok(qty) or not self.risk.rate_ok(ts):
            return False
        # закрывающая заявка не увеличивает |позицию|, поэтому лимит позиции к ней не применяем
        reduces = self.position * side.sign < 0 and qty <= abs(self.position)
        if not reduces and not self.risk.position_ok(self._exposure_after(side, qty)):
            return False
        self._seq += 1
        cid = f"{self.cfg.client_id_prefix}{self._seq}"
        price = self.inst.round_price(price)
        self.orders[cid] = Order(cid, side, price, qty, type_, created_ts=ts)
        self.n_orders += 1
        self._send(PlaceOrder(cid, side, price, qty, type_), ts)
        return True

    def _act(self, ts: int) -> None:
        book = self.last_book
        if book is None or not book.valid:
            return
        self.risk.check_pnl(self.equity_rub(book.mid))

        state = self.schedule.state(ts)
        stale = not self.risk.book_is_fresh(ts, book.ts)
        if self.risk.killed or state is not SessionState.TRADE:
            quotes = Quotes(flatten=True)
        elif stale:
            quotes = Quotes()  # данных нет — снимаем котировки, но позицию не трогаем
        else:
            quotes = self.strategy.quotes(ts, self.position)
        self.last_quotes = quotes

        if quotes.flatten:
            self._flatten(ts, book)
            return

        if quotes.take_side is not None and not self._active(type_=OrderType.IOC):
            side = quotes.take_side
            px = book.best_ask if side is Side.BUY else book.best_bid
            self._place(side, px, quotes.take_qty, ts, OrderType.IOC)

        self._reconcile(Side.BUY, quotes.bid_px, quotes.bid_qty, ts)
        self._reconcile(Side.SELL, quotes.ask_px, quotes.ask_qty, ts)

    def _reconcile(self, side: Side, px: float | None, qty: int, ts: int) -> None:
        working = [o for o in self._active(side, OrderType.LIMIT) if o.status is not OrderStatus.PENDING_CANCEL]
        if px is None or qty <= 0:
            for o in working:
                self._cancel(o, ts)
            return
        interval_us = self.cfg.requote_min_interval_ms * 1000
        keep: Order | None = None
        for o in working:
            if keep is None and abs(o.price - px) < EPS:
                keep = o
                continue
            more_aggressive = px > o.price if side is Side.BUY else px < o.price
            if keep is None and more_aggressive and ts - self._last_side_action[side] < interval_us:
                keep = o  # не дёргаем заявку слишком часто, текущая цена безопаснее целевой
                continue
            self._cancel(o, ts)
            self._last_side_action[side] = ts
        if keep is None:
            if self._place(side, px, qty, ts):
                self._last_side_action[side] = ts

    def _flatten(self, ts: int, book: OrderBook) -> None:
        for o in self._active(type_=OrderType.LIMIT):
            self._cancel(o, ts)
        if self.position == 0 or self._active(type_=OrderType.IOC):
            return
        tick = self.inst.tick_size
        slip = self.cfg.flatten_slippage_ticks * tick
        qty = min(abs(self.position), self.risk.cfg.max_order_qty)
        if self.position > 0:
            self._place(Side.SELL, book.best_bid - slip, qty, ts, OrderType.IOC)
        else:
            self._place(Side.BUY, book.best_ask + slip, qty, ts, OrderType.IOC)
