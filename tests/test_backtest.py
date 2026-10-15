import dataclasses
from datetime import datetime

from hft.backtest.data import merge_events, read_books, read_trades, write_books, write_trades
from hft.backtest.run import run_backtest
from hft.backtest.synthetic import generate
from hft.instruments import SI
from hft.risk import RiskConfig
from hft.sessions import MSK, SessionState, TradingSchedule
from hft.strategy import MarketMakerConfig
from hft.types import OrderBook

OPEN = TradingSchedule(always_open=True)


def test_position_limit_respected_and_deterministic():
    ev = generate(SI, seconds=300, seed=3)
    cfg = MarketMakerConfig(max_position=2, quote_qty=1)
    res1, m1 = run_backtest(SI, ev, cfg, schedule=OPEN)
    res2, m2 = run_backtest(SI, ev, cfg, schedule=OPEN)
    assert m1 == m2
    assert m1.contracts_traded > 0
    assert max(abs(p) for p in res1.position) <= 2


def test_alpha_beats_naive_on_synthetic():
    ev = generate(SI, seconds=900, seed=11)
    naive = MarketMakerConfig(k_micro=0, k_obi=0, k_flow=0)
    _, m_naive = run_backtest(SI, ev, naive, schedule=OPEN)
    _, m_alpha = run_backtest(SI, ev, MarketMakerConfig(), schedule=OPEN)
    assert m_alpha.pnl_rub > m_naive.pnl_rub


def test_kill_switch_flattens():
    ev = generate(SI, seconds=600, seed=5)
    expensive = dataclasses.replace(SI, maker_fee_rub=50.0)  # гарантированный убыток
    cfg = MarketMakerConfig(min_edge_ticks=-100)
    res, m = run_backtest(expensive, ev, cfg, risk=RiskConfig(max_daily_loss_rub=300), schedule=OPEN)
    assert m.kill_reason
    assert m.final_position == 0


def test_flatten_before_clearing():
    sched = TradingSchedule()
    ts = lambda h, mi: int(datetime(2024, 3, 1, h, mi, tzinfo=MSK).timestamp() * 1e6)
    assert sched.state(ts(12, 0)) is SessionState.TRADE
    assert sched.state(ts(13, 58)) is SessionState.FLATTEN
    assert sched.state(ts(14, 2)) is SessionState.CLOSED
    assert sched.state(ts(9, 0)) is SessionState.CLOSED


def test_csv_roundtrip(tmp_path):
    ev = generate(SI, seconds=5, seed=2)
    books = [e for e in ev if isinstance(e, OrderBook)]
    trades = [e for e in ev if not isinstance(e, OrderBook)]
    write_books(tmp_path / "b.csv", books)
    write_trades(tmp_path / "t.csv", trades)
    merged = list(merge_events(read_books(tmp_path / "b.csv"), read_trades(tmp_path / "t.csv")))
    assert len(merged) == len(ev)
    assert [e.ts for e in merged] == sorted(e.ts for e in merged)
    merged_books = [e for e in merged if isinstance(e, OrderBook)]
    assert merged_books[-1].bids == books[-1].bids and merged_books[-1].asks == books[-1].asks
