"""Live signal generator for Opening Drive Momentum (stateless).

Reads candles from the T-Invest REST API (read-only), reproduces the backtest logic
of strategies.open_drive and prints a JSON decision per instrument. Orders are
placed separately through the T-Invest MCP tools following LIVE_RUNBOOK.md.

Usage:
  python src/live.py signal [--equity RUB] [--now 2026-10-19T11:01]
  python src/live.py exit          # list what must be flat at 23:40
"""
import argparse, json, os, ssl, sys, tempfile, urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
MSK = timezone(timedelta(hours=3))
API = "https://invest-public-api.tbank.ru/rest/tinkoff.public.invest.api.contract.v1."

CFG = json.loads((ROOT / "live_config.json").read_text())
P = CFG["params"]          # k, lookback, checkpoints, start_tod
BAR = 5


def _ctx():
    bundle = Path(tempfile.gettempdir()) / "tinvest-ca.pem"
    parts = [ROOT / "certs" / "russian-trusted-ca.pem", Path("/root/.ccr/ca-bundle.crt")]
    bundle.write_text("".join(p.read_text() for p in parts if p.exists()))
    ctx = ssl.create_default_context()
    ctx.load_verify_locations(str(bundle))
    return ctx


CTX = _ctx()


def call(method, body):
    req = urllib.request.Request(API + method, data=json.dumps(body).encode(), headers={
        "Authorization": "Bearer " + os.environ["TINVEST_TOKEN"], "Content-Type": "application/json"})
    with urllib.request.urlopen(req, context=CTX, timeout=60) as r:
        return json.load(r)


def q(x):
    return int(x.get("units", 0)) + x.get("nano", 0) / 1e9 if x else np.nan


def front_contract(prefix, today):
    """Nearest-expiry contract with at least `roll_days` calendar days left."""
    fs = call("InstrumentsService/Futures", {"instrumentStatus": "INSTRUMENT_STATUS_BASE"})["instruments"]
    cs = [f for f in fs if f["ticker"].startswith(prefix) and len(f["ticker"]) == 4
          and f["ticker"][2] in "FGHJKMNQUVXZ" and f["classCode"] == "SPBFUT"]
    cs = [f for f in cs if pd.Timestamp(f["expirationDate"][:10]).date() - today >= timedelta(days=CFG["roll_days"])]
    return min(cs, key=lambda f: f["expirationDate"])


def candles(uid, start, end):
    rows, t = [], start
    while t < end:
        t2 = min(t + timedelta(days=6), end)
        js = call("MarketDataService/GetCandles", {
            "instrumentId": uid, "interval": "CANDLE_INTERVAL_5_MIN",
            "from": t.astimezone(timezone.utc).isoformat(), "to": t2.astimezone(timezone.utc).isoformat()})
        for c in js.get("candles", []):
            rows.append(dict(time=pd.Timestamp(c["time"]).tz_convert("Europe/Moscow").tz_localize(None),
                             open=q(c["open"]), high=q(c["high"]), low=q(c["low"]), close=q(c["close"]),
                             volume=int(c.get("volume", 0)), complete=c.get("isComplete", True)))
        t = t2
    df = pd.DataFrame(rows).drop_duplicates("time").sort_values("time")
    df = df[df.time.dt.weekday < 5]
    df["date"], df["tod"] = df.time.dt.normalize(), df.time.dt.hour * 60 + df.time.dt.minute
    return df


