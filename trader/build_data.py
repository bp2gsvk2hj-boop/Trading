"""Data for the «Помощник трейдера» web app (trader/index.html).

python3 trader/build_data.py [--out trader/data.json] [--since 2026-10-16T13:32:44.5Z]

Reads the account in robot.toml through the T-Invest API (same data as the MCP connection),
needs TINVEST_TOKEN. Writes one JSON file with:
- trades: position episodes per futures contract (flat -> flat), with entry/exit, P&L before and
  after commissions, how they were closed (exchange stop / strategy / cut / aborted), MAE/MFE and
  a short price path for the chart;
- days: portfolio value at each day end, daily P&L, TWR index and a money-market benchmark (LQDT);
- result: where the P&L came from (trades, open positions, commissions, fund income, tariff/tax);
- positions: open futures with their stops.
The account id and the token are not written. Everything else (amounts in rubles) is.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from robot.api import Client, q2f  # noqa: E402
from robot.config import load  # noqa: E402
from robot.market import fetch_candles  # noqa: E402
from robot import performance as perf  # noqa: E402

MSK = timezone(timedelta(hours=3))
FLOW = perf.FLOW_TYPES


def t_of(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fills(ops: list[dict]) -> list[dict]:
    """Futures BUY/SELL operations with their commission."""
    fee = defaultdict(float)
    for o in ops:
        if o["type"] == "OPERATION_TYPE_BROKER_FEE" and o.get("parentOperationId"):
            fee[o["parentOperationId"]] += -q2f(o.get("payment"))
    out = []
    for o in ops:
        if o.get("instrumentType") != "futures" or o["type"] not in ("OPERATION_TYPE_BUY", "OPERATION_TYPE_SELL"):
            continue
        qty = int(o.get("quantityDone") or o.get("quantity") or 0)
        px = q2f(o.get("price"))
        if not qty or not px:
            continue
        out.append({"t": t_of(o["date"]), "ticker": o.get("ticker", ""), "uid": o.get("instrumentUid", ""),
                    "side": 1 if o["type"] == "OPERATION_TYPE_BUY" else -1, "qty": qty, "price": px,
                    "pv": abs(q2f(o.get("payment"))) / (px * qty), "fee": fee.get(o["id"], 0.0)})
    return sorted(out, key=lambda f: f["t"])


def episodes(fs: list[dict]) -> tuple[list[dict], dict]:
    """Split fills into position episodes per ticker. Returns (closed, open by ticker)."""
    closed, cur = [], {}
    for f in fs:
        e = cur.get(f["ticker"])
        qty = f["qty"]
        while qty:
            if e is None:
                e = cur[f["ticker"]] = {"ticker": f["ticker"], "uid": f["uid"], "side": f["side"], "pos": 0,
                                        "max_qty": 0, "entry_t": f["t"], "in_qty": 0, "in_val": 0.0,
                                        "out_qty": 0, "out_val": 0.0, "fees": 0.0, "pv": f["pv"],
                                        "exit_t": None, "fills": []}
            same = f["side"] == e["side"]
            n = qty if same else min(qty, e["pos"])
            share = n / f["qty"]
            e["fees"] += f["fee"] * share
            e["fills"].append({"t": iso(f["t"]), "side": f["side"], "qty": n, "price": f["price"]})
            if same:
                e["pos"] += n
                e["in_qty"] += n
                e["in_val"] += n * f["price"]
                e["max_qty"] = max(e["max_qty"], e["pos"])
            else:
                e["pos"] -= n
                e["out_qty"] += n
                e["out_val"] += n * f["price"]
                e["exit_t"] = f["t"]
            qty -= n
            if e["pos"] == 0:
                closed.append(e)
                e = cur[f["ticker"]] = None
        if cur.get(f["ticker"]) is None:
            cur.pop(f["ticker"], None)
    return closed, cur


def executed_stops(c: Client, acc: str, since: datetime, now: datetime) -> list[dict]:
    r = c.call("StopOrdersService/GetStopOrders", {"accountId": acc, "status": "STOP_ORDER_STATUS_ALL",
                                                   "from": iso(since - timedelta(days=1)), "to": iso(now)})
    return r.get("stopOrders", [])


def classify(e: dict, stops: list[dict]) -> str:
    """How the episode was closed: stop / strategy / abort (closed at once) / cut (several exits)."""
    exits = [f for f in e["fills"] if f["side"] != e["side"]]
    last = t_of(exits[-1]["t"])
    dur = (last - e["entry_t"]).total_seconds()
    if dur < 300 and abs(e["out_val"] / e["out_qty"] - e["in_val"] / e["in_qty"]) < 1e-9:
        return "abort"
    for s in stops:
        if s.get("status") != "STOP_ORDER_STATUS_EXECUTED" or s.get("instrumentUid") != e["uid"]:
            continue
        side = 1 if s.get("direction") == "STOP_ORDER_DIRECTION_BUY" else -1
        at = s.get("executionDate") or s.get("activationDateTime") or s.get("createDate")
        if side == -e["side"] and at and abs((t_of(at) - last).total_seconds()) < 180:
            return "stop"
    return "cut" if len(exits) > 1 else "strategy"


def stop_at_entry(e: dict, stops: list[dict]) -> bool:
    for s in stops:
        side = 1 if s.get("direction") == "STOP_ORDER_DIRECTION_BUY" else -1
        if s.get("instrumentUid") == e["uid"] and side == -e["side"] and s.get("createDate"):
            if 0 <= (t_of(s["createDate"]) - e["entry_t"]).total_seconds() < 180:
                return True
    return False


def path_and_excursions(c: Client, e: dict, now: datetime) -> dict:
    """MAE/MFE in rubles for the max position and a short close-price path."""
    end = e["exit_t"] or now
    if (end - e["entry_t"]).total_seconds() < 600:
        return {"mae": 0.0, "mfe": 0.0, "path": []}
    interval = "1h" if (end - e["entry_t"]) > timedelta(hours=6) else "15m"
    try:
        bars = [b for b in fetch_candles(c, e["uid"], interval, e["entry_t"] - timedelta(minutes=1), end,
                                         complete_only=False) if b.t <= end]
    except Exception:
        bars = []
    if not bars:
        return {"mae": 0.0, "mfe": 0.0, "path": []}
    entry = e["in_val"] / e["in_qty"]
    k = e["pv"] * e["max_qty"]
    hi, lo = max(b.h for b in bars), min(b.l for b in bars)
    fav, adv = (hi - entry, lo - entry) if e["side"] > 0 else (entry - lo, entry - hi)
    step = max(1, len(bars) // 60)
    path = [[iso(b.t), b.c] for b in bars[::step]]
    if e["exit_t"]:
        path.append([iso(e["exit_t"]), e["out_val"] / e["out_qty"]])
    return {"mae": round(min(0.0, adv) * k, 2), "mfe": round(max(0.0, fav) * k, 2), "path": path}


def build(since: datetime | None = None) -> dict:
    cfg = load()
    c = Client()
    acc = cfg.account_id
    now = datetime.now(timezone.utc)
    start = since or datetime.fromisoformat(cfg.started.replace("Z", "+00:00"))
    ops = perf.operations(c, acc, start, now)
    stops = executed_stops(c, acc, start, now)

    # ---- trades (the first robot trade is a second before `start`, which sits after the tariff fee)
    early = perf.operations(c, acc, start - timedelta(hours=2), start)
    closed, opened = episodes(fills(early + ops))
    closed = [e for e in closed if e["entry_t"] >= start - timedelta(minutes=5)]
    trades = []
    for e in closed:
        entry, exit_ = e["in_val"] / e["in_qty"], e["out_val"] / e["out_qty"]
        gross = (exit_ - entry) * e["side"] * e["in_qty"] * e["pv"]
        x = path_and_excursions(c, e, now)
        trades.append({
            "ticker": e["ticker"], "side": e["side"], "qty": e["max_qty"], "entry_t": iso(e["entry_t"]),
            "exit_t": iso(e["exit_t"]), "entry": round(entry, 6), "exit": round(exit_, 6), "pv": e["pv"],
            "gross": round(gross, 2), "fees": round(e["fees"], 2), "net": round(gross - e["fees"], 2),
            "exit_kind": classify(e, stops), "stop_at_entry": stop_at_entry(e, stops), **x})
    trades.sort(key=lambda t: t["entry_t"])
    # re-entry within 60 min after a stop-out on the same contract
    for i, tr in enumerate(trades):
        prev = [p for p in trades[:i] if p["ticker"] == tr["ticker"] and p["exit_kind"] == "stop"]
        tr["after_stop"] = bool(prev) and (t_of(tr["entry_t"]) - t_of(prev[-1]["exit_t"])) < timedelta(minutes=60)

    # ---- current portfolio
    pf = c.call("OperationsService/GetPortfolio", {"accountId": acc, "currency": "RUB"})
    cash, units, prices, tickers, positions = 0.0, {}, {}, {}, []
    active = [s for s in stops if s.get("status") == "STOP_ORDER_STATUS_ACTIVE"]
    for p in pf.get("positions", []):
        kind, uid = p.get("instrumentType"), p.get("instrumentUid")
        if kind == "currency":
            if p.get("ticker", "").startswith("RUB"):
                cash += q2f(p.get("quantity"))
        elif kind == "futures":
            qty = int(q2f(p.get("quantity")))
            st = [q2f(s.get("stopPrice")) for s in active if s.get("instrumentUid") == uid]
            ep = opened.get(p.get("ticker"))
            positions.append({"ticker": p.get("ticker"), "qty": qty,
                              "avg": q2f(p.get("averagePositionPricePt")) or q2f(p.get("averagePositionPrice")),
                              "last": q2f(p.get("currentPrice")), "pnl": q2f(p.get("expectedYield")),
                              "stops": st, "since": iso(ep["entry_t"]) if ep else None})
        else:
            units[uid] = q2f(p.get("quantity"))
            prices[uid] = q2f(p.get("currentPrice"))
            tickers[uid] = p.get("ticker", uid)
    holdings = {tickers[u]: units[u] * prices[u] for u in units if units[u]}
    value_now = cash + sum(holdings.values())

    # ---- daily values (cash + securities at the day's close; futures P&L once cleared)
    daily_px: dict[str, dict[str, float]] = {}
    sec_uids = {o.get("instrumentUid") for o in ops if perf.units_effect(o)} | set(units)
    for uid in sec_uids:
        bars = fetch_candles(c, uid, "1d", start - timedelta(days=7), now + timedelta(days=1), complete_only=False)
        daily_px[uid] = {b.t.astimezone(MSK).strftime("%Y-%m-%d"): b.c for b in bars}

    def price_on(uid: str, day: str) -> float:
        best = None
        for d, px in sorted(daily_px.get(uid, {}).items()):
            if d <= day:
                best = px
        return best if best is not None else prices.get(uid, 0.0)

    def value_at(t: datetime) -> float:
        later = [o for o in ops if t_of(o["date"]) > t]
        v = cash - sum(perf.cash_effect(o) for o in later)
        held = dict(units)
        for o in later:
            held[o.get("instrumentUid", "")] = held.get(o.get("instrumentUid", ""), 0.0) - perf.units_effect(o)
        day = t.astimezone(MSK).strftime("%Y-%m-%d")
        return v + sum(q * price_on(u, day) for u, q in held.items() if abs(q) > 1e-9)

    flows = [(t_of(o["date"]), q2f(o.get("payment"))) for o in ops if o["type"] in FLOW]
    days, d = [], start.astimezone(MSK).date()
    idx, base, prev_v = 1.0, value_at(start), None
    start_value = base
    fi = 0
    bench_uid = next((u for u, tk in tickers.items() if tk == cfg.collateral.ticker), None)
    bench0 = price_on(bench_uid, start.astimezone(MSK).strftime("%Y-%m-%d")) if bench_uid else None
    while d <= now.astimezone(MSK).date():
        # the evening clearing credits the day's futures P&L just after midnight MSK: count it to that day
        end = min(datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=MSK) + timedelta(hours=1), now)
        flow_day = 0.0
        while fi < len(flows) and flows[fi][0] <= end:
            ft, f = flows[fi]
            v = value_at(ft - timedelta(seconds=1))
            if base > 0:
                idx *= v / base
            base = v + f
            flow_day += f
            fi += 1
        v = value_now if end == now else value_at(end)
        if base > 0:
            idx *= v / base
        base = v
        ref = prev_v if prev_v is not None else start_value
        bench = price_on(bench_uid, d.isoformat()) / bench0 if bench0 else None
        days.append({"d": d.isoformat(), "value": round(v, 2), "pnl": round(v - ref - flow_day, 2),
                     "flow": round(flow_day, 2), "twr": round(idx, 6),
                     "bench": round(bench, 6) if bench else None})
        prev_v = v
        d += timedelta(days=1)

    # ---- where the result came from
    fee_fut = sum(-q2f(o["payment"]) for o in ops if o["type"] == "OPERATION_TYPE_BROKER_FEE"
                  and o.get("instrumentType") == "futures")
    fee_sec = sum(-q2f(o["payment"]) for o in ops if o["type"] == "OPERATION_TYPE_BROKER_FEE"
                  and o.get("instrumentType") != "futures")
    tariff = sum(-q2f(o["payment"]) for o in ops if o["type"] in (
        "OPERATION_TYPE_SERVICE_FEE", "OPERATION_TYPE_TAX", "OPERATION_TYPE_TAX_CORRECTION",
        "OPERATION_TYPE_MARGIN_FEE", "OPERATION_TYPE_OTHER_FEE"))
    net_flows = sum(f for _, f in flows)
    total = value_now - start_value - net_flows
    closed_gross = sum(t["gross"] for t in trades)
    open_pnl = sum(p["pnl"] for p in positions)
    fund = total - (closed_gross + open_pnl - fee_fut - fee_sec - tariff)
    result = {"closed": round(closed_gross, 2), "open": round(open_pnl, 2), "fees_futures": round(-fee_fut, 2),
              "fees_fund": round(-fee_sec, 2), "tariff_tax": round(-tariff, 2),
              "fund_and_other": round(fund, 2), "total": round(total, 2)}

    return {
        "generated": iso(now), "start": iso(start), "start_value": round(start_value, 2),
        "value": round(value_now, 2), "cash": round(cash, 2),
        "holdings": {k: round(v, 2) for k, v in holdings.items()}, "net_flows": round(net_flows, 2),
        "capital": cfg.risk.capital_rub, "risk_per_trade": cfg.risk.risk_per_trade,
        "benchmark": cfg.collateral.ticker, "trades": trades, "positions": positions,
        "days": days, "result": result,
        "flows": [{"t": iso(t), "amount": round(f, 2)} for t, f in flows],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "trader" / "data.json"))
    ap.add_argument("--since", default=None)
    a = ap.parse_args()
    since = datetime.fromisoformat(a.since.replace("Z", "+00:00")) if a.since else None
    data = build(since)
    Path(a.out).write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    print(f"{len(data['trades'])} trades, {len(data['days'])} days, value {data['value']:.0f} -> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
