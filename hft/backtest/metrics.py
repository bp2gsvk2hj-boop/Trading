"""Сводные метрики бэктеста."""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass

from .engine import BacktestResult


@dataclass
class Metrics:
    pnl_rub: float
    fees_rub: float
    gross_pnl_rub: float
    n_fills: int
    contracts_traded: int
    maker_share: float
    pnl_per_contract_rub: float
    orders_sent: int
    commands_sent: int
    fill_ratio: float  # исполненные контракты / выставленные заявки
    max_drawdown_rub: float
    sharpe_annual: float  # по минутным приращениям equity, только как ориентир
    max_abs_position: int
    final_position: int
    kill_reason: str

    def as_dict(self) -> dict:
        return asdict(self)

    def report(self) -> str:
        rows = [
            ("PnL, руб. (после комиссий)", f"{self.pnl_rub:,.0f}"),
            ("Комиссии, руб.", f"{self.fees_rub:,.0f}"),
            ("PnL до комиссий, руб.", f"{self.gross_pnl_rub:,.0f}"),
            ("Сделок / контрактов", f"{self.n_fills} / {self.contracts_traded}"),
            ("Доля мейкерских", f"{self.maker_share:.1%}"),
            ("PnL на контракт, руб.", f"{self.pnl_per_contract_rub:.2f}"),
            ("Заявок / команд", f"{self.orders_sent} / {self.commands_sent}"),
            ("Контрактов на заявку", f"{self.fill_ratio:.3f}"),
            ("Макс. просадка, руб.", f"{self.max_drawdown_rub:,.0f}"),
            ("Sharpe (годовой, ориентир)", f"{self.sharpe_annual:.2f}"),
            ("Макс. |позиция|", str(self.max_abs_position)),
            ("Позиция на конец", str(self.final_position)),
        ]
        if self.kill_reason:
            rows.append(("Остановка риском", self.kill_reason))
        w = max(len(k) for k, _ in rows)
        return "\n".join(f"{k:<{w}}  {v}" for k, v in rows)


def compute_metrics(res: BacktestResult) -> Metrics:
    contracts = sum(f.qty for f in res.fills)
    maker = sum(f.qty for f in res.fills if f.maker)
    eq = res.equity_rub
    peak, mdd = -math.inf, 0.0
    for v in eq:
        peak = max(peak, v)
        mdd = max(mdd, peak - v)

    # минутные приращения
    rets: list[float] = []
    if res.equity_ts:
        bucket = res.equity_ts[0] // 60_000_000
        last = 0.0
        for ts, v in zip(res.equity_ts, eq):
            b = ts // 60_000_000
            if b != bucket:
                rets.append(v - last)
                last, bucket = v, b
        rets.append(res.final_equity_rub - last)
    sharpe = 0.0
    if len(rets) > 1:
        mu = sum(rets) / len(rets)
        sd = math.sqrt(sum((r - mu) ** 2 for r in rets) / (len(rets) - 1))
        # ~ 14 ч торгов * 60 мин * 252 дня
        sharpe = 0.0 if sd == 0 else mu / sd * math.sqrt(14 * 60 * 252)

    return Metrics(
        pnl_rub=res.final_equity_rub,
        fees_rub=res.fees_rub,
        gross_pnl_rub=res.final_equity_rub + res.fees_rub,
        n_fills=len(res.fills),
        contracts_traded=contracts,
        maker_share=maker / contracts if contracts else 0.0,
        pnl_per_contract_rub=res.final_equity_rub / contracts if contracts else 0.0,
        orders_sent=res.n_orders,
        commands_sent=res.n_commands,
        fill_ratio=contracts / res.n_orders if res.n_orders else 0.0,
        max_drawdown_rub=mdd,
        sharpe_annual=sharpe,
        max_abs_position=max((abs(p) for p in res.position), default=0),
        final_position=res.final_position,
        kill_reason=res.kill_reason,
    )
