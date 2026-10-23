"""Offline tests: python -m unittest discover -s tests"""
from __future__ import annotations

import json
import os
import unittest.mock
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from robot import backtest
from robot.api import f2q, q2f
from robot.config import Config, Slot
from robot.engine import Engine, Halted
from robot.market import Candle, Instrument
from robot.risk import RiskConfig, Sessions, size
from robot.strategies import Position, make

INST = Instrument("TEST", "SPBFUT", "uid-test", "Test", "T", step=0.01, step_rub=1.0, lot=1,
                  go_buy=100.0, go_sell=100.0, expiration="2099-12-31")   # 1 point = 100 RUB
NOW = datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc)                  # Wednesday 12:00 MSK
H = timedelta(hours=1)
PARAMS = {"entry_n": 5, "exit_n": 3, "atr_n": 3, "stop_atr": 2.0, "trend_ema": 0}


def bar(t, o, h, l, c):
    return Candle(t, o, h, l, c, 100)


def flat_then_breakout(n=15, end=NOW):
    """n-1 quiet hourly bars around 100, then a breakout bar closing at 102 at `end`."""
    bars = []
    for k in range(n - 1):
        t = end - (n - k) * H
        bars.append(bar(t, 100, 100.3, 99.7, 100 + (0.1 if k % 2 else -0.1)))
    bars.append(bar(end - H, 100, 102.1, 99.9, 102))
    return bars


class FakeClient:
    def __init__(self, bars):
        self.bars = bars
        self.open = True

    def call(self, method, body=None):
        if method == "MarketDataService/GetCandles":
            return {"candles": [{"time": b.t.strftime("%Y-%m-%dT%H:%M:%SZ"), "open": f2q(b.o), "high": f2q(b.h),
                                 "low": f2q(b.l), "close": f2q(b.c), "volume": "1", "isComplete": True}
                                for b in self.bars]}
        if method == "MarketDataService/GetTradingStatus":
            return {"tradingStatus": "SECURITY_TRADING_STATUS_NORMAL_TRADING" if self.open else "SECURITY_TRADING_STATUS_BREAK_IN_TRADING",
                    "apiTradeAvailableFlag": True, "marketOrderAvailableFlag": True}
        if method == "MarketDataService/GetLastPrices":
            return {"lastPrices": [{"price": f2q(self.bars[-1].c)}]}
        raise AssertionError(f"unexpected call {method}")


def make_engine(tmp, bars, mode="paper", risk=None):
    cfg = Config(mode=mode, account_id="", state_dir=Path(tmp), risk=risk or RiskConfig(capital_rub=50_000, max_risk_per_trade=0.05),
                 sessions=Sessions(), slots=[Slot("TEST", "trend", "1h", PARAMS)], path=Path(tmp) / "robot.toml")
    eng = Engine(cfg, client=FakeClient(bars))
    eng._insts["TEST"] = INST
    return eng


class QuotationTest(unittest.TestCase):
    def test_roundtrip(self):
        for x in (0.0, 1.5, 12.467, -3.25, 4831.26, 0.000001):
            self.assertAlmostEqual(q2f(f2q(x)), x, places=9)
        self.assertEqual(f2q(-3.25), {"units": "-3", "nano": -250000000})


