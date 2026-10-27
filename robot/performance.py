"""Portfolio value and returns since the robot started on the account (live mode).

Value = rubles + market value of securities (LQDT etc.), as the broker shows it. Futures P&L
counts once the clearing has credited it to cash (twice a day); intraday variation margin is not
included.

Past values are rebuilt backwards from today: cash(t) = cash now - cash effect of every operation
after t, holdings likewise, securities valued at the last hourly close before t. Futures BUY/SELL
operations carry the contract value as "payment" but move no money, so they are skipped.

TWR: chain-linked returns between external flows (deposits/withdrawals), independent of their size.
MWR: internal rate of return of the flows (start value, deposits, withdrawals, value now).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .api import Client, q2f
from .market import fetch_candles

FLOW_TYPES = {
    "OPERATION_TYPE_INPUT", "OPERATION_TYPE_OUTPUT", "OPERATION_TYPE_INPUT_SWIFT", "OPERATION_TYPE_OUTPUT_SWIFT",
    "OPERATION_TYPE_INPUT_ACQUIRING", "OPERATION_TYPE_OUTPUT_ACQUIRING", "OPERATION_TYPE_INP_MULTI",
    "OPERATION_TYPE_OUT_MULTI", "OPERATION_TYPE_TRANS_IIS_BS", "OPERATION_TYPE_TRANS_BS_BS",
}


@dataclass
class Report:
    start: datetime
    value: float                     # now
    cash: float
    holdings: list[tuple[str, float]]  # (ticker, market value)
    start_value: float
    net_flows: float                 # deposits - withdrawals since start
    pnl: float
    twr: float                       # for the whole period, e.g. 0.012 = +1.2%
    mwr: float                       # for the whole period
    mwr_annual: float
    days: float


def _t(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def operations(c: Client, account: str, since: datetime, now: datetime) -> list[dict]:
    out, cursor = [], ""
    while True:
        r = c.call("OperationsService/GetOperationsByCursor", {
            "accountId": account, "from": _iso(since), "to": _iso(now), "cursor": cursor, "limit": 1000,
            "state": "OPERATION_STATE_EXECUTED", "withoutTrades": True})
        out += r.get("items", [])
        if not r.get("hasNext"):
            return sorted(out, key=lambda o: o["date"])
        cursor = r["nextCursor"]


def is_trade(op: dict) -> bool:
    t = op.get("type", "")
    return ("_BUY" in t or "_SELL" in t) and "FEE" not in t


def cash_effect(op: dict) -> float:
    if op.get("instrumentType") == "futures" and is_trade(op):
        return 0.0
    return q2f(op.get("payment"))


def units_effect(op: dict) -> float:
    if op.get("instrumentType") in ("futures", "currency", "") or not is_trade(op):
        return 0.0
    qty = float(op.get("quantityDone") or op.get("quantity") or 0)
    return qty if "BUY" in op["type"] else -qty


def xirr_period(flows: list[tuple[float, float]]) -> float:
    """flows: (days from start, amount; money put in < 0). Daily rate solving NPV = 0, by bisection."""
    npv = lambda d: sum(a / (1 + d) ** t for t, a in flows)
    lo, hi = -0.5, 1.0
    if npv(lo) * npv(hi) > 0:
        raise ValueError("no IRR in range")
    for _ in range(200):
        mid = (lo + hi) / 2
        if npv(lo) * npv(mid) <= 0:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def compute(c: Client, account: str, start: datetime, now: datetime | None = None,
            price_at=None) -> Report:
    now = now or datetime.now(timezone.utc)
    pf = c.call("OperationsService/GetPortfolio", {"accountId": account, "currency": "RUB"})
    cash, units, prices, tickers = 0.0, {}, {}, {}
    for p in pf.get("positions", []):
        kind, uid = p.get("instrumentType"), p.get("instrumentUid")
        if kind == "currency":
            if p.get("ticker", "").startswith("RUB"):
                cash += q2f(p.get("quantity"))
        elif kind != "futures":
            units[uid] = q2f(p.get("quantity"))
            prices[uid] = q2f(p.get("currentPrice"))
            tickers[uid] = p.get("ticker", uid)
    holdings = [(tickers[u], units[u] * prices[u]) for u in units if units[u]]
    value = cash + sum(v for _, v in holdings)

    ops = operations(c, account, start, now)
    if price_at is None:
        def price_at(uid: str, t: datetime) -> float:
            bars = fetch_candles(c, uid, "1h", t - timedelta(days=5), t, complete_only=False)
            if not bars:
                raise LookupError(f"no price for {uid} at {t}")
            return bars[-1].c

    def value_at(t: datetime) -> float:
        later = [o for o in ops if _t(o["date"]) > t]
        v = cash - sum(cash_effect(o) for o in later)
        held: dict[str, float] = dict(units)
        for o in later:
            held[o.get("instrumentUid", "")] = held.get(o.get("instrumentUid", ""), 0.0) - units_effect(o)
        return v + sum(q * price_at(u, t) for u, q in held.items() if abs(q) > 1e-9)

    start_value = value_at(start)
    flows = [(_t(o["date"]), q2f(o.get("payment"))) for o in ops if o.get("type") in FLOW_TYPES]

    growth, base = 1.0, start_value
    for t, f in flows:
        v = value_at(t - timedelta(seconds=1))
        if base > 0:
            growth *= v / base
        base = v + f
    if base > 0:
        growth *= value / base

    days = (now - start).total_seconds() / 86400
    cf = [(0.0, -start_value)] + [((t - start).total_seconds() / 86400, -f) for t, f in flows] + [(days, value)]
    d = xirr_period(cf)
    net = sum(f for _, f in flows)
    return Report(start, value, cash, holdings, start_value, net, value - start_value - net,
                  growth - 1, (1 + d) ** days - 1, (1 + d) ** 365 - 1, days)


def format_report(r: Report) -> list[str]:
    n = lambda x: f"{x:,.0f}".replace(",", " ")
    pct = lambda x: f"{x * 100:+.2f}%".replace(".", ",")
    parts = [f"деньги {n(r.cash)} ₽"] + [f"{tk} {n(v)} ₽" for tk, v in r.holdings]
    lines = [f"💼 Портфель: {n(r.value)} ₽ ({', '.join(parts)}); вар. маржа фьючерсов — после клиринга",
             f"📈 С {r.start.astimezone(timezone(timedelta(hours=3))):%d.%m.%Y} ({r.days:.0f} дн.): "
             f"{'+' if r.pnl >= 0 else '−'}{n(abs(r.pnl))} ₽, TWR {pct(r.twr)}, MWR {pct(r.mwr)}"]
    if r.days >= 30:
        lines[-1] += f" ({pct(r.mwr_annual)} годовых)"
    return lines
