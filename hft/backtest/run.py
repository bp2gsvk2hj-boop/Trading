"""CLI бэктеста.

    python -m hft.backtest.run --synthetic --seconds 3600
    python -m hft.backtest.run --instrument Si --book data/book.csv --trades data/trades.csv
"""
from __future__ import annotations

import argparse
import dataclasses
import json

from ..instruments import INSTRUMENTS, Instrument
from ..risk import RiskConfig, RiskManager
from ..sessions import TradingSchedule
from ..strategy import MarketMakerConfig, OBIMarketMaker
from ..trader import Trader, TraderConfig
from .data import merge_events, read_books, read_trades
from .engine import Backtester
from .metrics import compute_metrics
from .synthetic import generate


def run_backtest(
    instrument: Instrument,
    events,
    mm: MarketMakerConfig | None = None,
    risk: RiskConfig | None = None,
    trader_cfg: TraderConfig | None = None,
    schedule: TradingSchedule | None = None,
    latency_us: int = 1_000,
):
    mm = mm or MarketMakerConfig()
    risk = risk or RiskConfig(max_position=mm.max_position)

    def factory(venue):
        return Trader(
            instrument,
            OBIMarketMaker(instrument, mm),
            RiskManager(risk),
            venue,
            schedule or TradingSchedule(),
            trader_cfg,
        )

    bt = Backtester(instrument, factory, latency_us=latency_us)
    res = bt.run(events)
    return res, compute_metrics(res)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Бэктест HFT маркет-мейкера на FORTS")
    ap.add_argument("--instrument", default="Si", choices=sorted(INSTRUMENTS))
    ap.add_argument("--synthetic", action="store_true", help="синтетические данные")
    ap.add_argument("--seconds", type=float, default=3600)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--book", help="CSV стакана")
    ap.add_argument("--trades", help="CSV сделок")
    ap.add_argument("--latency-ms", type=float, default=1.0, help="задержка до биржи в одну сторону")
    ap.add_argument("--config", help="JSON с параметрами MarketMakerConfig")
    ap.add_argument("--maker-fee", type=float, help="комиссия мейкера, руб./контракт")
    ap.add_argument("--taker-fee", type=float, help="комиссия тейкера, руб./контракт")
    ap.add_argument("--json", action="store_true", help="вывести метрики в JSON")
    args = ap.parse_args(argv)

    inst = INSTRUMENTS[args.instrument]
    if args.maker_fee is not None:
        inst = dataclasses.replace(inst, maker_fee_rub=args.maker_fee)
    if args.taker_fee is not None:
        inst = dataclasses.replace(inst, taker_fee_rub=args.taker_fee)

    mm = MarketMakerConfig()
    if args.config:
        with open(args.config) as f:
            mm = dataclasses.replace(mm, **json.load(f))

    if args.synthetic:
        events = generate(inst, seconds=args.seconds, seed=args.seed)
        schedule = TradingSchedule(always_open=True)
    else:
        if not (args.book and args.trades):
            ap.error("нужны --book и --trades или --synthetic")
        events = merge_events(read_books(args.book), read_trades(args.trades))
        schedule = TradingSchedule()

    _, m = run_backtest(inst, events, mm, schedule=schedule, latency_us=int(args.latency_ms * 1000))
    print(json.dumps(m.as_dict(), ensure_ascii=False, indent=2) if args.json else m.report())


if __name__ == "__main__":
    main()
