"""Intraday strategy simulators on 5-min bars.

Conventions (conservative):
  * signal on bar close, fill at next bar open +/- 1 tick slippage;
  * stops fill at the worse of stop price and bar open, -1 tick;
  * if stop and target are hit in the same bar the stop is assumed first;
  * every position is closed at the close of the last bar of the session.
"""
import numpy as np
import pandas as pd
from common import COMMISSION, SLIP_TICKS, SPECS, load_bars

BAR = 5


def prepare(name, start_tod=600, end_tod=1125, lookback=14, minutes=BAR):
    """Split bars into per-day arrays with VWAP, noise sigma and previous close.

    start_tod/end_tod: minutes since midnight (MSK) of first/last bar *start*.
    """
    bars = load_bars(name, minutes)
    tick = SPECS[name]["tick"]
    # previous close and contract of the previous trading day (whole day incl. evening)
    last = bars.groupby("date").agg(pc=("close", "last"), pcontract=("contract", "last"))
    last = last.shift(1)
    # rolling features on continuous series (for channel strategies)
    b = bars.copy()
    sess = b[(b.tod >= start_tod) & (b.tod <= end_tod)].copy()
    first_open = sess.groupby("date")["open"].transform("first")
    sess["move"] = (sess["close"] / first_open - 1).abs()
    piv = sess.pivot_table(index="date", columns="tod", values="move")
    sigma = piv.rolling(lookback, min_periods=max(5, lookback // 2)).mean().shift(1)
    # daily ATR-like range for sizing (fraction of price)
    rng = sess.groupby("date").apply(lambda d: (d.high.max() - d.low.min()) / d.open.iloc[0])
    rng = rng.rolling(20, min_periods=10).mean().shift(1)

    days = []
    for date, d in sess.groupby("date"):
        if date not in sigma.index or d.tod.iloc[0] > start_tod + 10 or len(d) < 20:
            continue
        sig = sigma.loc[date].reindex(d.tod.values).values
        if np.isnan(sig).mean() > 0.2:
            continue
        sig = pd.Series(sig).ffill().bfill().values
        tp = (d.high.values + d.low.values + d.close.values) / 3
        vol = d.volume.values.astype(float)
        vwap = np.cumsum(tp * vol) / np.maximum(np.cumsum(vol), 1e-9)
        pc, pcon = last.loc[date, "pc"], last.loc[date, "pcontract"]
        if pd.isna(pc) or pcon != d.contract.iloc[0]:
            pc = d.open.iloc[0]
        days.append(dict(date=date, o=d.open.values, h=d.high.values, l=d.low.values,
                         c=d.close.values, tod=d.tod.values, vwap=vwap, sig=sig, pc=pc,
                         rng=rng.get(date, np.nan), contract=d.contract.iloc[0]))
    return dict(name=name, tick=tick, days=days, start_tod=start_tod)


def _trade(date, side, ep, xp, et, xt, tick):
    slip = SLIP_TICKS * tick
    ep2, xp2 = ep + side * slip, xp - side * slip
    ret = side * (xp2 - ep2) / ep2 - 2 * COMMISSION
    return (date, side, ep2, xp2, et, xt, ret)


# ----------------------------------------------------------------------------- A
def noise_breakout(prep, k=1.0, interval=30, trail_vwap=True, last_entry=1080,
                   hard_stop=None, flip=True):
    """Noise-area intraday momentum (Zarattini/Aziz/Barbon 2024) adapted to FORTS.

    Bounds: U = max(O, PC)*(1+k*sigma_t), L = min(O, PC)*(1-k*sigma_t), where sigma_t is
    the average absolute move from the session open at time-of-day t over past days.
    Decisions only at `interval`-minute checkpoints. Exit when price falls back
    through max(U, VWAP) (long) / min(L, VWAP) (short).
    hard_stop: intrabar protective stop at entry*(1 -/+ hard_stop*sigma_t*k) (None = off).
    """
    tick, start, trades = prep["tick"], prep["start_tod"], []
    for D in prep["days"]:
        o, h, l, c, tod, vw, sig = D["o"], D["h"], D["l"], D["c"], D["tod"], D["vwap"], D["sig"]
        O = o[0]
        U = max(O, D["pc"]) * (1 + k * sig)
        L = min(O, D["pc"]) * (1 - k * sig)
        n, pos, ep, et, stop = len(o), 0, 0.0, 0, None
        for i in range(n - 1):
            # intrabar protective stop
            if pos and stop is not None and i > et:
                if (pos == 1 and l[i] <= stop) or (pos == -1 and h[i] >= stop):
                    xp = min(o[i], stop) if pos == 1 else max(o[i], stop)
                    trades.append(_trade(D["date"], pos, ep, xp, tod[et], tod[i], tick))
                    pos = 0
                    continue
            if (tod[i] + BAR - start) % interval:
                continue
            want = pos
            if pos == 1:
                if c[i] < max(U[i], vw[i]) if trail_vwap else c[i] < U[i]:
                    want = 0
                if flip and c[i] < L[i] and tod[i] <= last_entry:
                    want = -1
            elif pos == -1:
                if c[i] > min(L[i], vw[i]) if trail_vwap else c[i] > L[i]:
                    want = 0
                if flip and c[i] > U[i] and tod[i] <= last_entry:
                    want = 1
            elif tod[i] <= last_entry:
                want = 1 if c[i] > U[i] else -1 if c[i] < L[i] else 0
            if want != pos:
                if pos:
                    trades.append(_trade(D["date"], pos, ep, o[i + 1], tod[et], tod[i + 1], tick))
                pos = want
                if pos:
                    ep, et = o[i + 1], i + 1
                    stop = (ep * (1 - pos * hard_stop * k * sig[i])) if hard_stop else None
        if pos:
            trades.append(_trade(D["date"], pos, ep, c[-1], tod[et], tod[-1], tick))
    return trades


# ----------------------------------------------------------------------------- B
def orb(prep, or_min=30, stop_mode="opp", tp_r=None, last_entry=960, max_trades=1):
    """Opening range breakout from the 10:00 open."""
    tick, trades = prep["tick"], []
    nb = or_min // BAR
    for D in prep["days"]:
        o, h, l, c, tod = D["o"], D["h"], D["l"], D["c"], D["tod"]
        if len(o) <= nb + 1:
            continue
        hi, lo = h[:nb].max(), l[:nb].min()
        mid = (hi + lo) / 2
        pos, cnt, i = 0, 0, nb - 1
        n = len(o)
        while i < n - 1:
            if pos == 0:
                if cnt >= max_trades or tod[i] > last_entry:
                    break
                side = 1 if c[i] > hi else -1 if c[i] < lo else 0
                if side:
                    pos, ep, et = side, o[i + 1], i + 1
                    stop = (lo if side == 1 else hi) if stop_mode == "opp" else mid
                    risk = abs(ep - stop)
                    tp = ep + side * tp_r * risk if tp_r else None
                    cnt += 1
                i += 1
                continue
            # in position: check bar i (entered at open of et <= i)
            hit_stop = l[i] <= stop if pos == 1 else h[i] >= stop
            if hit_stop:
                xp = min(o[i], stop) if pos == 1 else max(o[i], stop)
                trades.append(_trade(D["date"], pos, ep, xp, tod[et], tod[i], tick))
                pos = 0
            elif tp is not None and (h[i] >= tp if pos == 1 else l[i] <= tp):
                xp = max(o[i], tp) if pos == 1 else min(o[i], tp)
                t = _trade(D["date"], pos, ep, xp + pos * SLIP_TICKS * tick, tod[et], tod[i], tick)
                trades.append(t)  # limit order: slippage refunded above
                pos = 0
            i += 1
        if pos:
            trades.append(_trade(D["date"], pos, ep, c[-1], tod[et], tod[-1], tick))
    return trades


# ----------------------------------------------------------------------------- C
def vwap_revert(prep, z_in=1.5, z_stop=3.0, first_entry=630, last_entry=1020, max_trades=2):
    """Fade stretches away from session VWAP measured in noise-sigma units."""
    tick, trades = prep["tick"], []
    for D in prep["days"]:
        o, c, tod, vw, sig = D["o"], D["c"], D["tod"], D["vwap"], D["sig"]
        O = o[0]
        z = (c - vw) / (O * np.maximum(sig, 1e-6))
        pos, cnt, n = 0, 0, len(o)
        for i in range(n - 1):
            want = pos
            if pos == 0 and cnt < max_trades and first_entry <= tod[i] <= last_entry:
                if z_in < z[i] < z_stop:
                    want = -1
                elif -z_stop < z[i] < -z_in:
                    want = 1
            elif pos == 1 and (c[i] >= vw[i] or z[i] < -z_stop):
                want = 0
            elif pos == -1 and (c[i] <= vw[i] or z[i] > z_stop):
                want = 0
            if want != pos:
                if pos:
                    trades.append(_trade(D["date"], pos, ep, o[i + 1], tod[et], tod[i + 1], tick))
                else:
                    cnt += 1
                pos = want
                if pos:
                    ep, et = o[i + 1], i + 1
        if pos:
            trades.append(_trade(D["date"], pos, ep, c[-1], tod[et], tod[-1], tick))
    return trades


# ----------------------------------------------------------------------------- D
def donchian(prep, n_bars=24, atr_mult=3.0, last_entry=1080, first_entry=630):
    """Intraday channel breakout (within session) with ATR(sigma) trailing stop."""
    tick, trades = prep["tick"], []
    for D in prep["days"]:
        o, h, l, c, tod, sig = D["o"], D["h"], D["l"], D["c"], D["tod"], D["sig"]
        O = o[0]
        # per-bar volatility proxy: 5-min sigma ~ daily range / sqrt(bars)
        unit = D["rng"] * O / np.sqrt(len(o)) if not np.isnan(D["rng"]) else None
        if unit is None:
            continue
        pos, n = 0, len(o)
        for i in range(n_bars, n - 1):
            if pos == 1:
                best = max(best, c[i])
                if c[i] < best - atr_mult * unit:
                    trades.append(_trade(D["date"], pos, ep, o[i + 1], tod[et], tod[i + 1], tick))
                    pos = 0
                continue
            if pos == -1:
                best = min(best, c[i])
                if c[i] > best + atr_mult * unit:
                    trades.append(_trade(D["date"], pos, ep, o[i + 1], tod[et], tod[i + 1], tick))
                    pos = 0
                continue
            if not first_entry <= tod[i] <= last_entry:
                continue
            hi, lo = h[i - n_bars:i].max(), l[i - n_bars:i].min()
            if c[i] > hi:
                pos, ep, et, best = 1, o[i + 1], i + 1, c[i]
            elif c[i] < lo:
                pos, ep, et, best = -1, o[i + 1], i + 1, c[i]
        if pos:
            trades.append(_trade(D["date"], pos, ep, c[-1], tod[et], tod[-1], tick))
    return trades


COLS = ["date", "side", "entry", "exit", "et", "xt", "ret"]


def to_df(trades):
    return pd.DataFrame(trades, columns=COLS)


# ----------------------------------------------------------------------------- E
def open_drive(prep, k=2.0, first_check=630, last_entry=780, interval=30,
               stop="open", stop_k=1.0, exit_mode="eod", use_pc=False):
    """Opening-drive momentum: one trade per day.

    Enter when the move from the 10:00 open exceeds k * sigma_t (average absolute move at
    that time of day over past days), checked every `interval` minutes from first_check
    to last_entry. Stops: 'open' -> back through the session open price;
    'sigma' -> entry -/+ stop_k * k * sigma_t * entry; None -> no stop.
    exit_mode: 'eod' hold to the last bar; 'band' exit on checkpoint close back inside
    the k*sigma band.
    """
    tick, start, trades = prep["tick"], prep["start_tod"], []
    for D in prep["days"]:
        o, h, l, c, tod, sig = D["o"], D["h"], D["l"], D["c"], D["tod"], D["sig"]
        O = o[0]
        up_ref = max(O, D["pc"]) if use_pc else O
        dn_ref = min(O, D["pc"]) if use_pc else O
        U, L = up_ref * (1 + k * sig), dn_ref * (1 - k * sig)
        n, pos, done = len(o), 0, False
        for i in range(n - 1):
            chk = (tod[i] + BAR - start) % interval == 0
            if pos:
                if stp is not None and i >= et and ((pos == 1 and l[i] <= stp) or (pos == -1 and h[i] >= stp)):
                    xp = min(o[i], stp) if pos == 1 else max(o[i], stp)
                    trades.append(_trade(D["date"], pos, ep, xp, tod[et], tod[i], tick))
                    pos = 0
                    continue
                if exit_mode == "band" and chk and ((pos == 1 and c[i] < U[i]) or (pos == -1 and c[i] > L[i])):
                    trades.append(_trade(D["date"], pos, ep, o[i + 1], tod[et], tod[i + 1], tick))
                    pos = 0
                continue
            if done or not chk or tod[i] + BAR < first_check or tod[i] > last_entry:
                continue
            side = 1 if c[i] > U[i] else -1 if c[i] < L[i] else 0
            if side:
                pos, ep, et, done = side, o[i + 1], i + 1, True
                if stop == "open":
                    stp = O
                elif stop == "sigma":
                    stp = ep * (1 - side * stop_k * k * sig[i])
                else:
                    stp = None
        if pos:
            trades.append(_trade(D["date"], pos, ep, c[-1], tod[et], tod[-1], tick))
    return trades
