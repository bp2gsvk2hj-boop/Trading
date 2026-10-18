"""Multi-sleeve intraday portfolio in RUB (latency-aware fills, risk-based sizing).

Sleeves:
  VB  - volatility breakout: broker-side stop-entry at O +/- 0.8 * avg 20d range,
        entries 10:00-18:00, stop at O, exit 23:40.  Si, CR, Eu, RI, MX, GZ.
  ODM - opening-drive momentum: hourly checks 11-15, k=2.0, stop at O, exit 23:40. Si, CR.
"""
import sys
import numpy as np, pandas as pd
from engine import Engine
from signals import vol_breakout, anchored_drive, Minute
from common import SPECS, ROOT

SPLIT = pd.Timestamp("2025-07-01")
VB = ["Si", "CR", "Eu", "RI", "MX", "GZ"]
ODM = ["Si", "CR"]


def sleeve_trades(delay=2):
    out = []
    for n in sorted(set(VB + ODM)):
        E = Engine(n, delay=delay)
        if n in VB:
            t = E.run(vol_breakout(n, k=0.8, n_range=20, last_entry_tod=1080)); t["sleeve"] = "VB"; t["inst"] = n
            out.append(t)
        if n in ODM:
            t = E.run(anchored_drive(n, "msk10", k=2.0)); t["sleeve"] = "ODM"; t["inst"] = n
            out.append(t)
    return pd.concat(out, ignore_index=True)


def usdrub():
    M = Minute("Si")
    return pd.Series({d: M.c[b - 1] / 1000 for d, a, b in M.days})


def simulate(tr, capital, risk, sleeves, max_trade_lev=1.0):
    usd = usdrub()
    tr = tr[tr.sleeve.isin(sleeves)].sort_values(["date", "et"])
    eq, rows = capital, []
    for date, g in tr.groupby("date"):
        u = usd.get(date, usd.iloc[0])
        pnl_day, gross = 0.0, 0.0
        for t in g.itertuples():
            pv = SPECS[t.inst]["pv"](u)
            dist = max(abs(t.entry - t.stop), 3 * SPECS[t.inst]["tick"]) * pv
            n = int(min(eq * risk / dist, eq * max_trade_lev / (t.entry * pv)))
            if n < 1:
                continue
            notional = n * t.entry * pv
            pnl = t.ret * notional
            gross += notional
            rows.append(dict(date=date, sleeve=t.sleeve, inst=t.inst, side=t.side, n=n, notional=notional,
                             pnl=pnl, ret=t.ret, et=t.et, xt=t.xt, reason=t.reason))
            pnl_day += pnl
        eq += pnl_day
    return pd.DataFrame(rows)


def metrics(rt, capital):
    days = pd.bdate_range("2024-02-01", "2026-10-16")
    daily = rt.groupby("date").pnl.sum().reindex(days, fill_value=0.0)
    eq = capital + daily.cumsum()
    r = daily / eq.shift(1).fillna(capital)
    res = {}
    for tag, m in (("Всего", np.ones(len(r), bool)), ("IS", r.index < SPLIT), ("OOS", r.index >= SPLIT)):
        rr, e = r[m], eq[m]
        yrs = len(rr) / 252
        start = e.iloc[0] - daily[m].iloc[0]
        t = rt[rt.date.isin(rr.index)]
        res[tag] = dict(CAGR=(e.iloc[-1] / start) ** (1 / yrs) - 1, Sharpe=rr.mean() / rr.std() * np.sqrt(252),
                        MaxDD=(e / e.cummax() - 1).min(), Trades=len(t), Win=(t.pnl > 0).mean(),
                        PF=t.pnl[t.pnl > 0].sum() / -t.pnl[t.pnl < 0].sum())
    return pd.DataFrame(res).T, eq, r


if __name__ == "__main__":
    capital = 3_000_000
    tr = sleeve_trades()
    tr.to_csv(ROOT / "results/trades_raw_v2.csv", index=False)
    pd.set_option("display.width", 200)
    # correlation of daily unit returns between components
    piv = tr.assign(key=tr.sleeve + ":" + tr.inst).pivot_table(index="date", columns="key", values="ret", aggfunc="sum").fillna(0)
    print("daily return correlation:\n", piv.corr().round(2).to_string())
    for sleeves in (["VB"], ["ODM"], ["VB", "ODM"]):
        for risk in (0.003, 0.005):
            rt = simulate(tr, capital, risk, sleeves)
            m, eq, r = metrics(rt, capital)
            print(f"\n== {'+'.join(sleeves)} risk {risk:.1%}/trade  (avg daily gross notional "
                  f"{rt.groupby('date').notional.sum().mean() / capital:.2f}x, max "
                  f"{rt.groupby('date').notional.sum().max() / capital:.2f}x)")
            print(m.round(3).to_string())
            print("by year RUB:", rt.groupby(rt.date.dt.year).pnl.sum().round(0).to_dict())
            if sleeves == ["VB", "ODM"] and risk == 0.003:
                rt.to_csv(ROOT / "results/trades_v2.csv", index=False)
                pd.DataFrame({"equity": eq, "ret": r}).to_csv(ROOT / "results/equity_v2.csv")
                print("by instrument:", rt.groupby(["sleeve", "inst"]).pnl.sum().round(0).to_dict())
