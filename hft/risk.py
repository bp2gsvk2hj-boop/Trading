"""Риск-менеджмент: лимиты позиции, убытка, частоты заявок и свежести данных."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass


@dataclass
class RiskConfig:
    max_position: int = 5
    max_order_qty: int = 5
    max_daily_loss_rub: float = 5_000.0
    # Биржа берёт сбор за «неэффективные транзакции» при плохом соотношении
    # заявок к сделкам — ограничиваем частоту отправки команд.
    max_orders_per_sec: int = 20
    max_book_age_ms: float = 500.0  # старый стакан => снимаем котировки


class RiskManager:
    def __init__(self, config: RiskConfig | None = None):
        self.cfg = config or RiskConfig()
        self.killed = False
        self.kill_reason = ""
        self._sent: deque[int] = deque()

    def kill(self, reason: str) -> None:
        if not self.killed:
            self.killed = True
            self.kill_reason = reason

    def check_pnl(self, pnl_rub: float) -> None:
        if pnl_rub <= -self.cfg.max_daily_loss_rub:
            self.kill(f"дневной лимит убытка: {pnl_rub:.0f} руб.")

    def book_is_fresh(self, now_us: int, book_ts_us: int) -> bool:
        return (now_us - book_ts_us) / 1000.0 <= self.cfg.max_book_age_ms

    def rate_ok(self, ts: int) -> bool:
        while self._sent and ts - self._sent[0] >= 1_000_000:
            self._sent.popleft()
        return len(self._sent) < self.cfg.max_orders_per_sec

    def record_command(self, ts: int) -> None:
        self._sent.append(ts)

    def position_ok(self, exposure_after: int) -> bool:
        """exposure_after — позиция + все активные заявки в эту сторону + новая заявка."""
        return abs(exposure_after) <= self.cfg.max_position

    def qty_ok(self, qty: int) -> bool:
        return 0 < qty <= self.cfg.max_order_qty
