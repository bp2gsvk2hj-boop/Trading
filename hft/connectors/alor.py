"""Каркас коннектора ALOR OpenAPI (WebSocket).

НЕ РЕАЛИЗОВАНО И НЕ ПРОВЕРЕНО на реальном счёте. Здесь зафиксировано, что нужно сделать;
сверяйте форматы сообщений с актуальной документацией https://alor.dev/docs.

План:
- Авторизация: обмен refresh-токена на JWT (`https://oauth.alor.ru/refresh`), продление по таймеру.
- Данные: WebSocket `wss://api.alor.ru/ws`, подписки `OrderBookGetAndSubscribe`
  (depth 5–10, format Simple) и `AllTradesGetAndSubscribe` по коду инструмента (например `SiZ4`),
  плюс `OrdersGetAndSubscribeV2` и `TradesGetAndSubscribeV2` по своему портфелю.
- Заявки: WebSocket API команд (`create:limit`, `delete`) — быстрее REST.
  Свой `client_id` → биржевой номер заявки храните в словаре, ответы матчите по `guid` запроса.
- Время сообщений переводите в микросекунды UTC.
- При разрыве соединения: `cancel_all`, переподключение, сверка позиции через REST до возобновления котирования.
"""
from __future__ import annotations

from ..types import Command
from .base import BrokerConnector


class AlorConnector(BrokerConnector):
    def __init__(self, symbol: str, portfolio: str, refresh_token: str, exchange: str = "MOEX"):
        super().__init__(symbol)
        self.portfolio = portfolio
        self.refresh_token = refresh_token
        self.exchange = exchange

    async def connect(self) -> None:
        raise NotImplementedError("ALOR: авторизация и подписки ещё не реализованы")

    async def run(self) -> None:
        raise NotImplementedError

    def send(self, cmd: Command) -> None:
        raise NotImplementedError

    async def cancel_all(self) -> None:
        raise NotImplementedError