class RiskTest(unittest.TestCase):
    def test_size_by_risk_and_margin(self):
        cfg = RiskConfig(capital_rub=5000, risk_per_trade=0.01, max_risk_per_trade=0.03, max_margin_share=0.6, fee_rate=0)
        # 0.2 points * 100 RUB = 20 RUB per contract -> 50 RUB risk allows 2
        self.assertEqual(size(INST, 100.0, 99.8, cfg)[0], 2)
        # 1.0 point = 100 RUB > 1% but < 3% cap -> 1 contract
        self.assertEqual(size(INST, 100.0, 99.0, cfg)[0], 1)
        # 2.0 points = 200 RUB > 150 RUB cap -> skip
        self.assertEqual(size(INST, 100.0, 98.0, cfg)[0], 0)
        # margin: 3000 allowed, 2950 used -> 0 contracts of GO 100
        self.assertEqual(size(INST, 100.0, 99.8, cfg, margin_in_use=2950)[0], 0)

    def test_sessions(self):
        s = Sessions()
        msk = timezone(timedelta(hours=3))
        self.assertTrue(s.can_enter(datetime(2026, 10, 14, 12, 0, tzinfo=msk)))
        self.assertFalse(s.can_enter(datetime(2026, 10, 14, 13, 55, tzinfo=msk)))   # clearing
        self.assertFalse(s.can_enter(datetime(2026, 10, 14, 9, 30, tzinfo=msk)))
        self.assertFalse(s.can_enter(datetime(2026, 10, 17, 12, 0, tzinfo=msk)))    # Saturday
        self.assertTrue(s.must_flatten(datetime(2026, 10, 14, 18, 31, tzinfo=msk)))


class StrategyTest(unittest.TestCase):
    def test_trend_breakout_and_trail(self):
        bars = flat_then_breakout()
        s = make("trend", "1h", PARAMS)
        ind = s.prepare(bars)
        a = s.decide(len(bars) - 1, ind, Position(), {})
        self.assertEqual((a.kind, a.side), ("enter", 1))
        self.assertLess(a.stop, 102)
        # long with a low stop: trailing channel lifts it
        a2 = s.decide(len(bars) - 1, ind, Position(1, 1, 102, 99.0), {})
        self.assertEqual(a2.kind, "stop")
        self.assertGreater(a2.stop, 99.0)

    def test_no_lookahead(self):
        """Changing a future bar must not change a past decision."""
        bars = flat_then_breakout()
        s = make("trend", "1h", PARAMS)
        i = len(bars) - 2
        before = s.decide(i, s.prepare(bars), Position(), {})
        bars2 = bars[:-1] + [bar(bars[-1].t, 100, 150, 50, 140)]
        after = s.decide(i, s.prepare(bars2), Position(), {})
        self.assertEqual(before, after)


