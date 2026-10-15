"""Запуск стратегии на реальном подключении.

    connector = AlorConnector("SiZ4", portfolio="...", refresh_token=os.environ["ALOR_TOKEN"])
    asyncio.run(run_live(connector, SI))
"""
from __future__ import annotations

import logging

from .connectors.base import BrokerConnector
from .instruments import Instrument
from .risk import RiskConfig, RiskManager
from .sessions import TradingSchedule
from .strategy import MarketMakerConfig, OBIMarketMaker
from .trader import Trader, TraderConfig

log = logging.getLogger(__name__)


async def run_live(
    connector: BrokerConnector,
    instrument: Instrument,
    mm: MarketMakerConfig | None = None,
    risk: RiskConfig | None = None,
    trader_cfg: TraderConfig | None = None,
    schedule: TradingSchedule | None = None,
) -> Trader:
    mm = mm or MarketMakerConfig()
    trader = Trader(
        instrument,
        OBIMarketMaker(instrument, mm),
        RiskManager(risk or RiskConfig(max_position=mm.max_position)),
        connector,
        schedule or TradingSchedule(),
        trader_cfg,
    )
    connector.bind(trader)
    await connector.connect()
    try:
        await connector.run()
    finally:
        log.warning("остановка: снимаем все заявки")
        await connector.cancel_all()
        await connector.close()
    return trader
