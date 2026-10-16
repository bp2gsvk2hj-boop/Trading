"""Bar-close strategies.

Each strategy is split in two so the same code runs in backtests and live:
  prepare(bars)          -> indicator arrays, computed once
  decide(i, ind, pos, st) -> Action for the bar that just closed at index i

Indicators at index i only use bars <= i (channels exclude bar i), so there is
no look-ahead. Orders are executed after the bar closes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone

from . import indicators as ta
from .market import INTERVALS, Candle

MSK = timezone(timedelta(hours=3))


@dataclass
class Position:
    side: int = 0            # 1 long, -1 short, 0 flat
    qty: int = 0
    entry: float = 0.0
    stop: float | None = None
    entry_time: datetime | None = None
    bars_held: int = 0


@dataclass
class Action:
    kind: str                # hold | enter | exit | stop
    side: int = 0            # for enter
    stop: float | None = None
    reason: str = ""


HOLD = Action("hold")


def parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


class Strategy:
    name = "base"
    intraday = False         # flatten before the end of the main session
    defaults: dict = {}

    def __init__(self, interval: str, **params):
        unknown = set(params) - set(self.defaults)
        if unknown:
            raise ValueError(f"{self.name}: unknown params {sorted(unknown)}")
        self.interval = interval
        self.bar = INTERVALS[interval][2]
        self.p = {**self.defaults, **params}

    @property
    def warmup(self) -> int:
        return 50

    def close_time(self, c: Candle) -> datetime:
        return c.t + self.bar

    def prepare(self, bars: list[Candle]) -> dict:
        raise NotImplementedError

    def decide(self, i: int, ind: dict, pos: Position, st: dict) -> Action:
        raise NotImplementedError


class DonchianTrend(Strategy):
    """Channel breakout with a trailing channel/ATR stop. Holds overnight.

    Long when the close breaks the highest high of `entry_n` bars (short: lowest low).
    Initial stop: `stop_atr` ATRs. Trailing stop: the opposite `exit_n`-bar channel.
    Optional trend filter: only trade in the direction of EMA(`trend_ema`).
    """
    name = "trend"
    defaults = {"entry_n": 20, "exit_n": 10, "atr_n": 20, "stop_atr": 2.0, "trend_ema": 0, "allow_short": True}

    @property
    def warmup(self) -> int:
        return max(self.p["entry_n"], self.p["atr_n"], self.p["trend_ema"]) + 5

    def prepare(self, bars):
        h = [b.h for b in bars]; l = [b.l for b in bars]; c = [b.c for b in bars]
        return {
            "bars": bars, "c": c,
            "hh": ta.highest(h, self.p["entry_n"]), "ll": ta.lowest(l, self.p["entry_n"]),
            "xh": ta.highest(h, self.p["exit_n"] - 1) if self.p["exit_n"] > 1 else h,
            "xl": ta.lowest(l, self.p["exit_n"] - 1) if self.p["exit_n"] > 1 else l,
            "atr": ta.atr(h, l, c, self.p["atr_n"]),
            "ema": ta.ema(c, self.p["trend_ema"]) if self.p["trend_ema"] else None,
        }

    def decide(self, i, ind, pos, st):
        c, a = ind["c"][i], ind["atr"][i]
        if a is None or ind["hh"][i] is None:
            return HOLD
        # Trailing channel includes the bar that just closed.
        xl = min(ind["xl"][i], ind["bars"][i].l) if ind["xl"][i] is not None else None
        xh = max(ind["xh"][i], ind["bars"][i].h) if ind["xh"][i] is not None else None
        if pos.side == 1:
            new = max(pos.stop or -1e18, xl) if xl is not None else pos.stop
            if c <= new:
                return Action("exit", reason="trail stop hit at close")
            return Action("stop", stop=new) if new != pos.stop else HOLD
        if pos.side == -1:
            new = min(pos.stop or 1e18, xh) if xh is not None else pos.stop
            if c >= new:
                return Action("exit", reason="trail stop hit at close")
            return Action("stop", stop=new) if new != pos.stop else HOLD
        e = ind["ema"][i] if ind["ema"] else None
        if c > ind["hh"][i] and (e is None or c > e):
            return Action("enter", side=1, stop=c - self.p["stop_atr"] * a, reason=f"breakout > {ind['hh'][i]:g}")
        if self.p["allow_short"] and c < ind["ll"][i] and (e is None or c < e):
            return Action("enter", side=-1, stop=c + self.p["stop_atr"] * a, reason=f"breakdown < {ind['ll'][i]:g}")
        return HOLD


class BollingerReversion(Strategy):
    """Intraday mean reversion in a range regime.

    Enter against a close outside the Bollinger band only while ADX is low
    (no trend). Exit at the middle band, on the ATR stop, or after `max_bars`.
    """
    name = "meanrev"
    intraday = True
    defaults = {"n": 20, "k": 2.0, "adx_n": 14, "adx_max": 20.0, "atr_n": 14, "stop_atr": 1.5, "max_bars": 16}

    @property
    def warmup(self) -> int:
        return max(self.p["n"], 3 * self.p["adx_n"], self.p["atr_n"]) + 5

    def prepare(self, bars):
        h = [b.h for b in bars]; l = [b.l for b in bars]; c = [b.c for b in bars]
        return {
            "c": c, "mid": ta.sma(c, self.p["n"]), "sd": ta.stdev(c, self.p["n"]),
            "adx": ta.adx(h, l, c, self.p["adx_n"]), "atr": ta.atr(h, l, c, self.p["atr_n"]),
        }

    def decide(self, i, ind, pos, st):
        c, mid, sd, adx, a = ind["c"][i], ind["mid"][i], ind["sd"][i], ind["adx"][i], ind["atr"][i]
        if None in (mid, sd, adx, a):
            return HOLD
        if pos.side == 1:
            if c >= mid:
                return Action("exit", reason="reached middle band")
            if pos.bars_held >= self.p["max_bars"]:
                return Action("exit", reason="time stop")
            return HOLD
        if pos.side == -1:
            if c <= mid:
                return Action("exit", reason="reached middle band")
            if pos.bars_held >= self.p["max_bars"]:
                return Action("exit", reason="time stop")
            return HOLD
        if adx >= self.p["adx_max"]:
            return HOLD
        lo, hi = mid - self.p["k"] * sd, mid + self.p["k"] * sd
        if c < lo:
            return Action("enter", side=1, stop=c - self.p["stop_atr"] * a, reason=f"close < lower band, ADX {adx:.0f}")
        if c > hi:
            return Action("enter", side=-1, stop=c + self.p["stop_atr"] * a, reason=f"close > upper band, ADX {adx:.0f}")
        return HOLD


class OpeningRangeBreakout(Strategy):
    """Breakout of the first `range_minutes` of the main session (10:00 MSK).

    One trade per day. Stop at the opposite side of the range (capped at
    `max_stop_atr` ATRs), target `target_r` x risk, flat at the session end.
    """
    name = "orb"
    intraday = True
    defaults = {"session_start": "10:00", "range_minutes": 60, "last_entry": "13:30",
                "target_r": 2.0, "atr_n": 14, "max_stop_atr": 2.0, "allow_short": True}

    @property
    def warmup(self) -> int:
        return self.p["atr_n"] + 5

    def prepare(self, bars):
        h = [b.h for b in bars]; l = [b.l for b in bars]; c = [b.c for b in bars]
        start = parse_hhmm(self.p["session_start"])
        rng_end = (datetime.combine(datetime.min, start) + timedelta(minutes=self.p["range_minutes"])).time()
        # Opening range per MSK date, using only bars fully inside [start, rng_end).
        orh: dict = {}; orl: dict = {}
        for b in bars:
            t0 = b.t.astimezone(MSK)
            t1 = (b.t + self.bar).astimezone(MSK)
            if t0.time() >= start and t1.time() <= rng_end and t1.date() == t0.date():
                d = t0.date()
                orh[d] = max(orh.get(d, b.h), b.h)
                orl[d] = min(orl.get(d, b.l), b.l)
        return {"bars": bars, "c": c, "atr": ta.atr(h, l, c, self.p["atr_n"]), "orh": orh, "orl": orl,
                "rng_end": rng_end, "last_entry": parse_hhmm(self.p["last_entry"])}

    def decide(self, i, ind, pos, st):
        b, c, a = ind["bars"][i], ind["c"][i], ind["atr"][i]
        now = self.close_time(b).astimezone(MSK)
        d = now.date()
        if pos.side:
            target = pos.entry + self.p["target_r"] * (pos.entry - pos.stop) if pos.stop is not None else None
            if target is not None and (c - target) * pos.side >= 0:
                return Action("exit", reason="target reached")
            return HOLD
        if a is None or d not in ind["orh"] or st.get("orb_day") == str(d):
            return HOLD
        if not (ind["rng_end"] <= now.time() <= ind["last_entry"]):
            return HOLD
        hi, lo = ind["orh"][d], ind["orl"][d]
        cap = self.p["max_stop_atr"] * a
        if c > hi:
            st["orb_day"] = str(d)
            return Action("enter", side=1, stop=max(lo, c - cap), reason=f"break above OR {hi:g}")
        if self.p["allow_short"] and c < lo:
            st["orb_day"] = str(d)
            return Action("enter", side=-1, stop=min(hi, c + cap), reason=f"break below OR {lo:g}")
        return HOLD


STRATEGIES = {cls.name: cls for cls in (DonchianTrend, BollingerReversion, OpeningRangeBreakout)}


def make(name: str, interval: str, params: dict | None = None) -> Strategy:
    return STRATEGIES[name](interval, **(params or {}))
