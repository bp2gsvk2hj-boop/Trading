"""Back-adjusted continuous history for expiring futures (for backtests).

Each contract is the front from the previous front's roll date until `roll_days`
before its own expiration. At every roll the older history is shifted by the
price gap between the two contracts (additive "Panama" adjustment), so channel
breakouts are not triggered by the roll itself. P&L per point is preserved;
the extra fees for rolling an open position are added by the backtest caller
using the returned roll times.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from .api import Client
from .market import DATA_DIR, Candle, fetch_candles


def contracts(client: Client, asset: str, prefix: str) -> list[dict]:
    fs = client.call("InstrumentsService/Futures", {"instrumentStatus": "INSTRUMENT_STATUS_ALL"})["instruments"]
    out = [f for f in fs if f.get("basicAsset") == asset and f["ticker"].startswith(prefix)
           and not f["expirationDate"].startswith("2099")]
    return sorted(out, key=lambda f: f["expirationDate"])


def build(client: Client | None, asset: str, prefix: str, interval: str = "1h", days: int = 365 * 6,
          roll_days: int = 5, refresh: bool = False) -> tuple[list[Candle], list[datetime], list[float]]:
    """Return (adjusted candles, roll times, per-bar shift). Real price = adjusted - shift.
    Cached in data/C_<prefix>_<interval>.json."""
    DATA_DIR.mkdir(exist_ok=True)
    path = DATA_DIR / f"C_{prefix}_{interval}.json"
    if path.exists() and not refresh:
        raw = json.loads(path.read_text())
        return ([Candle(datetime.fromisoformat(r[0]), *r[1:]) for r in raw["bars"]],
                [datetime.fromisoformat(t) for t in raw["rolls"]], raw["shifts"])
    if client is None:
        raise FileNotFoundError(path)
    now = datetime.now(timezone.utc)
    out, rolls, shifts = stitch(contracts(client, asset, prefix), client, interval,
                                now - timedelta(days=days), now, roll_days)
    path.write_text(json.dumps({"bars": [[c.t.isoformat(), c.o, c.h, c.l, c.c, c.v] for c in out],
                                "rolls": [t.isoformat() for t in rolls], "shifts": shifts}))
    return out, rolls, shifts


def stitch(futs: list[dict], client: Client, interval: str, start: datetime, now: datetime,
           roll_days: int = 5) -> tuple[list[Candle], list[datetime], list[float]]:
    """Glue contracts (sorted by expiration) into one back-adjusted series over [start, now]."""
    segs: list[list[Candle]] = []
    gaps: list[float] = []          # gaps[i]: new contract minus old one at roll i
    seg_from = start
    prev: list[Candle] | None = None
    for f in futs:
        exp = datetime.fromisoformat(f["expirationDate"].replace("Z", "+00:00"))
        seg_to = min(exp - timedelta(days=roll_days), now)
        if seg_to <= seg_from:
            continue
        # fetch a few days before the roll to measure the gap on the same bar
        bars = fetch_candles(client, f["uid"], interval, seg_from - timedelta(days=4), seg_to)
        seg = [b for b in bars if b.t >= seg_from]
        if seg:
            if prev:
                last = prev[-1]
                same = [b for b in bars if b.t <= last.t]
                ref = same[-1].c if same else seg[0].o
                gaps.append(ref - last.c)
            segs.append(seg)
            prev = seg
        seg_from = seg_to
        if seg_to >= now:
            break
    # Panama adjustment: the newest segment keeps real prices, older ones are shifted.
    out: list[Candle] = []
    shifts: list[float] = []
    rolls: list[datetime] = []
    shift = 0.0
    for i in range(len(segs) - 1, -1, -1):
        seg = segs[i]
        out = [Candle(b.t, b.o + shift, b.h + shift, b.l + shift, b.c + shift, b.v) for b in seg] + out
        shifts = [shift] * len(seg) + shifts
        if i > 0:
            shift += gaps[i - 1]
            rolls.append(seg[0].t)
    rolls.sort()
    return out, rolls, shifts


def recent(client: Client, asset: str, prefix: str, interval: str, start: datetime, now: datetime,
           roll_days: int = 5) -> list[Candle]:
    """Uncached continuous series for live trading; the current front keeps real prices."""
    futs = [f for f in contracts(client, asset, prefix)
            if datetime.fromisoformat(f["expirationDate"].replace("Z", "+00:00")) > start]
    return stitch(futs, client, interval, start, now, roll_days)[0]