def decide(name, now, equity):
    spec = CFG["instruments"][name]
    fut = front_contract(spec["prefix"], now.date())
    uid, ticker = fut["uid"], fut["ticker"]
    df = candles(uid, now - timedelta(days=40), now)
    start, end_tod = P["start_tod"], P["end_tod"]
    sess = df[(df.tod >= start) & (df.tod <= end_tod)].copy()
    sess["move"] = (sess.close / sess.groupby("date").open.transform("first") - 1).abs()
    piv = sess.pivot_table(index="date", columns="tod", values="move")
    today = pd.Timestamp(now.date())
    hist = piv[piv.index < today].tail(P["lookback"])
    out = dict(instrument=name, ticker=ticker, instrumentId=f"{ticker}_SPBFUT", uid=uid,
               now=now.strftime("%Y-%m-%d %H:%M"), action="NONE")
    d = sess[sess.date == today]
    if d.empty or d.tod.iloc[0] > start + 10:
        return {**out, "reason": "no 10:00 session bars yet"}
    if len(hist) < max(5, P["lookback"] // 2):
        return {**out, "reason": f"not enough history ({len(hist)} days)"}
    O = float(d.open.iloc[0])
    sigma = hist.mean()
    # completed checkpoint bars: bar starting at cp-5 must have closed by now
    now_tod = now.hour * 60 + now.minute
    due = [cp for cp in P["checkpoints"] if cp <= now_tod]
    if not due:
        return {**out, "O": O, "reason": "before first checkpoint"}
    first_trigger, checks = None, []
    for cp in due:
        bar = d[d.tod == cp - BAR]
        if bar.empty or np.isnan(sigma.get(cp - BAR, np.nan)):
            checks.append(dict(cp=cp, status="missing bar"))
            continue
        c, s = float(bar.close.iloc[0]), float(sigma[cp - BAR])
        U, L = O * (1 + P["k"] * s), O * (1 - P["k"] * s)
        side = 1 if c > U else -1 if c < L else 0
        checks.append(dict(cp=f"{cp // 60}:{cp % 60:02d}", close=c, U=round(U, 4), L=round(L, 4), side=side))
        if side and first_trigger is None:
            first_trigger = (cp, side, c)
    out.update(O=O, checks=checks)
    if first_trigger is None:
        return {**out, "reason": "no breakout so far today"}
    cp, side, c = first_trigger
    if cp != due[-1] or now_tod - cp > CFG["max_signal_delay_min"]:
        return {**out, "reason": f"signal already fired at {cp // 60}:{cp % 60:02d} (one trade per day)",
                "fired_side": side}
    tick = spec["tick"]
    stop = round(round(O / tick) * tick, 6)
    risk_per_lot = abs(c - stop) * spec["pv"]
    qty_risk = int(equity * CFG["risk"] / risk_per_lot) if equity else 0
    qty = max(CFG["min_qty"], min(qty_risk, CFG["max_qty"]))
    return {**out, "action": "BUY" if side == 1 else "SELL", "side": side, "signal_close": c,
            "stop_price": stop, "stop_direction": "SELL" if side == 1 else "BUY",
            "qty": qty, "qty_risk_based": qty_risk, "risk_rub_per_lot": round(risk_per_lot, 2),
            "exit_at": "23:40 MSK"}


def vb_levels(name, spec, now):
    """Volatility-breakout levels for today: O +/- k * mean(range of last n days)."""
    V = CFG["vb"]
    fut = front_contract(spec["prefix"], now.date())
    df = candles(fut["uid"], now - timedelta(days=45), now)
    sess = df[df.tod >= V["anchor_tod"]]
    today = pd.Timestamp(now.date())
    g = sess.groupby("date")
    rng = ((g.high.max() - g.low.min()) / g.open.first())
    rng = rng[rng.index < today].tail(V["n_range"])
    d = sess[sess.date == today]
    out = dict(instrument=name, ticker=fut["ticker"], instrumentId=f"{fut['ticker']}_SPBFUT")
    if d.empty or d.tod.iloc[0] > V["anchor_tod"] + 5:
        return {**out, "action": "NONE", "reason": "no 10:00 bar yet"}
    if len(rng) < V["n_range"]:
        return {**out, "action": "NONE", "reason": f"history {len(rng)} days"}
    O = float(d.open.iloc[0]); R = float(rng.mean()) * O; tick = spec["tick"]
    rnd = lambda x: round(round(x / tick) * tick, 6)
    up, dn = rnd(O + V["k"] * R), rnd(O - V["k"] * R)
    hi, lo = float(d.high.max()), float(d.low.min())
    return {**out, "action": "PLACE", "O": O, "range": round(R, 6), "buy_stop": up, "sell_stop": dn,
            "protective_stop": rnd(O), "already_hit_up": hi >= up, "already_hit_dn": lo <= dn,
            "entries_until": "18:00", "exit_at": "23:40"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["signal", "contracts", "vb"])
    ap.add_argument("--equity", type=float, default=0)
    ap.add_argument("--now", default=None)
    a = ap.parse_args()
    now = datetime.fromisoformat(a.now).replace(tzinfo=MSK) if a.now else datetime.now(MSK)
    res = []
    if a.cmd == "vb":
        for name, spec in CFG["vb"]["instruments"].items():
            try:
                res.append(vb_levels(name, spec, now))
            except Exception as e:
                res.append(dict(instrument=name, action="ERROR", error=repr(e)))
        print(json.dumps(res, ensure_ascii=False, indent=1, default=float))
        return
    for name in CFG["instruments"]:
        try:
            if a.cmd == "contracts":
                f = front_contract(CFG["instruments"][name]["prefix"], now.date())
                res.append(dict(instrument=name, ticker=f["ticker"], instrumentId=f"{f['ticker']}_SPBFUT",
                                expiration=f["expirationDate"][:10]))
            else:
                res.append(decide(name, now, a.equity))
        except Exception as e:  # report, never crash the run
            res.append(dict(instrument=name, action="ERROR", error=repr(e)))
    print(json.dumps(res, ensure_ascii=False, indent=1, default=float))


if __name__ == "__main__":
    sys.exit(main())
