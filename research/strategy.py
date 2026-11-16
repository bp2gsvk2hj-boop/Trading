"""Overnight-to-morning continuation on MOEX futures (research backtest).

The only lead-lag that survived out of sample (research/sessions.py) is: the overnight move of
global assets (from the MOEX close at 23:50 to its morning open) continues during the thin
MOEX morning session. The strategy: at the morning open, measure the overnight return of a
signal basket, enter the target in that direction after the first hour (the open itself is too
thin to trade on), exit at 10:00 or 16:00 MSK.

Costs per round trip: 2 x 0.04% broker+exchange fee plus 2 x `slip` of price.
Discovery on the first two thirds of days, check on the last third.
"""
from __future__ import annotations

import math
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from research.sessions import NAMES, load  # noqa: E402

FEE = 0.0004
SLIP = {"GOLD": 0.0002, "SILV": 0.0004, "PLT": 0.0005, "COPPER": 0.0004, "NASD": 0.0002,
        "SPYF": 0.0002, "BR": 0.0002, "NG": 0.0005, "BTC": 0.0004}


def daily(bars):
    """day -> dict(open, h1 = close of the first full hour, p10, p16, close, prev_close)."""
    days = {}
    for t, o, c, v in bars:
        if t.weekday() >= 5:
            continue
        days.setdefault(t.strftime("%Y-%m-%d"), []).append((t, o, c))
    out, prev = {}, None
    for d in sorted(days):
        b = days[d]
        at = lambda hh: ([c for t, o, c in b if t.hour < hh] or [None])[-1]
        if b[0][0].hour < 9 and b[-1][0].hour >= 20 and prev:
            out[d] = {"open": b[0][1], "h1": b[0][2], "h2": b[1][2] if len(b) > 1 else None,
                      "p10": at(10), "p16": at(16), "close": b[-1][2], "prev": prev,
                      "first": b[0][0].hour}
        prev = b[-1][2]
    return out


def run(D, signal: list[str], target: str, entry: str, exit_: str, k: float, window: int = 60):
    """Trades: list of (day, net return). k = threshold in rolling st.devs of the signal."""
    days = sorted(set.intersection(*(set(D[s]) for s in signal + [target])))
    hist, trades = [], []
    for d in days:
        sig = sum(math.log(D[s][d]["open"] / D[s][d]["prev"]) for s in signal) / len(signal)
        sd = math.sqrt(sum(x * x for x in hist[-window:]) / len(hist[-window:])) if len(hist) >= 20 else None
        hist.append(sig)
        if sd is None or abs(sig) < k * sd:
            continue
        T = D[target][d]
        pe, px = T.get(entry), T.get(exit_)
        if not pe or not px:
            continue
        pos = 1 if sig > 0 else -1
        net = pos * math.log(px / pe) - 2 * (FEE + SLIP.get(target, 0.0004))
        trades.append((d, net))
    return trades


def stats(tr):
    if len(tr) < 10:
        return None
    r = [x for _, x in tr]
    m = sum(r) / len(r); sd = math.sqrt(sum((x - m) ** 2 for x in r) / (len(r) - 1)) or 1e-9
    return {"n": len(r), "mean": m, "hit": sum(x > 0 for x in r) / len(r), "t": m / sd * math.sqrt(len(r)),
            "sum": sum(r)}


def main():
    D = {n: daily(b) for n, p in NAMES.items() if (b := load(p)) and n != "WTI"}
    metals = ["GOLD", "SILV", "PLT"]
    tests = []
    for target in ["COPPER", "PLT", "SILV", "GOLD", "NASD", "SPYF", "BTC", "BR"]:
        for name, sig in [("own", [target]), ("metals", metals), ("index", ["NASD", "SPYF"])]:
            for entry in ["open", "h1", "h2"]:
                for ex in ["p10", "p16"]:
                    for k in [0.0, 0.5, 1.0]:
                        tr = run(D, sig, target, entry, ex, k)
                        if len(tr) < 30:
                            continue
                        cut = tr[int(len(tr) * 2 / 3)][0]
                        a, b = stats([x for x in tr if x[0] < cut]), stats([x for x in tr if x[0] >= cut])
                        if a and b:
                            tests.append((target, name, entry, ex, k, a, b))
    print(f"{len(tests)} variants. Net of costs, % per trade. Discovery | check (last third)")
    print(f"{'target':7s} {'signal':7s} {'in':5s} {'out':4s} {'k':>3s} | {'n':>4s} {'mean%':>7s} {'hit':>5s} {'t':>5s} | {'n':>4s} {'mean%':>7s} {'hit':>5s} {'t':>5s}")
    for target, name, entry, ex, k, a, b in sorted(tests, key=lambda x: -x[5]["t"])[:25]:
        print(f"{target:7s} {name:7s} {entry:5s} {ex:4s} {k:3.1f} | {a['n']:4d} {a['mean']*100:7.3f} {a['hit']:5.2f} {a['t']:5.1f} | "
              f"{b['n']:4d} {b['mean']*100:7.3f} {b['hit']:5.2f} {b['t']:5.1f}")
    print("\nEntry at the open is not tradeable in practice (thin book, the robot needs a pass); 'h1'/'h2' are.")


if __name__ == "__main__":
    main()
