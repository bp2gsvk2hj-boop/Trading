"""Спецификации фьючерсов FORTS и модель комиссий.

ВАЖНО: комиссии ниже — заглушки для прототипа. Перед использованием подставьте
фактические биржевой сбор (тейкер/мейкер) и тариф своего брокера. Для HFT
комиссия брокера часто важнее любой альфы.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Instrument:
    symbol: str
    tick_size: float  # шаг цены, пунктов
    tick_value: float  # стоимость шага цены, руб. за контракт
    maker_fee_rub: float = 0.0  # фиксированная часть комиссии за контракт (биржа + брокер)
    taker_fee_rub: float = 0.0
    maker_fee_pct: float = 0.0  # доля от стоимости контракта (0.0001 = 0.01%)
    taker_fee_pct: float = 0.0

    @property
    def rub_per_point(self) -> float:
        return self.tick_value / self.tick_size

    def notional_rub(self, price: float) -> float:
        return price * self.rub_per_point

    def fee_rub(self, price: float, qty: int, maker: bool) -> float:
        fixed = self.maker_fee_rub if maker else self.taker_fee_rub
        pct = self.maker_fee_pct if maker else self.taker_fee_pct
        return qty * (fixed + pct * self.notional_rub(price))

    def fee_in_points(self, price: float, maker: bool) -> float:
        """Комиссия за один контракт, выраженная в пунктах цены."""
        return self.fee_rub(price, 1, maker) / self.rub_per_point

    def round_price(self, price: float) -> float:
        return round(round(price / self.tick_size) * self.tick_size, 8)

    def floor_price(self, price: float) -> float:
        return round(math.floor(price / self.tick_size + 1e-9) * self.tick_size, 8)

    def ceil_price(self, price: float) -> float:
        return round(math.ceil(price / self.tick_size - 1e-9) * self.tick_size, 8)


# Si — фьючерс на курс доллар/рубль, 1 контракт = 1000 USD, шаг 1 пункт = 1 руб.
SI = Instrument("Si", tick_size=1.0, tick_value=1.0, maker_fee_rub=0.5, taker_fee_rub=1.0)

# RTS — фьючерс на индекс РТС, шаг 10 пунктов. Стоимость шага плавает вместе с курсом
# доллара (0.2 USD за шаг), задайте актуальное значение.
RTS = Instrument("RTS", tick_size=10.0, tick_value=18.0, maker_fee_rub=1.0, taker_fee_rub=2.0)

INSTRUMENTS = {"Si": SI, "RTS": RTS}