class BacktestTest(unittest.TestCase):
    def test_fill_next_open_and_gap_stop(self):
        bars = flat_then_breakout(end=NOW)
        # next bar opens at 102.5 (entry), then a gap down through any stop
        bars.append(bar(NOW, 102.5, 103, 102.2, 102.8))
        bars.append(bar(NOW + H, 90, 90.5, 89, 90))
        bars.append(bar(NOW + 2 * H, 90, 90, 90, 90))
        risk = RiskConfig(capital_rub=50_000, fee_rate=0.0, slippage_ticks=0, max_risk_per_trade=1.0)
        res = backtest.run(make("trend", "1h", PARAMS), INST, bars, risk)
        self.assertEqual(len(res.trades), 1)
        t = res.trades[0]
        self.assertEqual(t.entry, 102.5)          # next bar open, not the signal close
        self.assertEqual(t.exit, 90)              # gapped through the stop: filled at the open
        self.assertEqual(t.reason, "stop")
        self.assertAlmostEqual(t.pnl, (90 - 102.5) * 100 * t.qty)


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_paper_lifecycle(self):
        bars = flat_then_breakout()
        eng = make_engine(self.tmp.name, bars)
        notes = eng.run_once(NOW)
        self.assertTrue(any("[entered]" in n for n in notes), notes)
        h = eng.broker.holding(INST)
        self.assertGreater(h.qty, 0)
        stops = eng.broker.stops(INST)
        self.assertEqual(len(stops), 1)
        self.assertEqual((stops[0].side, stops[0].qty), (-1, h.qty))
        first_stop = stops[0].price

        # same bar again: nothing new happens
        eng2 = make_engine(self.tmp.name, bars)
        self.assertEqual(eng2.run_once(NOW + timedelta(minutes=5)), [])
        self.assertEqual(eng2.broker.holding(INST).qty, h.qty)

        # new higher bars: trailing stop moves up, still one stop
        bars += [bar(NOW, 102, 103.5, 101.9, 103.4), bar(NOW + H, 103.4, 104.6, 103.3, 104.5)]
        eng3 = make_engine(self.tmp.name, bars)
        eng3.run_once(NOW + 2 * H)
        stops = eng3.broker.stops(INST)
        self.assertEqual(len(stops), 1)
        self.assertGreater(stops[0].price, first_stop)

        # crash through the stop: paper stop fills, position flat, no orphan stop
        bars.append(bar(NOW + 2 * H, 104.5, 104.5, 95, 96))
        eng4 = make_engine(self.tmp.name, bars)
        notes = eng4.run_once(NOW + 3 * H)
        self.assertTrue(any("paper_fill" in n for n in notes), notes)
        self.assertTrue(any("closed_outside" in n for n in notes), notes)
        # the crash bar is also a breakdown: the robot reverses short, with a buy stop above
        h = eng4.broker.holding(INST)
        self.assertLess(h.qty, 0)
        stops = eng4.broker.stops(INST)
        self.assertEqual([(s.side, s.qty) for s in stops], [(1, -h.qty)])
        self.assertGreater(stops[0].price, 96)

    def test_stopout_limit_blocks_reentry(self):
        bars = flat_then_breakout()
        risk = RiskConfig(capital_rub=50_000, max_risk_per_trade=0.05, max_stopouts_per_day=1)
        make_engine(self.tmp.name, bars, risk=risk).run_once(NOW)
        bars.append(bar(NOW, 102, 102, 95, 96))      # stop-out and a breakdown in the same bar
        eng = make_engine(self.tmp.name, bars, risk=risk)
        notes = eng.run_once(NOW + H)
        self.assertEqual(eng.broker.holding(INST).qty, 0, notes)
        self.assertTrue(any("stop-outs today" in n for n in notes), notes)

    def test_restores_missing_stop_and_cancels_orphans(self):
        bars = flat_then_breakout()
        eng = make_engine(self.tmp.name, bars)
        eng.run_once(NOW)
        for s in eng.broker.stops(INST):
            eng.broker.cancel_stop(s.id)          # stop lost somehow
        eng2 = make_engine(self.tmp.name, bars)
        notes = eng2.run_once(NOW + timedelta(minutes=5))
        self.assertEqual(len(eng2.broker.stops(INST)), 1, notes)

        # flat with a leftover stop -> cancelled
        eng2.broker.s["pos"].clear(); eng2.broker.save()
        eng3 = make_engine(self.tmp.name, bars)
        eng3.run_once(NOW + timedelta(minutes=10))
        self.assertEqual(eng3.broker.stops(INST), [])

    def test_no_entry_when_market_closed_or_halted(self):
        bars = flat_then_breakout()
        eng = make_engine(self.tmp.name, bars)
        eng.c.open = False
        notes = eng.run_once(NOW)
        self.assertEqual(eng.broker.holding(INST).qty, 0)
        self.assertTrue(any("signal_skipped" in n for n in notes), notes)

        (Path(self.tmp.name) / "HALT").write_text("test")
        eng2 = make_engine(self.tmp.name, flat_then_breakout(end=NOW + H))
        notes = eng2.run_once(NOW + H)
        self.assertTrue(any("[halted]" in n for n in notes), notes)
        self.assertEqual(eng2.broker.holding(INST).qty, 0)

    def test_dry_run_sends_nothing(self):
        bars = flat_then_breakout()
        cfg_eng = make_engine(self.tmp.name, bars)
        cfg_eng.dry = True
        notes = cfg_eng.run_once(NOW)
        self.assertTrue(any("would_market" in n for n in notes), notes)
        self.assertEqual(cfg_eng.broker.holding(INST).qty, 0)

    def test_live_requires_explicit_opt_in(self):
        os.environ.pop("ROBOT_LIVE", None)
        cfg = Config(mode="live", account_id="123", state_dir=Path(self.tmp.name), risk=RiskConfig(),
                     sessions=Sessions(), slots=[], path=Path(self.tmp.name) / "robot.toml")
        with self.assertRaises(Halted):
            Engine(cfg, client=FakeClient([]))
        Engine(cfg, client=FakeClient([]), read_only=True)   # status needs no opt-in


