"""Генератор синтетического стакана и ленты сделок.

Нужен для проверки механики (движок, стратегия, риск), а не для оценки доходности.
В модели дисбаланс стакана намеренно несёт информацию о следующем движении цены,
поэтому результат на синтетике нельзя переносить на реальный рынок.
"""
from __future__ import annotations

import random
from datetime import datetime

from ..instruments import Instrument
from ..sessions import MSK
from ..types import OrderBook, Side, Trade


def generate(
    instrument: Instrument,
    seconds: float = 3600,
    seed: int = 1,
    start_price: float = 92_000.0,
    start: datetime = datetime(2024, 3, 1, 10, 5, tzinfo=MSK),
    mean_step_ms: float = 25.0,
    levels: int = 5,
    base_qty: float = 25.0,
    phi: float = 0.97,
    imb_noise: float = 0.12,
    trade_prob: float = 0.35,
    informed: float = 0.35,
    noise_move_prob: float = 0.004,
) -> list[OrderBook | Trade]:
    rng = random.Random(seed)
    tick = instrument.tick_size
    bid = round(start_price / tick)  # цена лучшей покупки в тиках
    bid_q = [max(1, int(rng.gauss(base_qty, 5))) for _ in range(levels)]
    ask_q = [max(1, int(rng.gauss(base_qty, 5))) for _ in range(levels)]
    imb = 0.0
    ts = int(start.timestamp() * 1_000_000)
    end = ts + int(seconds * 1_000_000)
    out: list[OrderBook | Trade] = []

    def fresh() -> int:
        return max(1, int(rng.expovariate(1 / (base_qty * 0.4))))

    def move(up: bool) -> None:
        nonlocal bid, bid_q, ask_q
        if up:
            bid += 1
            bid_q = [fresh()] + bid_q[:-1]
            ask_q = ask_q[1:] + [max(1, int(rng.gauss(base_qty, 5)))]
        else:
            bid -= 1
            ask_q = [fresh()] + ask_q[:-1]
            bid_q = bid_q[1:] + [max(1, int(rng.gauss(base_qty, 5)))]

    while ts < end:
        ts += max(1, int(rng.expovariate(1 / (mean_step_ms * 1000))))
        imb = max(-0.9, min(0.9, phi * imb + rng.gauss(0, imb_noise)))
        # пополнение лучших уровней к целевому объёму, зависящему от дисбаланса
        tb, ta = base_qty * (1 + imb), base_qty * (1 - imb)
        bid_q[0] = max(1, bid_q[0] + round(0.3 * (tb - bid_q[0]) + rng.gauss(0, 2)))
        ask_q[0] = max(1, ask_q[0] + round(0.3 * (ta - ask_q[0]) + rng.gauss(0, 2)))

        if rng.random() < trade_prob:
            buy = rng.random() < 0.5 + informed * imb
            size = 1 + int(rng.expovariate(1 / 4.0))
            if buy:
                q = min(size, ask_q[0])
                out.append(Trade(ts, (bid + 1) * tick, q, Side.BUY))
                ask_q[0] -= q
                if ask_q[0] <= 0:
                    move(up=True)
            else:
                q = min(size, bid_q[0])
                out.append(Trade(ts, bid * tick, q, Side.SELL))
                bid_q[0] -= q
                if bid_q[0] <= 0:
                    move(up=False)
        elif rng.random() < noise_move_prob:
            move(up=rng.random() < 0.5)

        out.append(
            OrderBook(
                ts,
                [((bid - i) * tick, bid_q[i]) for i in range(levels)],
                [((bid + 1 + i) * tick, ask_q[i]) for i in range(levels)],
            )
        )
    return out
