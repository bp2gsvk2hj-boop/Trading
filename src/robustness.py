import numpy as np, pandas as pd
import strategies as S, common
SPLIT = pd.Timestamp("2025-07-01")
BASE = dict(k=2.0, interval=60, last_entry=900, stop=None, exit_mode="eod")
preps = {n: S.prepare(n, end_tod=1415) for n in ["Si", "CR"]}

def run(n, **kw):
    p = {**BASE, **kw}
    return S.to_df(S.open_drive(preps[n], **p))

def stats(tr):
    by = tr.groupby(tr.date.dt.year)["ret"]
    out = {f"{y}": f"{g.mean()*1e4:+.1f}bp/{len(g)}" for y, g in by}
    d = tr.groupby("date").ret.sum()
    out["avg_bp"] = round(tr.ret.mean() * 1e4, 1); out["win"] = round((tr.ret > 0).mean(), 2)
    out["pf"] = round(tr.ret[tr.ret > 0].sum() / -tr.ret[tr.ret < 0].sum(), 2)
    return out
pd.set_option("display.width", 220)
rows = []
for n in preps:
    rows.append(dict(inst=n, var="BASE", **stats(run(n))))
    for k in (1.5, 1.75, 2.25, 2.5): rows.append(dict(inst=n, var=f"k={k}", **stats(run(n, k=k))))
    for le in (780, 840, 960, 1020): rows.append(dict(inst=n, var=f"last_entry={le//60}:{le%60:02d}", **stats(run(n, last_entry=le))))
    rows.append(dict(inst=n, var="interval=30", **stats(run(n, interval=30))))
    for sk in (1.0, 1.5, 2.0, 3.0): rows.append(dict(inst=n, var=f"stop sigma x{sk}", **stats(run(n, stop="sigma", stop_k=sk))))
    rows.append(dict(inst=n, var="stop=open", **stats(run(n, stop="open"))))
    rows.append(dict(inst=n, var="use_pc", **stats(run(n, use_pc=True))))
    common_c = S.COMMISSION
    S.COMMISSION = 0.0005; rows.append(dict(inst=n, var="comm 0.05%", **stats(run(n)))); S.COMMISSION = common_c
    S.SLIP_TICKS = 3; rows.append(dict(inst=n, var="slip 3 ticks", **stats(run(n)))); S.SLIP_TICKS = 1
for n in preps:  # main-session-only exit for comparison
    p = S.prepare(n, end_tod=1120)
    rows.append(dict(inst=n, var="exit 18:40", **stats(S.to_df(S.open_drive(p, **BASE)))))
print(pd.DataFrame(rows).to_string())
