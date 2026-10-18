"""Latency-aware execution on 1-min bars.

Strategies emit *intents* computed from completed bars; the engine fills them on 1-min
data as a slow client would:
  * market entries/exits: open of the first 1-min bar starting >= decision + delay,
    +/- `slip` ticks;
  * stop-loss and stop-entry orders live at the broker (no client latency), filled at
    the worse of level and bar open, +/- `stop_slip` ticks;
  * take-profit limit orders fill only if price trades *through* the level by a tick;
  * if stop and target are inside the same 1-min bar, the stop is assumed first;
  * anything still open is closed at the session cutoff (`flat_tod`) at market.
Commission: COMMISSION per side on notional.
"""
import numpy as np
import pandas as pd
from common import COMMISSION, SPECS, load_1m


class Engine:
    def __init__(self, name, delay=2, slip=1, stop_slip=2, flat_tod=1420):
        m = load_1m(name)
        m = m[m.time.dt.weekday < 5].sort_values("time")
        self.name, self.tick = name, SPECS[name]["tick"]
        self.delay, self.slip, self.stop_slip, self.flat_tod = delay, slip, stop_slip, flat_tod
        self.t = m.time.values.astype("datetime64[m]")
        self.o, self.h, self.l, self.c = (m[k].values for k in ("open", "high", "low", "close"))
        day = m.time.dt.normalize().values
        self.day_start = {}
        self.day_end = {}
        idx = np.flatnonzero(np.r_[True, day[1:] != day[:-1]])
        for a, b in zip(idx, np.r_[idx[1:], len(day)]):
            self.day_start[pd.Timestamp(day[a])] = a
            self.day_end[pd.Timestamp(day[a])] = b

    def _bar_at(self, date, ts, lo, hi):
        """index of first bar in [lo, hi) with time >= ts."""
        i = lo + np.searchsorted(self.t[lo:hi], np.datetime64(ts, "m"))
        return i if i < hi else None

    def run(self, intents):
        """intent keys: date, time (decision ts), side, [stop], [target], [exit_time],
        [entry_stop] (stop-entry level: fill when traded through, no latency)."""
        out = []
        tick = self.tick
        for it in intents:
            date = pd.Timestamp(it["date"])
            if date not in self.day_start:
                continue
            lo, hi = self.day_start[date], self.day_end[date]
            cutoff = date + pd.Timedelta(minutes=self.flat_tod)
            hi_cut = self._bar_at(date, cutoff, lo, hi) or hi
            side = it["side"]
            if it.get("entry_limit") is not None:  # resting limit entry: fill only if traded through
                i0 = self._bar_at(date, it["time"], lo, hi_cut)
                if i0 is None:
                    continue
                lvl, i = it["entry_limit"], i0
                last_entry = self._bar_at(date, it.get("entry_until", cutoff), lo, hi_cut) or hi_cut
                while i < last_entry and not ((side == 1 and self.l[i] < lvl) or (side == -1 and self.h[i] > lvl)):
                    i += 1
                if i >= last_entry:
                    continue
                ep = (min(self.o[i], lvl) if side == 1 else max(self.o[i], lvl))
                ei = i
            elif it.get("entry_stop") is not None:  # resting stop-entry from decision time
                i0 = self._bar_at(date, it["time"], lo, hi_cut)
                if i0 is None:
                    continue
                lvl, i = it["entry_stop"], i0
                last_entry = self._bar_at(date, it.get("entry_until", cutoff), lo, hi_cut) or hi_cut
                while i < last_entry and not ((side == 1 and self.h[i] >= lvl) or (side == -1 and self.l[i] <= lvl)):
                    i += 1
                if i >= last_entry:
                    continue
                ep = (max(self.o[i], lvl) if side == 1 else min(self.o[i], lvl)) + side * self.stop_slip * tick
                ei = i
            else:
                ei = self._bar_at(date, it["time"] + pd.Timedelta(minutes=self.delay), lo, hi_cut)
                if ei is None:
                    continue
                ep = self.o[ei] + side * self.slip * tick
            stop, target = it.get("stop"), it.get("target")
            xt = it.get("exit_time")
            xi = self._bar_at(date, xt + pd.Timedelta(minutes=self.delay), lo, hi_cut) if xt is not None else None
            end = xi if xi is not None else hi_cut
            xp, reason, j = None, "eod", ei
            for j in range(ei, end):
                if stop is not None and ((side == 1 and self.l[j] <= stop) or (side == -1 and self.h[j] >= stop)):
                    base = min(self.o[j], stop) if side == 1 else max(self.o[j], stop)
                    if j == ei:  # stopped within the entry bar: cannot be better than stop
                        base = stop
                    xp, reason = base - side * self.stop_slip * tick, "stop"
                    break
                if target is not None and ((side == 1 and self.h[j] > target) or (side == -1 and self.l[j] < target)):
                    xp, reason = (max(self.o[j], target) if side == 1 else min(self.o[j], target)) if j > ei else target, "target"
                    break
            if xp is None:
                if xi is not None:
                    j, xp, reason = xi, self.o[xi] - side * self.slip * tick, "exit"
                else:
                    j = end - 1
                    xp, reason = self.c[j] - side * self.slip * tick, "eod"
            ret = side * (xp - ep) / ep - COMMISSION * (1 + xp / ep)
            out.append((date, side, ep, xp, str(self.t[ei])[11:16], str(self.t[j])[11:16], reason, ret,
                        it.get("tag", ""), stop))
        return pd.DataFrame(out, columns=["date", "side", "entry", "exit", "et", "xt", "reason", "ret", "tag",
                                          "stop"])