if __name__ == "__main__":
    unittest.main()


class TelegramTest(unittest.TestCase):
    STATUS = {"free_money": 4595.21, "slots": [
        {"ticker": "CNYRUBF", "qty": -1, "avg": 12.449, "last": 12.445, "unrealized_rub": 4.0,
         "stops": [[1, 1, 12.532]]},
        {"ticker": "GLDRUBF", "qty": 0, "avg": 0.0, "last": 11149.5, "unrealized_rub": 0.0, "stops": []}]}

    def test_quiet_pass_sends_nothing(self):
        from robot.telegram import build
        self.assertIsNone(build("[skip] ticker=X, why=only 3 bars\n", self.STATUS, 0, False))
        self.assertIsNone(build("[entered] ticker=CNYRUBF, side=-1, qty=1\n", self.STATUS, 0, False))

    def test_summary(self):
        from robot.telegram import build
        msg = build("", self.STATUS, 0, True)
        self.assertIn("Итог дня", msg)
        self.assertIn("CNYRUBF: шорт 1 @ 12.449", msg)
        self.assertIn("стоп 12.532", msg)

    def test_trades(self):
        from robot.telegram import trade_messages
        t = datetime(2026, 10, 19, 8, 0, tzinfo=timezone.utc)
        tr = lambda side, qty, px, stop=False, k=0: {"ticker": "CNYRUBF", "t": t + timedelta(hours=k), "side": side,
                                                     "qty": qty, "price": px, "point_value": 1000.0, "by_stop": stop}
        pos = {}
        msgs = trade_messages([tr(-1, 1, 12.449), tr(1, 1, 12.532, stop=True, k=1)], pos, {"CNYRUBF": "breakout"})
        self.assertIn("Открыта позиция CNYRUBF: шорт 1 по 12.449 (11:00 МСК)", msgs[0])
        self.assertIn("Причина: breakout", msgs[0])
        self.assertIn("Закрыта позиция по стопу CNYRUBF", msgs[1])
        self.assertIn("результат -83 ₽", msgs[1])
        self.assertEqual(pos["CNYRUBF"]["qty"], 0)
        # Flip from long 1 to short 1 in one trade: close with P&L, then open.
        pos = {"CNYRUBF": {"qty": 1, "avg": 12.0}}
        msgs = trade_messages([tr(-1, 2, 12.1)], pos, {})
        self.assertIn("результат +100 ₽", msgs[0])
        self.assertIn("Открыта позиция CNYRUBF: шорт 1", msgs[1])
        self.assertEqual(pos["CNYRUBF"], {"qty": -1, "avg": 12.1})

    def test_orders(self):
        from robot.telegram import order_messages, parse_run
        log = ("[stop_cancelled] ticker=CNYRUBF, why=trail\n"
               "[stop_placed] ticker=CNYRUBF, side=1, qty=1, price=12.5\n"
               "[stop_cancelled] ticker=GLDRUBF, why=orphan stop, no position\n"
               "[stop_cancelled] ticker=X, why=exit\n")
        msgs = order_messages(parse_run(log))
        self.assertEqual(msgs, ["🛡 Стоп передвинут CNYRUBF: покупка 1 по 12.5",
                                "✖️ Стоп-заявка снята GLDRUBF: orphan stop, no position"])

    def test_alerts(self):
        from robot.telegram import build
        self.assertIn("ТРЕБУЕТСЯ ВНИМАНИЕ", build("[error] ticker=X, error=boom\n", self.STATUS, 0, False))
        self.assertIn("кодом 1", build("Traceback ...\n", self.STATUS, 1, False))
        self.assertIn("Статус получить не удалось", build("", None, 0, False))
        self.assertIn("сделки", build("", self.STATUS, 0, False, problems=["Не удалось получить сделки"]))
        naked = {"slots": [{"ticker": "X", "qty": 1, "avg": 1, "last": 1, "unrealized_rub": 0, "stops": []}]}
        self.assertIn("НЕТ СТОПА", build("", naked, 0, False))


