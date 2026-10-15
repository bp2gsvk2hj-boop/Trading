"""Абстракция брокерского подключения.

Коннектор отвечает за две вещи:
1. Поток рыночных данных (стакан, обезличенные сделки) — передаёт их в `MarketDataHandler`.
2. Исполнение: принимает команды `PlaceOrder`/`CancelOrder` (протокол `Venue`)
   и сообщает о подтверждениях, снятиях и сделках в `ExecutionHandler`.

Все колбэки должны вызываться из одного потока (event loop), трейдер не потокобезопасен.
Метод `send` не должен синхронно вызывать колбэки — только ставить их в очередь.
"""
from __future__ import annotations

import abc
from typing import Protocol

from ..types import Command, Fill, OrderBook, Trade


class MarketDataHandler(Protocol):
    def on_book(self, book: OrderBook) -> None: ...
    def on_trade(self, trade: Trade) -> None: ...


class ExecutionHandler(Protocol):
    def on_ack(self, client_id: str, ts: int) -> None: ...
    def on_cancelled(self, client_id: str, ts: int) -> None: ...
    def on_reject(self, client_id: str, ts: int, reason: str = "") -> None: ...
    def on_fill(self, fill: Fill) -> None: ...


class BrokerConnector(abc.ABC):
    """Базовый класс коннектора. Трейдер одновременно и MarketDataHandler, и ExecutionHandler."""

    def __init__(self, symbol: str):
        self.symbol = symbol
        self.handler = None  # Trader

    def bind(self, handler) -> None:
        self.handler = handler

    @abc.abstractmethod
    async def connect(self) -> None:
        """Авторизация, подписка на стакан/сделки/свои заявки и сделки."""

    @abc.abstractmethod
    async def run(self) -> None:
        """Цикл чтения сообщений: разбирает их и вызывает колбэки `self.handler`."""

    @abc.abstractmethod
    def send(self, cmd: Command) -> None:
        """Неблокирующая отправка команды (реализация протокола `Venue`)."""

    @abc.abstractmethod
    async def cancel_all(self) -> None:
        """Аварийное снятие всех заявок по инструменту (kill switch)."""

    async def close(self) -> None:
        pass
