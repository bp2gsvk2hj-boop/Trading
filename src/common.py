"""Contract specs, data loading and shared helpers."""
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

# tick: min price step; pv: RUB per 1.0 of price (callable of USD/RUB for USD-quoted)
SPECS = {
    "IMOEXF": dict(tick=0.5, pv=lambda usd: 10.0),
    "USDRUBF": dict(tick=0.01, pv=lambda usd: 1000.0),
    "CNYRUBF": dict(tick=0.001, pv=lambda usd: 1000.0),
    "GLDRUBF": dict(tick=0.1, pv=lambda usd: 1.0),
    "BR": dict(tick=0.01, pv=lambda usd: 10.0 * usd),
    "NG": dict(tick=0.001, pv=lambda usd: 100.0 * usd),
    "Si": dict(tick=1.0, pv=lambda usd: 1.0),        # quarterly USD/RUB, RUB per 1000 USD
    "CR": dict(tick=0.001, pv=lambda usd: 1000.0),   # quarterly CNY/RUB
    "MX": dict(tick=25.0, pv=lambda usd: 1.0),       # MOEX index x100
    "RI": dict(tick=10.0, pv=lambda usd: 0.02 * usd),
    "Eu": dict(tick=1.0, pv=lambda usd: 1.0),
    "GD": dict(tick=0.1, pv=lambda usd: usd),         # gold, USD per troy oz, 1 oz
    "SV": dict(tick=0.01, pv=lambda usd: 10.0 * usd),  # silver, 10 oz
    "SR": dict(tick=1.0, pv=lambda usd: 1.0),        # SBER, 100 shares
    "GZ": dict(tick=1.0, pv=lambda usd: 1.0),        # GAZP, 100 shares
    "NA": dict(tick=1.0, pv=lambda usd: 0.01 * usd),  # Nasdaq 100
    "SF": dict(tick=0.01, pv=lambda usd: usd),        # SPY
}
INSTRUMENTS = list(SPECS)
COMMISSION = 0.0004   # per side, fraction of notional (broker + exchange, conservative)
SLIP_TICKS = 1        # per side, market orders / stops


def load_1m(name):
    return pd.read_parquet(DATA / f"{name}.parquet")


def resample(df, minutes=5):
    """1-min -> N-min bars, weekdays only. Bars are labelled by their start time."""
    df = df[df["time"].dt.weekday < 5]
    g = df.set_index("time").groupby("contract").resample(f"{minutes}min")
    out = g.agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
    out = out.dropna(subset=["open"]).reset_index().sort_values("time")
    out["date"] = out["time"].dt.normalize()
    # keep only the selected (most liquid) contract per day: guaranteed by download step
    out["tod"] = out["time"].dt.hour * 60 + out["time"].dt.minute
    return out.reset_index(drop=True)


def load_bars(name, minutes=5):
    cache = DATA / f"{name}_{minutes}m.parquet"
    if cache.exists():
        return pd.read_parquet(cache)
    bars = resample(load_1m(name), minutes)
    bars.to_parquet(cache)
    return bars


def usdrub_daily():
    b = load_bars("USDRUBF")
    return b.groupby("date")["close"].last()


def metrics(daily, label=""):
    """daily: Series of daily returns (fraction of notional). Returns dict."""
    daily = daily.fillna(0.0)
    if len(daily) == 0 or daily.std() == 0:
        return dict(label=label, days=len(daily), ret=0, sharpe=0, maxdd=0)
    eq = daily.cumsum()
    dd = (eq - eq.cummax()).min()
    years = len(daily) / 252
    return dict(label=label, days=len(daily), ann_ret=daily.sum() / years,
                ann_vol=daily.std() * np.sqrt(252),
                sharpe=daily.mean() / daily.std() * np.sqrt(252), maxdd=dd,
                calmar=(daily.sum() / years) / -dd if dd < 0 else np.nan)
