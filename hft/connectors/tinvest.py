"""Каркас коннектора T-Invest API (gRPC).

НЕ РЕАЛИЗОВАНО И НЕ ПРОВЕРЕНО. Подходит скорее для отладки и медленных версий стратегии:
задержки и лимиты запросов T-Invest API не рассчитаны на высокочастотное котирование.

План:
- SDK `t-tech-investments` (бывший `tinkoff-investments`), `AsyncClient`.
- Данные: `MarketDataStream` — подписка на стакан (`OrderBookInstrument`, depth 10) и сделки
  (`TradeInstrument`) по FIGI/UID фьючерса.
- Заявки: `PostOrder` (лимитные), `CancelOrder`; свои сделки — `OrdersStream`/`TradesStream`.
- `order_id` в PostOrder — идемпотентный ключ, используйте свой `client_id`.
"""
from __future__ import annotations

from ..types import Command
from .base import BrokerConnector


class TInvestConnector(BrokerConnector):
    def __init__(self, symbol: str, account_id: str, token: str):
        super().__init__(symbol)
        self.account_id = account_id
        self.token = token

    async def connect(self) -> None:
        raise NotImplementedError("T-Invest: подключение ещё не реализовано")

    async def run(self) -> None:
        raise NotImplementedError

    def send(self, cmd: Command) -> None:
        raise NotImplementedError

    async def cancel_all(self) -> None:
        raise NotImplementedError