class SignalsTest(unittest.TestCase):
    def test_virtual_trades_become_readable_signals(self):
        from robot.signals import build_messages
        from robot.telegram import parse_run
        book = {}
        pv = lambda t: INST.point_value
        with tempfile.TemporaryDirectory() as tmp:
            bars = flat_then_breakout()
            msgs = build_messages(parse_run("\n".join(make_engine(tmp, bars).run_once(NOW))), book, pv, "счёт 5 млн ₽")
            self.assertEqual(len(msgs), 1, msgs)
            self.assertIn("🟢 Открыть ЛОНГ TEST", msgs[0])
            self.assertIn("Стоп-лосс: продажа", msgs[0])
            self.assertIn("пробой максимума 20 часов", msgs[0])
            self.assertRegex(msgs[0], r"Риск до стопа ≈ [\d ]+ ₽, ГО ≈ [\d ]+ ₽")
            entry, qty = book["TEST"]["entry"], book["TEST"]["qty"]

            bars += [bar(NOW, 102, 103.5, 101.9, 103.4), bar(NOW + H, 103.4, 104.6, 103.3, 104.5)]
            msgs = build_messages(parse_run("\n".join(make_engine(tmp, bars).run_once(NOW + 2 * H))), book, pv, "x")
            self.assertEqual(len(msgs), 1, msgs)
            self.assertIn("🛡 Передвинуть стоп TEST: продажа", msgs[0])
            self.assertIn("(было", msgs[0])

            bars.append(bar(NOW + 2 * H, 104.5, 104.5, 95, 96))
            msgs = build_messages(parse_run("\n".join(make_engine(tmp, bars).run_once(NOW + 3 * H))), book, pv, "x")
            self.assertIn("⛔ Сработал стоп TEST", msgs[0])
            self.assertIn(f"вход {entry:g}", msgs[0])
            # the crash bar is also a breakdown: a new short signal follows
            self.assertIn("🟢 Открыть ШОРТ TEST", msgs[1])
            self.assertEqual(book["TEST"]["side"], -1)

    def test_strategy_exit_reports_result(self):
        from robot.signals import build_messages
        book = {"CNYRUBF": {"side": -1, "qty": 1000, "entry": 12.5, "stop": 12.6}}
        ev = [("stop_cancelled", "ticker=CNYRUBF, why=exit"),
              ("market", "ticker=CNYRUBF, side=1, qty=1000, price=12.4, why=trail stop hit at close")]
        msgs = build_messages(ev, book, lambda t: 1000.0, "x")
        self.assertIn("🔴 Закрыть ШОРТ CNYRUBF: 1 000 контр.", msgs[0])
        self.assertIn("+100 000 ₽", msgs[0])        # (12.4 - 12.5) * -1 * 1000 * 1000
        self.assertEqual(book, {})

    def test_summary_suggests_money_fund(self):
        from robot.signals import summary
        st = {"free_money": 5_000_000, "slots": [{"ticker": "CNYRUBF", "qty": -500, "avg": 12.1, "last": 12.0,
                                                  "unrealized_rub": 50_000, "stops": [[1, 500, 12.2]],
                                                  "go_per_contract": 1000}]}
        msg = summary(st, 5_000_000, "Торговые сигналы Сбер")
        self.assertIn("ШОРТ 500", msg)
        self.assertIn("+50 000 ₽ с начала", msg)
        # 5.05M - 2*0.5M margin - 10% = 3.545M
        self.assertIn("фонде денежного рынка можно держать ≈ 3 545 000 ₽", msg)
