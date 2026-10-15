"""Событийный бэктестер с моделью очереди и задержек.

Модель исполнения:
- Команды трейдера доходят до биржи через `latency_us`, ответы биржи возвращаются
  через столько же.
- Пассивная заявка встаёт в конец очереди: впереди неё весь видимый объём на её цене.
  Объём впереди уменьшается сделками по этой цене и сокращением уровня в стакане.
- Заявка исполняется, когда объём сделок по её цене превысил очередь впереди,
  когда сделка прошла «сквозь» её цену, или когда стакан пересёк её цену.
- Агрессивные заявки (IOC и пересекающие спред) исполняются по видимому стакану.
Модель консервативна к очереди, но не учитывает влияние наших заявок на других
участников — для крупных объёмов результат будет оптимистичным.
"""
from __future__ import annotations

import heapq
import itertools
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from ..instruments import Instrument
from ..types import CancelOrder, Command, Fill, OrderBook, OrderType, PlaceOrder, Side, Trade

EPS = 1e-9


class Scheduler:
    def __init__(self) -> None:
        self.now = 0
        self._heap: list[tuple[int, int, Callable[[], None]]] = []
        self._seq = itertools.count()

    def at(self, ts: int, fn: Callable[[], None]) -> None:
        heapq.heappush(self._heap, (ts, next(self._seq), fn))

    def run_until(self, ts: int) -> None:
        while self._heap and self._heap[0][0] <= ts:
            t, _, fn = heapq.heappop(self._heap)
            self.now = max(self.now, t)
            fn()
        self.now = max(self.now, ts)

    def drain(self) -> None:
        while self._heap:
            t, _, fn = heapq.heappop(self._heap)
            self.now = max(self.now, t)
            fn()


@dataclass(slots=True)
class SimOrder:
    client_id: str
    side: Side
    price: float
    remaining: int
    queue_ahead: int


