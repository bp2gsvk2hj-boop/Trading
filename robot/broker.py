"""Order execution: real account, T-Invest sandbox, or local paper simulation.

All brokers expose the same small interface used by the engine. Futures prices
are in points; quantities are in contracts (lot = 1 for all supported futures).
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .api import Client, f2q, q2f
from .market import Candle, Instrument


@dataclass
class StopOrder:
    id: str
    side: int          # +1 buys (protects a short), -1 sells (protects a long)
    qty: int
    price: float


@dataclass
class Holding:
    qty: int           # signed
    avg_price: float   # points


def order_key(*parts) -> str:
    """Deterministic idempotency key: the same decision never sends two orders."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "robot:" + ":".join(map(str, parts))))


class Broker:
    mode = "base"

    def holding(self, inst: Instrument) -> Holding: ...
    def stops(self, inst: Instrument) -> list[StopOrder]: ...
    def market(self, inst: Instrument, side: int, qty: int, key: str) -> float: ...
    def post_stop(self, inst: Instrument, side: int, qty: int, price: float, key: str) -> str: ...
    def cancel_stop(self, stop_id: str) -> None: ...
    def free_money(self) -> float: ...
    def stopouts_today(self, insts: list[Instrument], since: datetime) -> int: ...
    def sync(self, inst: Instrument, bars: list[Candle]) -> list[str]:
        """Paper only: simulate stop fills on new bars. Returns event notes."""
        return []