class ContinuousTest(unittest.TestCase):
    def test_panama_adjustment_removes_roll_gap(self):
        from robot import continuous
        t0 = datetime(2026, 1, 5, 7, 0, tzinfo=timezone.utc)
        # two contracts: old trades at 100, new one at 110 (gap +10) and keeps rising
        old = [bar(t0 + k * H, 100, 100.5, 99.5, 100) for k in range(48)]
        new = [bar(t0 + k * H, 110 + k * 0.1, 111 + k * 0.1, 109 + k * 0.1, 110 + k * 0.1) for k in range(24 * 40)]
        futs = [{"basicAsset": "X", "ticker": "XXH6", "uid": "old", "expirationDate": "2026-01-08T00:00:00Z"},
                {"basicAsset": "X", "ticker": "XXM6", "uid": "new", "expirationDate": "2026-12-01T00:00:00Z"}]

        class C:
            def call(self, method, body=None):
                if method == "InstrumentsService/Futures":
                    return {"instruments": futs}
                src = old if body["instrumentId"] == "old" else new
                lo = datetime.fromisoformat(body["from"].replace("Z", "+00:00"))
                hi = datetime.fromisoformat(body["to"].replace("Z", "+00:00"))
                return FakeClient([b for b in src if lo <= b.t < hi]).call(method, body)

        with tempfile.TemporaryDirectory() as tmp:
            orig = continuous.DATA_DIR
            continuous.DATA_DIR = Path(tmp)
            try:
                now = t0 + timedelta(days=30)
                with unittest.mock.patch.object(continuous, "datetime", wraps=datetime) as dt:
                    dt.now.return_value = now
                    bars, rolls, shifts = continuous.build(C(), "X", "XX", "1h", days=30, roll_days=1)
            finally:
                continuous.DATA_DIR = orig
        self.assertEqual(len(rolls), 1)
        k = next(i for i, b in enumerate(bars) if b.t >= rolls[0])
        # no jump across the roll: old segment shifted up by the 10-point gap
        self.assertAlmostEqual(bars[k - 1].c, 110 + (k - 1) * 0.1, delta=0.5)
        self.assertAlmostEqual(bars[0].c - shifts[0], 100)     # real price recoverable
        self.assertEqual(shifts[-1], 0.0)                      # newest contract unchanged


class RollTest(unittest.TestCase):
    def test_position_moves_to_new_front_with_shifted_stop(self):
        from robot import continuous, engine as eng_mod
        old_i = Instrument("XXZ6", "SPBFUT", "uid-old", "old", "X", 0.01, 1.0, 1, 100.0, 100.0, "2026-12-18")
        new_i = Instrument("XXH7", "SPBFUT", "uid-new", "new", "X", 0.01, 1.0, 1, 100.0, 100.0, "2027-03-19")
        futs = [{"ticker": "XXZ6", "uid": "uid-old", "expirationDate": "2026-12-18T00:00:00Z"},
                {"ticker": "XXH7", "uid": "uid-new", "expirationDate": "2027-03-19T00:00:00Z"}]

        class C(FakeClient):
            def call(self, method, body=None):
                if method == "MarketDataService/GetLastPrices":
                    px = {"uid-old": 100.0, "uid-new": 103.0}
                    return {"lastPrices": [{"instrumentUid": u, "price": f2q(px[u])} for u in body["instrumentId"]]}
                return super().call(method, body)

        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(mode="paper", account_id="", state_dir=Path(tmp), risk=RiskConfig(capital_rub=50_000),
                         sessions=Sessions(), slots=[], path=Path(tmp) / "robot.toml")
            e = Engine(cfg, client=C([]))
            e._insts.update({"XXZ6": old_i, "XXH7": new_i})
            e.broker.s["pos"]["uid-old"] = {"qty": -2, "avg": 101.0, "ticker": "XXZ6"}
            e.broker.post_stop(old_i, 1, 2, 102.5, "s1")
            e.c.bars = [bar(NOW, 100, 100, 100, 100)]
            with unittest.mock.patch.object(continuous, "contracts", return_value=futs), \
                 unittest.mock.patch.object(e.broker, "_last", side_effect=lambda i: {"uid-old": 100.0, "uid-new": 103.0}[i.uid]):
                e._roll("X", "XX", new_i, "k", NOW)
            self.assertEqual(e.broker.holding(old_i).qty, 0)
            self.assertEqual(e.broker.holding(new_i).qty, -2)
            self.assertEqual(e.broker.stops(old_i), [])
            st = e.broker.stops(new_i)
            self.assertEqual([(s.side, s.qty) for s in st], [(1, 2)])
            self.assertAlmostEqual(st[0].price, 105.5)     # 102.5 + (103 - 100)