class SimulatedExchange:
    """Реализует протокол `Venue` поверх исторических данных."""

    def __init__(self, instrument: Instrument, scheduler: Scheduler, latency_us: int = 1_000):
        self.inst = instrument
        self.sched = scheduler
        self.latency_us = latency_us
        self.book: OrderBook | None = None
        self.resting: dict[str, SimOrder] = {}
        self.trader = None  # устанавливается бэктестером

    # ----------------------------------------------------------- Venue
    def send(self, cmd: Command) -> None:
        self.sched.at(self.sched.now + self.latency_us, lambda: self._arrive(cmd))

    # ------------------------------------------------------- внутреннее
    def _reply(self, fn: Callable[[], None]) -> None:
        self.sched.at(self.sched.now + self.latency_us, fn)

    def _fill(self, cid: str, side: Side, price: float, qty: int, maker: bool) -> None:
        ts = self.sched.now + self.latency_us
        fee = self.inst.fee_rub(price, qty, maker)
        fill = Fill(ts, cid, side, price, qty, fee, maker)
        self._reply(lambda: self.trader.on_fill(fill))

    def _arrive(self, cmd: Command) -> None:
        if isinstance(cmd, PlaceOrder):
            self._arrive_place(cmd)
        elif isinstance(cmd, CancelOrder):
            self._arrive_cancel(cmd)

    def _arrive_place(self, cmd: PlaceOrder) -> None:
        cid = cmd.client_id
        now = self.sched.now
        self._reply(lambda: self.trader.on_ack(cid, now + self.latency_us))
        remaining = cmd.qty
        book = self.book
        if book is not None and book.valid:
            remaining = self._take(cmd, book)
        if remaining <= 0:
            return
        if cmd.type is OrderType.IOC or book is None:
            self._reply(lambda: self.trader.on_cancelled(cid, now + self.latency_us))
            return
        same = book.bids if cmd.side is Side.BUY else book.asks
        best_same = same[0][0] if same else None
        better = best_same is None or (
            cmd.price > best_same + EPS if cmd.side is Side.BUY else cmd.price < best_same - EPS
        )
        queue = 0 if better else book.qty_at(cmd.side, cmd.price)
        self.resting[cid] = SimOrder(cid, cmd.side, cmd.price, remaining, queue)

    def _take(self, cmd: PlaceOrder, book: OrderBook) -> int:
        """Агрессивное исполнение против видимого стакана. Съеденный объём убираем из снимка."""
        remaining = cmd.qty
        levels = book.asks if cmd.side is Side.BUY else book.bids
        new_levels = []
        for i, (p, q) in enumerate(levels):
            crosses = p <= cmd.price + EPS if cmd.side is Side.BUY else p >= cmd.price - EPS
            if remaining <= 0 or not crosses:
                new_levels.extend(levels[i:])
                break
            take = min(q, remaining)
            self._fill(cmd.client_id, cmd.side, p, take, maker=False)
            remaining -= take
            if q - take > 0:
                new_levels.append((p, q - take))
        if cmd.side is Side.BUY:
            book.asks = new_levels
        else:
            book.bids = new_levels
        return remaining

    def _arrive_cancel(self, cmd: CancelOrder) -> None:
        cid = cmd.client_id
        now = self.sched.now
        if self.resting.pop(cid, None) is not None:
            self._reply(lambda: self.trader.on_cancelled(cid, now + self.latency_us))
        else:
            self._reply(lambda: self.trader.on_reject(cid, now + self.latency_us, "order not found"))

    # ---------------------------------------------------- рыночные данные
    def on_book(self, book: OrderBook) -> None:
        self.book = OrderBook(book.ts, list(book.bids), list(book.asks))
        if not self.book.valid:
            return
        for cid, o in list(self.resting.items()):
            crossed = (
                o.price >= self.book.best_ask - EPS if o.side is Side.BUY else o.price <= self.book.best_bid + EPS
            )
            if crossed:
                self._fill(cid, o.side, o.price, o.remaining, maker=True)
                del self.resting[cid]
            else:
                o.queue_ahead = min(o.queue_ahead, self.book.qty_at(o.side, o.price))

    def on_trade(self, trade: Trade) -> None:
        passive = trade.aggressor.opposite
        volume = trade.qty
        for cid, o in list(self.resting.items()):
            if o.side is not passive:
                continue
            through = trade.price < o.price - EPS if o.side is Side.BUY else trade.price > o.price + EPS
            if through:
                self._fill(cid, o.side, o.price, o.remaining, maker=True)
                del self.resting[cid]
                continue
            if abs(trade.price - o.price) > EPS:
                continue
            eaten = min(o.queue_ahead, volume)
            o.queue_ahead -= eaten
            left = volume - eaten
            if left > 0:
                qty = min(o.remaining, left)
                self._fill(cid, o.side, o.price, qty, maker=True)
                o.remaining -= qty
                if o.remaining <= 0:
                    del self.resting[cid]


@dataclass
class BacktestResult:
    equity_ts: list[int] = field(default_factory=list)
    equity_rub: list[float] = field(default_factory=list)
    position: list[int] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    n_orders: int = 0
    n_commands: int = 0
    fees_rub: float = 0.0
    final_position: int = 0
    final_equity_rub: float = 0.0
    kill_reason: str = ""


class Backtester:
    def __init__(self, instrument: Instrument, trader_factory, latency_us: int = 1_000, sample_ms: int = 1_000):
        """`trader_factory(venue) -> Trader`."""
        self.inst = instrument
        self.sched = Scheduler()
        self.exchange = SimulatedExchange(instrument, self.sched, latency_us)
        self.trader = trader_factory(self.exchange)
        self.exchange.trader = self.trader
        self.sample_us = sample_ms * 1000

    def run(self, events: Iterable[OrderBook | Trade]) -> BacktestResult:
        res = BacktestResult()
        next_sample = None
        for ev in events:
            self.sched.run_until(ev.ts)
            if isinstance(ev, OrderBook):
                self.exchange.on_book(ev)
                self.trader.on_book(ev)
            else:
                self.exchange.on_trade(ev)
                self.trader.on_trade(ev)
            if next_sample is None:
                next_sample = ev.ts
            if ev.ts >= next_sample:
                res.equity_ts.append(ev.ts)
                res.equity_rub.append(self.trader.equity_rub())
                res.position.append(self.trader.position)
                next_sample = ev.ts + self.sample_us
        self.sched.drain()
        t = self.trader
        res.fills = t.fills
        res.n_orders = t.n_orders
        res.n_commands = t.n_commands
        res.fees_rub = t.fees_rub
        res.final_position = t.position
        res.final_equity_rub = t.equity_rub()
        res.kill_reason = t.risk.kill_reason
        return res
