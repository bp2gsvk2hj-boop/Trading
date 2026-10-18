"""Signal generators producing intents for engine.Engine.

anchored_drive: momentum after an anchor time (session open, US cash open, data release).
vol_breakout:   resting stop-entry at anchor price +/- k * average daily range.
"""
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd
from common import load_1m

NY, MSK = ZoneInfo("America/New_York"), ZoneInfo("Europe/Moscow")


def ny_to_msk(date, hh, mm):
    t = datetime.combine(date.date(), time(hh, mm), NY).astimezone(MSK)
    return pd.Timestamp(t.replace(tzinfo=None))


ANCHORS = {
    "msk10": lambda d: d + pd.Timedelta(hours=10),
    "us_open": lambda d: ny_to_msk(d, 9, 30),
    "us_data": lambda d: ny_to_msk(d, 8, 30),                                # CPI/NFP/claims hour
    "eia_oil": lambda d: ny_to_msk(d, 10, 30) if d.weekday() == 2 else None,  # Wed crude stocks
    "eia_gas": lambda d: ny_to_msk(d, 10, 30) if d.weekday() == 3 else None,  # Thu gas storage
    "nfp": lambda d: ny_to_msk(d, 8, 30) if d.weekday() == 4 and d.day <= 7 else None,
}


class Minute:
    """1-min arrays with per-day index, cached per instrument."""
    _cache = {}

    def __new__(cls, name):
        if name in cls._cache:
            return cls._cache[name]
        self = super().__new__(cls)
        m = load_1m(name)
        m = m[m.time.dt.weekday < 5].sort_values("time").reset_index(drop=True)
        self.t = m.time.values.astype("datetime64[m]")
        self.o, self.h, self.l, self.c = (m[k].values for k in ("open", "high", "low", "close"))
        self.v = m.volume.values
        self.contract = m.contract.values
        day = m.time.dt.normalize().values
        idx = np.flatnonzero(np.r_[True, day[1:] != day[:-1]])
        self.days = [(pd.Timestamp(day[a]), a, b) for a, b in zip(idx, np.r_[idx[1:], len(day)])]
        cls._cache[name] = self
        return self

    def at(self, a, b, ts):
        i = a + np.searchsorted(self.t[a:b], np.datetime64(ts, "m"))
        return i if i < b else None


def anchored_drive(name, anchor="msk10", k=2.0, checks=(60, 120, 180, 240, 300), lookback=14,
                   stop="anchor", exit_after=None, cutoff_tod=1415, min_hist=7, ref="open"):
    """One trade/day: first check where |price/ref - 1| > k * sigma(offset) -> enter.

    sigma(offset) = mean |move| at that offset over the previous `lookback` anchor days.
    stop: 'anchor' -> stop at ref price; None -> no stop.
    exit_after: minutes after entry decision to exit (None = session cutoff).
    ref: 'open' (open of the anchor minute) or 'prev' (close of the minute before anchor).
    """
    M = Minute(name)
    afn = ANCHORS[anchor]
    hist = []          # rows of abs moves per offset for previous anchor days
    intents = []
    for date, a, b in M.days:
        at = afn(date)
        if at is None:
            continue
        i0 = M.at(a, b, at)
        if i0 is None or (M.t[i0] - np.datetime64(at, "m")) > np.timedelta64(5, "m"):
            continue
        refp = M.o[i0] if ref == "open" or i0 == a else M.c[i0 - 1]
        moves = []
        for off in checks:
            j = M.at(a, b, at + pd.Timedelta(minutes=off))  # first bar at/after check time
            moves.append(np.nan if j is None or j == 0 else M.c[j - 1] / refp - 1)  # close of bar ending at check
        moves = np.array(moves)
        if len(hist) >= min_hist:
            sig = np.nanmean(np.abs(np.array(hist[-lookback:])), axis=0)
            for off, mv, s in zip(checks, moves, sig):
                ct = at + pd.Timedelta(minutes=off)
                if np.isnan(mv) or np.isnan(s) or (ct.hour * 60 + ct.minute) > cutoff_tod - 10:
                    continue
                if abs(mv) > k * s:
                    side = int(np.sign(mv))
                    it = dict(date=date, time=ct, side=side, tag=f"{anchor}+{off}")
                    if stop == "anchor":
                        it["stop"] = refp
                    if exit_after:
                        it["exit_time"] = ct + pd.Timedelta(minutes=exit_after)
                    intents.append(it)
                    break
        hist.append(moves)
    return intents


