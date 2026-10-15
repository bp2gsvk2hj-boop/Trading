from hft.instruments import RTS, SI
from hft.signals import TradeFlow, book_imbalance, microprice
from hft.types import OrderBook, Side, Trade


def test_imbalance_and_microprice():
    b = OrderBook(0, [(100.0, 30)], [(101.0, 10)])
    assert book_imbalance(b, levels=1) == 0.5
    # больше объём на покупке => microprice ближе к аску
    assert microprice(b) == 100.0 + 30 / 40
    assert book_imbalance(OrderBook(0, [(100.0, 10)], [(101.0, 10)])) == 0.0


def test_trade_flow_decays_and_signs():
    f = TradeFlow(tau_sec=1.0)
    f.on_trade(Trade(0, 100.0, 5, Side.BUY))
    f.on_trade(Trade(0, 100.0, 5, Side.SELL))
    assert abs(f.value(0)) < 1e-12
    f.on_trade(Trade(1_000_000, 100.0, 10, Side.BUY))
    assert f.value(1_000_000) > 0.5


def test_price_rounding_and_fees():
    assert RTS.floor_price(105_014.0) == 105_010.0
    assert RTS.ceil_price(105_011.0) == 105_020.0
    assert RTS.round_price(105_015.1) == 105_020.0
    assert SI.fee_rub(92_000, 3, maker=True) == 3 * SI.maker_fee_rub
    assert RTS.rub_per_point == RTS.tick_value / 10