class TelegramTargetTest(unittest.TestCase):
    def test_own_bot_or_fallback_to_shared(self):
        from robot.config import TelegramCfg
        cfg = TelegramCfg("TELEGRAM_BOT_TOKEN_SBER", "TELEGRAM_CHAT_ID_SBER", "TELEGRAM_THREAD_ID_SBER")
        env = {"TELEGRAM_BOT_TOKEN": "shared", "TELEGRAM_CHAT_ID": "42", "TELEGRAM_BOT_TOKEN_SBER": "",
               "TELEGRAM_CHAT_ID_SBER": "", "TELEGRAM_THREAD_ID_SBER": ""}
        with unittest.mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(cfg.resolve(), ("shared", "42", None))      # empty secrets -> shared chat
        with unittest.mock.patch.dict(os.environ, {**env, "TELEGRAM_BOT_TOKEN_SBER": "sber"}, clear=True):
            self.assertEqual(cfg.resolve(), ("sber", "42", None))        # own bot, same person
        with unittest.mock.patch.dict(os.environ, {**env, "TELEGRAM_THREAD_ID_SBER": "7"}, clear=True):
            self.assertEqual(cfg.resolve(), ("shared", "42", "7"))       # topic in a group


class PaperCapitalTest(unittest.TestCase):
    def test_capital_change_is_a_deposit(self):
        from robot.broker import PaperBroker
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "paper.json"
            b = PaperBroker(FakeClient([]), path, 5_000_000)
            b.s["money"] += 12_345           # virtual profit
            b.s["pos"]["uid"] = {"qty": -3, "avg": 1.0, "ticker": "X"}
            b.save()
            b = PaperBroker(FakeClient([]), path, 6_000_000)
            self.assertEqual(b.free_money(), 6_012_345)
            self.assertEqual(b.s["pos"]["uid"]["qty"], -3)          # positions kept
            self.assertEqual(b.s["deposits"][-1]["amount"], 1_000_000)
            b.save()
            self.assertEqual(PaperBroker(FakeClient([]), path, 6_000_000).free_money(), 6_012_345)

    def test_legacy_state_without_capital(self):
        from robot.broker import PaperBroker
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "paper.json"
            path.write_text(json.dumps({"money": 5_041_000, "pos": {}, "stops": {}, "stopouts": [], "trades": []}))
            self.assertEqual(PaperBroker(FakeClient([]), path, 6_000_000).free_money(), 6_041_000)