class ApiBroker(Broker):
    """Real account (sandbox=False) or T-Invest sandbox account (sandbox=True)."""

    def __init__(self, client: Client, account_id: str, sandbox: bool):
        self.c, self.acc, self.sandbox = client, account_id, sandbox
        self.mode = "sandbox" if sandbox else "live"

    def _m(self, live: str, sandbox: str) -> str:
        return sandbox if self.sandbox else live

    def holding(self, inst):
        pos = self.c.call(self._m("OperationsService/GetPositions", "SandboxService/GetSandboxPositions"),
                          {"accountId": self.acc})
        qty = sum(int(f.get("balance", 0)) for f in pos.get("futures", []) if f.get("instrumentUid") == inst.uid)
        avg = 0.0
        if qty:
            pf = self.c.call(self._m("OperationsService/GetPortfolio", "SandboxService/GetSandboxPortfolio"),
                             {"accountId": self.acc})
            for p in pf.get("positions", []):
                if p.get("instrumentUid") == inst.uid:
                    avg = q2f(p.get("averagePositionPricePt")) or q2f(p.get("averagePositionPrice"))
        return Holding(qty, avg)

    def stops(self, inst):
        r = self.c.call(self._m("StopOrdersService/GetStopOrders", "SandboxService/GetSandboxStopOrders"),
                        {"accountId": self.acc})
        out = []
        for s in r.get("stopOrders", []):
            if s.get("instrumentUid") != inst.uid or s.get("status", "STOP_ORDER_STATUS_ACTIVE") != "STOP_ORDER_STATUS_ACTIVE":
                continue
            side = 1 if s["direction"] == "STOP_ORDER_DIRECTION_BUY" else -1
            out.append(StopOrder(s["stopOrderId"], side, int(s.get("lotsRequested", 0)), q2f(s.get("stopPrice"))))
        return out

    def market(self, inst, side, qty, key):
        r = self.c.call(self._m("OrdersService/PostOrder", "SandboxService/PostSandboxOrder"), {
            "instrumentId": inst.uid, "quantity": str(qty), "accountId": self.acc,
            "direction": "ORDER_DIRECTION_BUY" if side > 0 else "ORDER_DIRECTION_SELL",
            "orderType": "ORDER_TYPE_MARKET", "orderId": key,
        })
        status = r.get("executionReportStatus", "")
        if status not in ("EXECUTION_REPORT_STATUS_FILL", "EXECUTION_REPORT_STATUS_PARTIALLYFILL", "EXECUTION_REPORT_STATUS_NEW"):
            raise RuntimeError(f"order not filled: {status} {r}")
        # executedOrderPrice is in rubles per contract for futures; convert to points
        px = q2f(r.get("executedOrderPrice"))
        return px / inst.point_value if px else 0.0

    def post_stop(self, inst, side, qty, price, key):
        r = self.c.call(self._m("StopOrdersService/PostStopOrder", "SandboxService/PostSandboxStopOrder"), {
            "instrumentId": inst.uid, "quantity": str(qty), "accountId": self.acc,
            "direction": "STOP_ORDER_DIRECTION_BUY" if side > 0 else "STOP_ORDER_DIRECTION_SELL",
            "stopPrice": f2q(inst.round_price(price)),
            "stopOrderType": "STOP_ORDER_TYPE_STOP_LOSS",
            "exchangeOrderType": "EXCHANGE_ORDER_TYPE_MARKET",
            "expirationType": "STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL",
            "orderId": key,
        })
        return r["stopOrderId"]

    def cancel_stop(self, stop_id):
        self.c.call(self._m("StopOrdersService/CancelStopOrder", "SandboxService/CancelSandboxStopOrder"),
                    {"accountId": self.acc, "stopOrderId": stop_id})

    def free_money(self):
        r = self.c.call(self._m("OperationsService/GetWithdrawLimits", "SandboxService/GetSandboxWithdrawLimits"),
                        {"accountId": self.acc})
        return sum(q2f(m) for m in r.get("money", []) if m.get("currency") == "rub")

    def stopouts_today(self, insts, since):
        r = self.c.call(self._m("StopOrdersService/GetStopOrders", "SandboxService/GetSandboxStopOrders"), {
            "accountId": self.acc, "status": "STOP_ORDER_STATUS_EXECUTED",
            "from": since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "to": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
        uids = {i.uid for i in insts}
        return sum(1 for s in r.get("stopOrders", [])
                   if s.get("instrumentUid") in uids and s.get("orderType") == "STOP_ORDER_TYPE_STOP_LOSS")


class PaperBroker(Broker):
    """Local simulation: fills market orders at the last price +/- slippage,
    triggers stops on candle highs/lows. State lives in a JSON file."""

    mode = "paper"

    def __init__(self, client: Client, path: Path, capital: float, slippage_ticks: int = 1, fee_rate: float = 0.0005):
        self.c, self.path = client, path
        self.slip_ticks, self.fee_rate = slippage_ticks, fee_rate
        self.clock = lambda: datetime.now(timezone.utc)
        self.s = json.loads(path.read_text()) if path.exists() else {
            "money": capital, "capital": capital, "pos": {}, "stops": {}, "stopouts": [], "trades": []}
        # The configured capital changed: book the difference as a deposit/withdrawal so sizing
        # follows the new size while open virtual positions and history are kept. States from
        # before this field existed were created at a round number of millions.
        prev = self.s.get("capital", round(self.s["money"], -6))
        if capital != prev:
            self.s["money"] += capital - prev
            self.s.setdefault("deposits", []).append(
                {"t": self.clock().isoformat(), "amount": capital - prev})
        self.s["capital"] = capital

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.s, indent=1, ensure_ascii=False))

    def _last(self, inst):
        r = self.c.call("MarketDataService/GetLastPrices", {"instrumentId": [inst.uid]})
        return q2f(r["lastPrices"][0]["price"])

    def _fill(self, inst, side, qty, price, why):
        p = self.s["pos"].get(inst.uid, {"qty": 0, "avg": 0.0})
        q0, q1 = p["qty"], p["qty"] + side * qty
        fee = abs(price) * inst.point_value * qty * self.fee_rate
        self.s["money"] -= fee
        if q0 and (q0 > 0) != (side > 0):  # reducing
            closed = min(abs(q0), qty)
            self.s["money"] += (price - p["avg"]) * (1 if q0 > 0 else -1) * closed * inst.point_value
        if q1 == 0:
            self.s["pos"].pop(inst.uid, None)
        else:
            avg = price if (q0 == 0 or (q0 > 0) != (q1 > 0)) else (
                p["avg"] if abs(q1) < abs(q0) else (p["avg"] * abs(q0) + price * qty) / abs(q1))
            self.s["pos"][inst.uid] = {"qty": q1, "avg": avg, "ticker": inst.ticker}
        self.s["trades"].append({"t": self.clock().isoformat(), "ticker": inst.ticker,
                                 "side": side, "qty": qty, "price": price, "why": why})
        self.save()

    def holding(self, inst):
        p = self.s["pos"].get(inst.uid)
        return Holding(p["qty"], p["avg"]) if p else Holding(0, 0.0)

    def stops(self, inst):
        return [StopOrder(k, v["side"], v["qty"], v["price"]) for k, v in self.s["stops"].items() if v["uid"] == inst.uid]

    def market(self, inst, side, qty, key):
        if key in {t.get("key") for t in self.s["trades"]}:
            return 0.0
        price = self._last(inst) + side * self.slip_ticks * inst.step
        self._fill(inst, side, qty, price, "market")
        self.s["trades"][-1]["key"] = key
        self.save()
        return price

    def post_stop(self, inst, side, qty, price, key):
        self.s["stops"][key] = {"uid": inst.uid, "ticker": inst.ticker, "side": side, "qty": qty,
                                "price": inst.round_price(price),
                                "since": self.clock().isoformat()}
        self.save()
        return key

    def cancel_stop(self, stop_id):
        self.s["stops"].pop(stop_id, None)
        self.save()

    def free_money(self):
        return self.s["money"]

    def stopouts_today(self, insts, since):
        uids = {i.uid for i in insts}
        return sum(1 for s in self.s["stopouts"] if s["uid"] in uids and datetime.fromisoformat(s["t"]) >= since)

    def sync(self, inst, bars):
        notes = []
        for sid, s in list(self.s["stops"].items()):
            if s["uid"] != inst.uid:
                continue
            since = datetime.fromisoformat(s["since"])
            # Only bars that started after the stop was placed can trigger it.
            for b in bars:
                if b.t < since:
                    continue
                hit = b.l <= s["price"] if s["side"] < 0 else b.h >= s["price"]
                if hit:
                    gap = b.o <= s["price"] if s["side"] < 0 else b.o >= s["price"]
                    px = (b.o if gap else s["price"]) + s["side"] * self.slip_ticks * inst.step
                    self.s["stops"].pop(sid)
                    self._fill(inst, s["side"], s["qty"], px, "stop")
                    self.s["stopouts"].append({"uid": inst.uid, "t": b.t.isoformat()})
                    self.save()
                    notes.append(f"paper stop filled {inst.ticker} @ {px:g}")
                    break
        return notes