def vol_breakout(name, k=0.5, anchor_tod=600, last_entry_tod=900, n_range=10, stop_k=None):
    """Resting stop-entry orders at O +/- k * avg range (previous n days, anchor..cutoff).

    Whichever side trades first is taken (one trade/day); stop at O (or entry -/+ stop_k*range).
    """
    M = Minute(name)
    ranges, intents = [], []
    for date, a, b in M.days:
        at = date + pd.Timedelta(minutes=anchor_tod)
        i0 = M.at(a, b, at)
        if i0 is None or (M.t[i0] - np.datetime64(at, "m")) > np.timedelta64(5, "m"):
            continue
        O = M.o[i0]
        if len(ranges) >= n_range:
            R = np.mean(ranges[-n_range:]) * O
            up, dn = O + k * R, O - k * R
            last = M.at(a, b, date + pd.Timedelta(minutes=last_entry_tod)) or b
            hi, lo = M.h[i0:last], M.l[i0:last]
            iu = np.argmax(hi >= up) if (hi >= up).any() else None
            idn = np.argmax(lo <= dn) if (lo <= dn).any() else None
            side = None
            if iu is not None and (idn is None or iu < idn):
                side, lvl = 1, up
            elif idn is not None and (iu is None or idn < iu):
                side, lvl = -1, dn
            if side:
                stp = O if stop_k is None else lvl - side * stop_k * R
                intents.append(dict(date=date, time=at, side=side, entry_stop=lvl, stop=stp,
                                    entry_until=date + pd.Timedelta(minutes=last_entry_tod),
                                    tag="vb"))
        hi_d, lo_d = M.h[i0:b].max(), M.l[i0:b].min()
        ranges.append((hi_d - lo_d) / O)
    return intents


def range_fade(name, k=1.0, anchor_tod=600, last_entry_tod=1080, n_range=10, stop_k=0.5,
               target="open"):
    """Limit orders at O +/- k*R fading the move; stop at level +/- stop_k*R; target O or half-way."""
    M = Minute(name)
    ranges, intents = [], []
    for date, a, b in M.days:
        at = date + pd.Timedelta(minutes=anchor_tod)
        i0 = M.at(a, b, at)
        if i0 is None or (M.t[i0] - np.datetime64(at, "m")) > np.timedelta64(5, "m"):
            continue
        O = M.o[i0]
        if len(ranges) >= n_range:
            R = np.mean(ranges[-n_range:]) * O
            up, dn = O + k * R, O - k * R
            last = M.at(a, b, date + pd.Timedelta(minutes=last_entry_tod)) or b
            hi, lo = M.h[i0:last], M.l[i0:last]
            iu = np.argmax(hi > up) if (hi > up).any() else None
            idn = np.argmax(lo < dn) if (lo < dn).any() else None
            side = None
            if iu is not None and (idn is None or iu < idn):
                side, lvl = -1, up
            elif idn is not None and (iu is None or idn < iu):
                side, lvl = 1, dn
            if side:
                tgt = O if target == "open" else (lvl + O) / 2
                intents.append(dict(date=date, time=at, side=side, entry_limit=lvl,
                                    stop=lvl - side * stop_k * R, target=tgt,
                                    entry_until=date + pd.Timedelta(minutes=last_entry_tod), tag="fade"))
        ranges.append((M.h[i0:b].max() - M.l[i0:b].min()) / O)
    return intents
