"""Расписание торгов FORTS: окна, в которых стратегия котирует, и закрытие позиции перед клирингом.

Время — московское (UTC+3, без перехода на летнее время). Расписание биржи иногда
меняется, окна задаются в конфиге.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from enum import Enum

MSK = timezone(timedelta(hours=3))


class SessionState(Enum):
    TRADE = "trade"  # котируем
    FLATTEN = "flatten"  # скоро клиринг/закрытие: снимаем заявки и закрываем позицию
    CLOSED = "closed"  # вне окон


def _t(s: str) -> time:
    h, m = s.split(":")[:2]
    return time(int(h), int(m))


@dataclass
class TradingSchedule:
    # Основная сессия с паузами на дневной (14:00–14:05) и вечерний (18:50–19:05) клиринг.
    # Первые минуты после открытия пропускаем: широкие спреды и аукционные эффекты.
    windows: list[tuple[str, str]] = field(
        default_factory=lambda: [("10:01", "13:59"), ("14:06", "18:49"), ("19:06", "23:49")]
    )
    flatten_before_sec: int = 120
    always_open: bool = False  # для синтетических данных и тестов

    def state(self, ts_us: int) -> SessionState:
        if self.always_open:
            return SessionState.TRADE
        now = datetime.fromtimestamp(ts_us / 1e6, tz=MSK)
        sec = now.hour * 3600 + now.minute * 60 + now.second
        for start, end in self.windows:
            s, e = _t(start), _t(end)
            s_sec = s.hour * 3600 + s.minute * 60
            e_sec = e.hour * 3600 + e.minute * 60
            if s_sec <= sec < e_sec:
                return SessionState.FLATTEN if sec >= e_sec - self.flatten_before_sec else SessionState.TRADE
        return SessionState.CLOSED
