"""Grid research of strategy families, in-sample / out-of-sample split.

IS: 2024-01 .. 2025-06, OOS: 2025-07 .. 2026-10.
Output: results/grid.csv with per (strategy, params, instrument, session) metrics.
"""
import itertools, sys, time
from pathlib import Path
import numpy as np
import pandas as pd
import strategies as S
from common import INSTRUMENTS, ROOT

SPLIT = pd.Timestamp("2025-07-01")
SESSIONS = {"main": 1120, "eve": 1415}  # start of last bar: 18:40 / 23:35

GRIDS = {
    "drive": (S.open_drive, dict(k=[1.0, 1.5, 2.0, 2.5], interval=[30, 60], last_entry=[720, 900],
                                 stop=["open", "sigma", None], exit_mode=["eod", "band"])),
    "noise": (S.noise_breakout, dict(k=[0.75, 1.0, 1.25, 1.5], interval=[15, 30, 60],
                                     trail_vwap=[True])),
    "orb": (S.orb, dict(or_min=[15, 30, 60], stop_mode=["opp", "mid"], tp_r=[None, 2.0])),
    "vwap": (S.vwap_revert, dict(z_in=[1.0, 1.5, 2.0], z_stop=[3.0, 4.0])),
    "donch": (S.donchian, dict(n_bars=[12, 24, 48], atr_mult=[1.0, 2.0, 3.0])),
}


def summarize(tr, dates):
    if tr.empty:
        return {}
    daily = tr.groupby("date")["ret"].sum().reindex(dates, fill_value=0.0)
    out = {}
    for tag, m in (("is", daily.index < SPLIT), ("oos", daily.index >= SPLIT)):
        d = daily[m]
        t = tr[(tr.date < SPLIT) if tag == "is" else (tr.date >= SPLIT)]
        sd = d.std()
        out[f"{tag}_sharpe"] = d.mean() / sd * np.sqrt(252) if sd > 0 else 0
        out[f"{tag}_annret"] = d.mean() * 252
        out[f"{tag}_trades"] = len(t)
        out[f"{tag}_avg_bps"] = t.ret.mean() * 1e4 if len(t) else 0
        out[f"{tag}_win"] = (t.ret > 0).mean() if len(t) else 0
        eq = d.cumsum()
        out[f"{tag}_maxdd"] = (eq - eq.cummax()).min()
    return out


def main():
    which = [a for a in sys.argv[1:] if a in GRIDS] or list(GRIDS)
    insts = [a for a in sys.argv[1:] if a in INSTRUMENTS] or INSTRUMENTS
    rows = []
    for name in insts:
        for sess, end in SESSIONS.items():
            t0 = time.time()
            prep = S.prepare(name, end_tod=end)
            dates = pd.DatetimeIndex([d["date"] for d in prep["days"]])
            for strat in which:
                fn, grid = GRIDS[strat]
                for combo in itertools.product(*grid.values()):
                    p = dict(zip(grid, combo))
                    tr = S.to_df(fn(prep, **p))
                    rows.append(dict(strategy=strat, inst=name, session=sess,
                                     params=str(p), **summarize(tr, dates)))
            print(name, sess, f"{time.time() - t0:.0f}s", flush=True)
    out = ROOT / "results"
    out.mkdir(exist_ok=True)
    df = pd.DataFrame(rows)
    df.to_csv(out / f"grid_{'_'.join(which + insts)}.csv", index=False)
    return df


if __name__ == "__main__":
    main()
