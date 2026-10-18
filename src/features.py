"""Daily feature table for intraday-hold models (one row per instrument-day)."""
import numpy as np, pandas as pd
from common import INSTRUMENTS, load_bars

T = [540, 600, 630, 660, 720, 840, 960, 1080, 1125, 1145, 1265, 1425]


def daily_table(name):
    b = load_bars(name)
    df = pd.DataFrame({t: b[b.tod >= t].groupby("date")["open"].first() for t in T})
    # guard against a time key mapping to a much later bar (missing data)
    first_tod = {t: b[b.tod >= t].groupby("date")["tod"].first() for t in T}
    for t in T:
        late = (first_tod[t] > t + 15).reindex(df.index, fill_value=True)
        df[t] = df[t].mask(late)
    df["close"] = b.groupby("date")["close"].last()
    df["contract"] = b.groupby("date")["contract"].first()
    df["same"] = df["contract"] == df["contract"].shift(1)
    lr = lambda a, c: np.log(df[c] / df[a])
    f = pd.DataFrame(index=df.index)
    f["inst"] = name
    f["gap"] = np.log(df[600] / df[1125].shift(1)).where(df["same"])
    f["morn"] = lr(540, 600)
    f["h30"] = lr(600, 630)
    f["h1"] = lr(600, 660)
    f["y30"] = lr(630, 1125)           # 10:30 -> 18:45
    f["y60"] = lr(660, 1125)           # 11:00 -> 18:45
    f["mid"] = lr(660, 1080)
    f["last1h"] = lr(1080, 1125)
    f["main"] = lr(600, 1125)
    f["eve"] = lr(1145, 1425)
    f["pmain"] = f["main"].shift(1).where(df["same"])
    f["peve"] = f["eve"].shift(1).where(df["same"])
    # daily trend on main-session closes (pc of perps continuous; monthly: within contract)
    c = df[1125]
    ret = np.log(c / c.shift(1)).where(df["same"], 0.0)
    f["mom5"] = ret.rolling(5).sum().shift(1)
    f["mom20"] = ret.rolling(20).sum().shift(1)
    f["vol20"] = f["main"].rolling(20, min_periods=10).std().shift(1)
    f["dvol20"] = ret.rolling(20, min_periods=10).std().shift(1)
    f["price"] = df[630]
    return f


def all_tables():
    return pd.concat([daily_table(n) for n in INSTRUMENTS])
