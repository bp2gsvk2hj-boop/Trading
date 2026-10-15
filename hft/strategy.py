"""Маркет-мейкер с альфой из микроструктуры стакана.

Идея:
1. Справедливая цена = mid + альфа, где альфа складывается из microprice,
   многоуровневого дисбаланса стакана (OBI) и дисбаланса потока сделок.
2. Цена резервирования сдвигается против накопленной позиции (inventory skew,
   в духе Avellaneda–Stoikov), чтобы позиция возвращалась к нулю.
3. Котируем по обе стороны вокруг цены резервирования. Ширина — не меньше
   `min_half_width_ticks` и растёт с волатильностью.
4. Сторону, на которой ожидаемый край (edge) после комиссии меньше порога,
   не котируем — это защищает от «токсичных» исполнений перед движением цены.
5. Опционально: при очень сильном сигнале берём ликвидность агрессивно.
"""
from __future__ import annotations

from dataclasses import dataclass

from .instruments import Instrument
from .signals import MidVolatility, TradeFlow, book_imbalance, microprice
from .types import Fill, OrderBook, Quotes, Side, Trade


@dataclass
class MarketMakerConfig:
    quote_qty: int = 1
    max_position: int = 5
    # веса альфы
    k_micro: float = 1.0  # множитель (microprice - mid)
    k_obi: float = 0.6  # тиков на единицу дисбаланса стакана
    k_flow: float = 0.3  # тиков на единицу дисбаланса потока сделок
    obi_levels: int = 3
    obi_decay: float = 0.5
    flow_tau_sec: float = 2.0
    # ширина котировок
    min_half_width_ticks: float = 0.5
    vol_mult: float = 0.0  # добавка к полуширине: vol_mult * σ(mid за 1 c)
    vol_tau_sec: float = 30.0
    # инвентарь
    inventory_skew_ticks: float = 1.0  # сдвиг цены резервирования при |позиции| = max_position
    # фильтр по ожидаемому краю после комиссии, в тиках
    min_edge_ticks: float = 0.0
    # не стоять дальше N тиков от лучшей цены (там почти не исполняет, а транзакции платные)
    max_depth_ticks: int = 2
    # агрессивный вход: если |альфа| > порога (в тиках) — пересекаем спред. None = выключено
    take_threshold_ticks: float | None = None
    take_qty: int = 1


class OBIMarketMaker:
    def __init__(self, instrument: Instrument, config: MarketMakerConfig | None = None):
        self.inst = instrument
        self.cfg = config or MarketMakerConfig()
        self.book: OrderBook | None = None
        self.flow = TradeFlow(self.cfg.flow_tau_sec)
        self.vol = MidVolatility(self.cfg.vol_tau_sec)

    # --- события рынка ---
    def on_book(self, book: OrderBook) -> None:
        self.book = book
        if book.valid:
            self.vol.on_mid(book.ts, book.mid)

    def on_trade(self, trade: Trade) -> None:
        self.flow.on_trade(trade)

    def on_fill(self, fill: Fill) -> None:  # хук для адаптивных стратегий
        pass

    # --- сигналы ---
    def fair_value(self, ts: int) -> tuple[float, float]:
        """Возвращает (справедливая цена, альфа в пунктах)."""
        b = self.book
        assert b is not None
        tick = self.inst.tick_size
        alpha = (
            self.cfg.k_micro * (microprice(b) - b.mid)
            + self.cfg.k_obi * book_imbalance(b, self.cfg.obi_levels, self.cfg.obi_decay) * tick
            + self.cfg.k_flow * self.flow.value(ts) * tick
        )
        return b.mid + alpha, alpha

    # --- котирование ---
    def quotes(self, ts: int, position: int) -> Quotes:
        b = self.book
        if b is None or not b.valid:
            return Quotes.none()
        cfg, inst = self.cfg, self.inst
        tick = inst.tick_size

        fair, alpha = self.fair_value(ts)
        reservation = fair - cfg.inventory_skew_ticks * tick * position / max(cfg.max_position, 1)
        half_width = max(cfg.min_half_width_ticks * tick, cfg.vol_mult * self.vol.value)

        bid_px = inst.floor_price(reservation - half_width)
        ask_px = inst.ceil_price(reservation + half_width)
        # пассивные заявки: не пересекаем спред
        bid_px = min(bid_px, b.best_ask - tick)
        ask_px = max(ask_px, b.best_bid + tick)

        q = Quotes(meta={"fair": fair, "alpha": alpha, "reservation": reservation})

        fee = inst.fee_in_points(b.mid, maker=True)
        min_edge = cfg.min_edge_ticks * tick
        bid_ok = (
            fair - bid_px - fee >= min_edge
            and position + cfg.quote_qty <= cfg.max_position
            and bid_px >= b.best_bid - cfg.max_depth_ticks * tick
        )
        ask_ok = (
            ask_px - fair - fee >= min_edge
            and position - cfg.quote_qty >= -cfg.max_position
            and ask_px <= b.best_ask + cfg.max_depth_ticks * tick
        )
        if bid_ok:
            q.bid_px, q.bid_qty = bid_px, cfg.quote_qty
        if ask_ok:
            q.ask_px, q.ask_qty = ask_px, cfg.quote_qty

        if cfg.take_threshold_ticks is not None:
            taker_fee = inst.fee_in_points(b.mid, maker=False)
            thr = cfg.take_threshold_ticks * tick
            # платим полспреда + комиссию, ожидаем движение на |alpha|
            cost = b.spread / 2 + taker_fee
            if alpha - cost > thr and position + cfg.take_qty <= cfg.max_position:
                q.take_side, q.take_qty = Side.BUY, cfg.take_qty
            elif -alpha - cost > thr and position - cfg.take_qty >= -cfg.max_position:
                q.take_side, q.take_qty = Side.SELL, cfg.take_qty
        return q
