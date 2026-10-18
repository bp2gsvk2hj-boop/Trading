"""Final portfolio backtest of the chosen strategy in RUB with risk-based sizing.

Strategy: Opening Drive Momentum (open_drive) on Si + CR front quarterly futures,
k=2.0, hourly checks 11:00-15:00, one trade/day/instrument, stop at the 10:00 open,
exit at 23:40 (start of last bar 23:35).
"""
import sys
import numpy as np, pandas as pd
import strategies as S
from common import SPECS, COMMISSION, ROOT

PARAMS = dict(k=2.0, interval=60, last_entry=900, stop="open", exit_mode="eod")
INSTS = ["Si", "CR"]
CAPITAL = float(sys.argv[1]) if len(sys.argv) > 1 else 1_000_000
RISK = float(sys.argv[2]) if len(sys.argv) > 2 else 0.005  # fraction of equity risked to the stop per trade
MAX_LEV = 2.0         # max notional per instrument as multiple of equity
SPLIT = pd.Timestamp("2025-07-01")


def trades_rub():
    allt = []
    for n in INSTS:
        prep = S.prepare(n, end_tod=1415)
        opens = {D["date"]: D["o"][0] for D in prep["days"]}
        tr = S.to_df(S.open_drive(prep, **PARAMS))
        tr["inst"], tr["O"] = n, tr.date.map(opens)
        allt.append(tr)
    return pd.concat(allt).sort_values(["date", "et"]).reset_index(drop=True)


def simulate(tr, capital=CAPITAL):
    eq, rows = capital, []
    for date, g in tr.groupby("date"):
        day_pnl = 0.0
        for _, t in g.iterrows():
            pv, tick = SPECS[t.inst]["pv"](0), SPECS[t.inst]["tick"]
            stop_dist = max(abs(t.entry - t.O), 5 * tick) * pv
            n = int(min(eq * RISK / stop_dist, eq * MAX_LEV / (t.entry * pv)))
            if n < 1:
                continue
            gross = t.side * (t.exit - t.entry) * pv * n
            fee = COMMISSION * (t.entry + t.exit) * pv * n
            rows.append(dict(date=date, inst=t.inst, side=t.side, n=n, pnl=gross - fee,
                             entry_t=f"{t.et // 60:02d}:{t.et % 60:02d}",
                             exit_t=f"{t.xt // 60:02d}:{t.xt % 60:02d}"))
            day_pnl += gross - fee
        eq += day_pnl
    return pd.DataFrame(rows)


def report(rt, capital=CAPITAL):
    days = pd.bdate_range("2024-01-03", "2026-10-16")
    daily = rt.groupby("date").pnl.sum().reindex(days, fill_value=0.0)
    equity = capital + daily.cumsum()
    ret = daily / equity.shift(1).fillna(capital)
    dd = equity / equity.cummax() - 1
    out = {}
    for tag, m in (("Всего", slice(None)), ("IS 2024-01..2025-06", daily.index < SPLIT),
                   ("OOS 2025-07..2026-10", daily.index >= SPLIT)):
        r, e = ret[m], equity[m]
        yrs = len(r) / 252
        t = rt[(rt.date.isin(r.index))]
        out[tag] = {"CAGR": (e.iloc[-1] / (e.iloc[0] - daily[m].iloc[0])) ** (1 / yrs) - 1,
                    "Sharpe": r.mean() / r.std() * np.sqrt(252),
                    "MaxDD": (e / e.cummax() - 1).min(),
                    "Сделок": len(t), "Win%": (t.pnl > 0).mean(),
                    "PF": t.pnl[t.pnl > 0].sum() / -t.pnl[t.pnl < 0].sum()}
    yearly = rt.groupby(rt.date.dt.year).pnl.sum()
    return pd.DataFrame(out).T, equity, dd, yearly


if __name__ == "__main__":
    tr = trades_rub()
    rt = simulate(tr)
    summ, equity, dd, yearly = report(rt)
    pd.set_option("display.width", 200)
    print(summ.round(3).to_string())
    print("P&L by year, RUB:", yearly.round(0).to_dict())
    print("per instrument:", rt.groupby("inst").pnl.agg(["sum", "count", "mean"]).round(0).to_dict("index"))
    print("daily corr Si/CR pnl:", round(rt.pivot_table(index="date", columns="inst", values="pnl").fillna(0).corr().iloc[0, 1], 2))
    print("avg contracts:", rt.groupby("inst").n.mean().round(1).to_dict())
    out = ROOT / "results"; out.mkdir(exist_ok=True)
    rt.to_csv(out / "trades.csv", index=False)
    pd.DataFrame({"equity": equity, "drawdown": dd}).to_csv(out / "equity.csv")
