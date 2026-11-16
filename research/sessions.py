"""Session returns of MOEX futures on global assets and their correlations (research only).

python3 research/sessions.py            # correlation report
Sessions, MSK, from hourly bars of the back-adjusted continuous contract:
  night  = last close of the previous trading day -> first open of the day (US after-hours + Asia open)
  asia   = first open of the day                  -> 10:00
  europe = 10:00                                   -> 16:00
  us     = 16:00                                   -> last close of the day (23:50)
Returns are log returns on the real price (adjusted price minus its roll shift), so rolls add nothing.
"""
from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MSK = timezone(timedelta(hours=3))
NAMES = {"BR": "BR", "WTI": "WT", "NG": "NG", "GOLD": "GD", "SILV": "SV", "PLT": "PT",
         "COPPER": "CE", "BTC": "BT", "NASD": "NA", "SPYF": "SF"}
SESS = ["night", "asia", "europe", "us"]


def load(prefix: str):
    p = ROOT / "data" / f"C_{prefix}_1h.json"
    if not p.exists():
        return None
    raw = json.loads(p.read_text())
    out = []
    for (t, o, h, l, c, v), s in zip(raw["bars"], raw["shifts"]):
        out.append((datetime.fromisoformat(t).astimezone(MSK), o - s, c - s, v))
    return out


def sessions(bars) -> dict[str, dict[str, float]]:
    """day -> {night, asia, europe, us, open, p10, p16, close} with log returns; weekdays only."""
    days: dict[str, list] = {}
    for t, o, c, v in bars:
        if t.weekday() >= 5:
            continue
        days.setdefault(t.strftime("%Y-%m-%d"), []).append((t, o, c))
    res, prev_close = {}, None
    for d in sorted(days):
        b = days[d]
        first_o = b[0][1]
        def price_at(hh):            # close of the last bar that ends by hh:00
            xs = [c for t, o, c in b if t.hour < hh]
            return xs[-1] if xs else None
        p10, p16, close = price_at(10), price_at(16), b[-1][2]
        r = {"open": first_o, "p10": p10, "p16": p16, "close": close, "first_hour": b[0][0].hour,
             "last_hour": b[-1][0].hour}
        ok = p10 and p16 and first_o > 0 and b[0][0].hour < 10 and b[-1][0].hour >= 20
        if ok:
            r["asia"] = math.log(p10 / first_o)
            r["europe"] = math.log(p16 / p10)
            r["us"] = math.log(close / p16)
            r["night"] = math.log(first_o / prev_close) if prev_close else None
            res[d] = r
        prev_close = close
    return res


def corr(xs, ys):
    pts = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    n = len(pts)
    if n < 30:
        return None, n, None
    mx = sum(p[0] for p in pts) / n; my = sum(p[1] for p in pts) / n
    sx = math.sqrt(sum((p[0] - mx) ** 2 for p in pts)); sy = math.sqrt(sum((p[1] - my) ** 2 for p in pts))
    if not sx or not sy:
        return None, n, None
    r = sum((p[0] - mx) * (p[1] - my) for p in pts) / (sx * sy)
    t = r * math.sqrt((n - 2) / max(1e-12, 1 - r * r))
    return r, n, t


def next_day(days: list[str], d: str):
    i = days.index(d)
    return days[i + 1] if i + 1 < len(days) else None


def pairs_report(S: dict, split: float = 2 / 3):
    """Lead-lag: session X of asset A -> the next session of asset B (same day, or the next day's
    night for X = us). Discovery on the first `split` of days, check on the rest."""
    order = {"night": ("asia", 0), "asia": ("europe", 0), "europe": ("us", 0), "us": ("night", 1)}
    rows = []
    for a, SA in S.items():
        for b, SB in S.items():
            common = sorted(set(SA) & set(SB))
            if len(common) < 120:
                continue
            cut = common[int(len(common) * split)]
            for x, (y, shift) in order.items():
                xs_in, ys_in, xs_out, ys_out = [], [], [], []
                for d in common:
                    dy = next_day(common, d) if shift else d
                    if not dy:
                        continue
                    xv, yv = SA[d].get(x), SB[dy].get(y)
                    (xs_in if d < cut else xs_out).append(xv)
                    (ys_in if d < cut else ys_out).append(yv)
                r1, n1, t1 = corr(xs_in, ys_in)
                r2, n2, t2 = corr(xs_out, ys_out)
                if r1 is None or r2 is None:
                    continue
                rows.append((a, x, b, y, r1, n1, t1, r2, n2, t2))
    return rows


def main():
    S = {}
    for name, prefix in NAMES.items():
        bars = load(prefix)
        if not bars:
            continue
        S[name] = sessions(bars)
        hrs = sorted({(r["first_hour"], r["last_hour"]) for r in S[name].values()})
        ds = sorted(S[name])
        print(f"{name:6s} {len(ds)} days {ds[0]}..{ds[-1]}  first/last bar hours {hrs[:3]}")
    # volatility by session
    print("\nSession volatility (st.dev of log return, %) and mean (%)")
    print("asset   " + "  ".join(f"{s:>14s}" for s in SESS))
    for a, SA in S.items():
        cells = []
        for s in SESS:
            v = [r[s] for r in SA.values() if r.get(s) is not None]
            m = sum(v) / len(v); sd = math.sqrt(sum((x - m) ** 2 for x in v) / (len(v) - 1))
            cells.append(f"{sd*100:6.2f} ({m*100:+.3f})")
        print(f"{a:7s} " + "  ".join(f"{c:>14s}" for c in cells))
    # same-session cross-asset correlations (europe and us)
    for s in ("asia", "europe", "us", "night"):
        print(f"\nSame-session correlation: {s}")
        names = list(S)
        print("       " + " ".join(f"{n[:6]:>6s}" for n in names))
        for a in names:
            line = []
            for b in names:
                common = sorted(set(S[a]) & set(S[b]))
                r, n, t = corr([S[a][d].get(s) for d in common], [S[b][d].get(s) for d in common])
                line.append(f"{r:6.2f}" if r is not None else "     -")
            print(f"{a[:6]:6s} " + " ".join(line))
    rows = pairs_report(S)
    print(f"\nLead-lag pairs tested: {len(rows)}. Significant in discovery (|t|>3) and same sign out of sample:")
    print(f"{'from':>14s} -> {'to':<14s} {'r_in':>6s} {'n':>4s} {'t_in':>5s} | {'r_out':>6s} {'n':>4s} {'t_out':>5s}")
    good = [r for r in rows if abs(r[6]) > 3 and r[4] * r[7] > 0]
    for a, x, b, y, r1, n1, t1, r2, n2, t2 in sorted(good, key=lambda r: -abs(r[6])):
        print(f"{a+'.'+x:>14s} -> {b+'.'+y:<14s} {r1:6.3f} {n1:4d} {t1:5.1f} | {r2:6.3f} {n2:4d} {t2:5.1f}")
    print("\nStrongest in discovery regardless of out-of-sample:")
    for a, x, b, y, r1, n1, t1, r2, n2, t2 in sorted(rows, key=lambda r: -abs(r[6]))[:15]:
        print(f"{a+'.'+x:>14s} -> {b+'.'+y:<14s} {r1:6.3f} {n1:4d} {t1:5.1f} | {r2:6.3f} {n2:4d} {t2:5.1f}")
    json.dump({k: v for k, v in S.items()}, open(ROOT / "data" / "sessions.json", "w"))


if __name__ == "__main__":
    main()
