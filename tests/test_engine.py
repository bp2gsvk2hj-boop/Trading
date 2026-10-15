"""Проверки симулятора биржи: очередь, сквозные сделки, снятия, IOC."""
from hft.backtest.engine import Scheduler, SimulatedExchange
from hft.instruments import SI
from hft.types import CancelOrder, OrderBook, OrderType, PlaceOrder, Side, Trade


class Recorder:
    def __init__(self):
        self.fills, self.acks, self.cancels, self.rejects = [], [], [], []

    def on_fill(self, f):
        self.fills.append(f)

    def on_ack(self, cid, ts):
        self.acks.append(cid)

    def on_cancelled(self, cid, ts):
        self.cancels.append(cid)

    def on_reject(self, cid, ts, reason=""):
        self.rejects.append(cid)


def make(latency=100):
    s = Scheduler()
    ex = SimulatedExchange(SI, s, latency_us=latency)
    r = Recorder()
    ex.trader = r
    ex.on_book(OrderBook(0, [(100.0, 10), (99.0, 20)], [(101.0, 10), (102.0, 20)]))
    return s, ex, r


def test_queue_position():
    s, ex, r = make()
    ex.send(PlaceOrder("a", Side.BUY, 100.0, 2))
    s.run_until(200)
    assert ex.resting["a"].queue_ahead == 10
    ex.on_trade(Trade(300, 100.0, 6, Side.SELL))
    s.drain()
    assert r.fills == []
    ex.on_trade(Trade(400, 100.0, 5, Side.SELL))  # 4 съедают очередь, 1 — нам
    s.drain()
    assert sum(f.qty for f in r.fills) == 1 and r.fills[0].maker
    assert ex.resting["a"].remaining == 1


def test_queue_shrinks_with_book_and_trade_through_fills():
    s, ex, r = make()
    ex.send(PlaceOrder("a", Side.BUY, 100.0, 1))
    s.run_until(200)
    ex.on_book(OrderBook(300, [(100.0, 3)], [(101.0, 10)]))
    assert ex.resting["a"].queue_ahead == 3
    ex.on_trade(Trade(400, 99.0, 1, Side.SELL))  # сделка ниже нашей цены
    s.drain()
    assert len(r.fills) == 1 and r.fills[0].price == 100.0
    assert "a" not in ex.resting


def test_cancel_and_cancel_reject():
    s, ex, r = make()
    ex.send(PlaceOrder("a", Side.SELL, 101.0, 1))
    s.run_until(150)
    ex.send(CancelOrder("a"))
    ex.send(CancelOrder("zzz"))
    s.drain()
    assert r.cancels == ["a"] and r.rejects == ["zzz"]


def test_ioc_takes_liquidity_and_cancels_rest():
    s, ex, r = make()
    ex.send(PlaceOrder("x", Side.BUY, 102.0, 35, OrderType.IOC))
    s.drain()
    assert [(f.price, f.qty, f.maker) for f in r.fills] == [(101.0, 10, False), (102.0, 20, False)]
    assert r.cancels == ["x"]


def test_improving_order_has_empty_queue():
    s, ex, r = make()
    ex.on_book(OrderBook(0, [(100.0, 10)], [(102.0, 10)]))
    ex.send(PlaceOrder("a", Side.BUY, 101.0, 1))
    s.run_until(200)
    assert ex.resting["a"].queue_ahead == 0
    ex.on_trade(Trade(300, 101.0, 1, Side.SELL))
    s.drain()
    assert len(r.fills) == 1