class FeeTest(unittest.TestCase):
    def test_fixed_plus_percent_fee(self):
        from robot.risk import fee
        # 0.5 RUB per contract + 0.00462% of a 12 100 RUB contract, 100 contracts
        inst = Instrument("CNY", "SPBFUT", "u", "n", "a", 0.001, 1.0, 1, 1000.0, 1000.0, "2099-12-31")  # 1 pt = 1000 RUB
        self.assertAlmostEqual(fee(inst, 12.1, 100, 0.0000462, 0.5), 12_100 * 100 * 0.0000462 + 50)

    def test_paper_broker_uses_slot_rate_and_fixed_fee(self):
        from robot.broker import PaperBroker
        with tempfile.TemporaryDirectory() as tmp:
            b = PaperBroker(FakeClient([bar(NOW, 100, 100, 100, 100)]), Path(tmp) / "p.json", 1_000_000,
                            slippage_ticks=0, fee_rate=0.001, fee_per_contract=0.5)
            b.fee_rates[INST.uid] = 0.0002
            b.market(INST, 1, 10, "k")                 # 10 contracts at 100 pts x 100 RUB/pt
            self.assertAlmostEqual(1_000_000 - b.free_money(), 100 * 100 * 10 * 0.0002 + 5)


class CollateralTest(unittest.TestCase):
    def _engine(self, tmp, bars):
        from robot.config import CollateralCfg

        class C(FakeClient):
            def call(self, method, body=None):
                if method == "InstrumentsService/FindInstrument":
                    return {"instruments": [{"ticker": "LQDT", "classCode": "SPBRU", "uid": "fund"}]}
                return super().call(method, body)

        cfg = Config(mode="paper", account_id="", state_dir=Path(tmp),
                     risk=RiskConfig(capital_rub=1_000_000, max_risk_per_trade=0.05, max_contracts=10_000),
                     sessions=Sessions(), slots=[Slot("TEST", "trend", "1h", PARAMS)], path=Path(tmp) / "robot.toml",
                     collateral=CollateralCfg(enabled=True))
        e = Engine(cfg, client=C(bars))
        e._insts["TEST"] = INST
        return e

    def test_buys_fund_and_sells_it_when_cash_goes_negative(self):
        with tempfile.TemporaryDirectory() as tmp:
            bars = [bar(NOW - H, 100, 100, 100, 100)]            # fund price 100 in this fake
            e = self._engine(tmp, bars)
            e._rebalance(NOW)
            self.assertEqual(e.broker.units("fund"), 9_800)          # 98% of 1M at 100
            self.assertAlmostEqual(e.broker.cash(), 1_000_000 - 980_000 - 490)   # 0.05% fee
            self.assertTrue(any("[collateral] ticker=LQDT, action=buy" in n for n in e.notes), e.notes)

            e.broker.s["money"] = -5_000.0                            # variation margin loss
            e._rebalance(NOW + timedelta(minutes=15))
            # equity 975 000 -> keep 2% = 19 500 in cash: sell ceil(24 500 / 100) = 245 units
            self.assertEqual(e.broker.units("fund"), 9_800 - 245)
            self.assertGreater(e.broker.cash(), 19_000)

            before = e.broker.units("fund")
            e._rebalance(NOW + timedelta(minutes=30))                  # cash inside the band: nothing
            self.assertEqual(e.broker.units("fund"), before)

    def test_sizing_counts_the_fund_as_capital(self):
        with tempfile.TemporaryDirectory() as tmp:
            bars = flat_then_breakout()
            e = self._engine(tmp, bars)
            e.broker.s["money"] = 20_000.0
            e.broker.s["sec"] = {"fund": 9_800.0}                      # 980 000 RUB in the fund
            notes = e.run_once(NOW)
            entered = [n for n in notes if n.startswith("[entered]")]
            self.assertTrue(entered, notes)
            # risk 1% of ~1M = 10 000 RUB; with only 20 000 cash it would have been ~200 RUB
            self.assertIn("risk", entered[0])
            risk_rub = int(entered[0].split("sizing=risk ")[1].split(" RUB")[0])
            self.assertGreater(risk_rub, 5_000)

    def test_telegram_reports_fund_trades(self):
        from robot.telegram import order_messages, parse_run
        msgs = order_messages(parse_run("[collateral] ticker=LQDT, action=buy, units=463000, price=2.1149, amount=979199, cash_before=1000435\n"))
        self.assertEqual(msgs, ["💰 LQDT: Куплено 463000 паёв по 2.1149 (≈979 199 ₽) — обеспечение"])
