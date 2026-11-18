"""Perpetual vs quarterly futures: can the spread between them be traded? (research only)

python3 research/basis.py [days] [interval]   # default 730 1h; history is cached in data/

For every pair the hourly relative basis b = Q / (m * P) - 1 is built from the front quarterly
contract Q (real prices, rolled 5 days before expiry) and the perpetual P (m converts units,
e.g. Si is quoted per 1000 USD). Fair basis drifts slowly to zero towards expiry; the
trade is mean reversion of b around its rolling mean:
  enter when b leaves mean +/- z * sd (short the rich leg, long the cheap one),
  exit when b returns to the mean, or after max_hold hours, or at the roll.
P&L is the change of b times the notional of one leg. Costs: 4 fills (2 legs x in/out),
each 0.04% fee + 1 price step of slippage. Funding of the perpetual is assumed to pay for
the carry that the quarterly loses (first-order fair), so it is not modelled.
"""
from __future__ import annotations

import math
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from robot.config import load  # noqa: E402
from robot.continuous import build, contracts  # noqa: E402
from robot.engine import Engine  # noqa: E402
from robot.market import load_history  # noqa: E402

MSK = timezone(timedelta(hours=3))
FEE = 0.0004
# perpetual, base asset, quarterly prefix
PAIRS = [
    ("CNYRUBF", "CNY/RUB", "CR"),
    ("USDRUBF", "USD/RUB", "Si"),
    ("EURRUBF", "EUR/RUB", "Eu"),
    ("GLDRUBF", "Золото в рублях", "GL"),
    ("IMOEXF", "Индекс московской биржи", "MM"),
    ("GAZPF", "GAZP", "GZ"),
    ("SBERF", "SBER", "SR"),
]
WINDOW = 120          # hours for the rolling mean / sd
Z_IN = (2.0, 3.0)
MAX_HOLD = 72         # hours


def series(c, perp: str, asset: str, prefix: str, days: int, interval: str = "1h"):
    p = {b.t: b for b in load_history(c, perp, interval, days)}
    qa, rolls, shifts = build(c, asset, prefix, interval, days)
    q = {b.t: (b, b.c - s) for b, s in zip(qa, shifts)}
    ts = sorted(t for t in q if t in p and p[t].v > 0 and q[t][0].v > 0)
    if not ts:
        return None
    ratio = statistics.median(q[t][1] / p[t].c for t in ts[:200])
    m = 10 ** round(math.log10(ratio))          # unit conversion: 1, 10, 100, 1000
    # front contract expiry per bar (rolled 5 days early)
    exps = [datetime.fromisoformat(f["expirationDate"].replace("Z", "+00:00")) for f in contracts(c, asset, prefix)]
    rows = []
    for t in ts:
        exp = next((e for e in exps if e - timedelta(days=5) > t), None)
        if exp is None:
            continue
        rows.append((t, q[t][1] / (m * p[t].c) - 1, exp))
    return rows, m, q[ts[-1]][1]


def backtest(rows, z_in: float, cost: float, delay: int = 0, window: int = WINDOW, max_hold: int = MAX_HOLD):
    """Mean reversion of the basis inside one front contract. Returns per-trade net P&L in bp."""
    out, hist, pos = [], [], None    # pos = (side, entry_b, entry_i, exp); out = [(time, net bp)]
    s1 = s2 = 0.0                    # running sums of the last `window` basis values

    def fill(j: int) -> float:  # the basis we actually get: `delay` bars after the signal, same contract
        k = min(j + delay, len(rows) - 1)
        return rows[k][1] if rows[k][2] == rows[j][2] else rows[j][1]
    for i, (t, b, exp) in enumerate(rows):
        if hist and hist[-1][1] != exp:          # roll: new contract, new history, close
            s1 = s2 = 0.0
            if pos:
                out.append((t, pos[0] * (b_prev - pos[1]) * 1e4 - cost))
                pos = None
            hist = []
        b_prev = b
        hist.append((b, exp))
        s1 += b; s2 += b * b
        if len(hist) > window:
            old = hist[-window - 1][0]
            s1 -= old; s2 -= old * old
        if len(hist) < window:
            continue
        mu = s1 / window
        sd = math.sqrt(max(s2 / window - mu * mu, 0.0))
        if pos:
            side, b0, i0, _ = pos
            if (side == 1 and b >= mu) or (side == -1 and b <= mu) or i - i0 >= max_hold:
                out.append((rows[i][0], side * (fill(i) - b0) * 1e4 - cost))
                pos = None
        elif sd > 0 and abs(b - mu) > z_in * sd and i + delay < len(rows) and rows[i + delay][2] == exp:
            pos = (1 if b < mu else -1, fill(i), i, exp)   # long the basis when it is too low
    return out


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 730
    interval = sys.argv[2] if len(sys.argv) > 2 else "1h"
    k = {"1h": 1, "15m": 4, "5m": 12, "1m": 60}[interval]     # bars per hour
    cfg = load()
    c = Engine(cfg, read_only=True).c
    print("Net of costs, bp of one leg's notional. Base: z=3, window 120h. delay 1 = fill one bar after the signal.")
    print(f"{'pair':16} {'basis':>6} {'cost':>5} | {'d=0 n':>5} {'net':>6} | {'d=1 n':>5} {'net':>6} {'/trade':>6} "
          f"{'1st half':>8} {'2nd half':>8} | grid d=1 >0")
    for perp, asset, prefix in PAIRS:
        try:
            r = series(c, perp, asset, prefix, days, interval)
        except Exception as e:  # missing history for a pair should not stop the report
            print(f"{perp}/{prefix}: {type(e).__name__}: {e}")
            continue
        if not r:
            print(f"{perp}/{prefix}: no overlapping history")
            continue
        rows, m, px_q = r
        q_step = {"CR": 0.001, "Si": 1, "Eu": 1, "GL": 0.1, "MM": 0.05, "GZ": 1, "SR": 1}[prefix]
        # cost in bp of one leg's notional: 4 fills x (fee + one price step)
        cost = 4 * FEE * 1e4 + 4 * q_step / px_q * 1e4
        mid = rows[len(rows) // 2][0]
        t0 = backtest(rows, 3.0, cost, delay=0, window=120 * k, max_hold=72 * k)
        t1 = backtest(rows, 3.0, cost, delay=1, window=120 * k, max_hold=72 * k)
        h1 = sum(x for t, x in t1 if t < mid); h2 = sum(x for t, x in t1 if t >= mid)
        grid = [sum(x for _, x in backtest(rows, z, cost, delay=1, window=w * k, max_hold=72 * k)) > 0
                for z in (2.5, 3.0, 3.5) for w in (60, 120, 240)]
        n1 = len(t1); net1 = sum(x for _, x in t1)
        print(f"{perp + '/' + prefix:16} {rows[-1][1] * 1e4:>5.0f}  {cost:>5.1f} | {len(t0):>5} {sum(x for _, x in t0):>6.0f} | "
              f"{n1:>5} {net1:>6.0f} {(net1 / n1 if n1 else 0):>6.1f} {h1:>8.0f} {h2:>8.0f} | {sum(grid)}/9")


if __name__ == "__main__":
    main()
