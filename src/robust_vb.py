import numpy as np, pandas as pd
from engine import Engine
from signals import vol_breakout
G = ["Si", "CR", "Eu", "RI", "MX", "GZ"]
BASE = dict(k=0.8, n_range=20, last_entry_tod=1080)
def yr(tr):
    return {str(y): f"{g.ret.mean()*1e4:+.0f}/{len(g)}" for y, g in tr.groupby(tr.date.dt.year)}
rows = []
for n in G:
    E = Engine(n, delay=2)
    for name, kw, ekw in [("BASE", {}, {}), ("k=0.7", dict(k=0.7), {}), ("k=0.9", dict(k=0.9), {}),
                          ("nr=15", dict(n_range=15), {}), ("nr=30", dict(n_range=30), {}),
                          ("le=17:00", dict(last_entry_tod=1020), {}), ("le=19:00", dict(last_entry_tod=1140), {}),
                          ("stop 1R", dict(stop_k=1.0), {}), ("stop 0.5R", dict(stop_k=0.5), {}),
                          ("delay5 stopslip3", {}, dict(delay=5, stop_slip=3)), ("flat 18:40", {}, dict(flat_tod=1120))]:
        EE = Engine(n, **{"delay": 2, **ekw}) if ekw else E
        tr = EE.run(vol_breakout(n, **{**BASE, **kw}))
        d = tr.groupby("date").ret.sum().reindex(pd.bdate_range("2024-02-01", "2026-10-16"), fill_value=0)
        rows.append(dict(inst=n, var=name, n=len(tr), bp=round(tr.ret.mean()*1e4, 1),
                         sh=round(d.mean()/d.std()*np.sqrt(252), 2), **yr(tr)))
    print(n, flush=True)
pd.set_option("display.width", 200)
print(pd.DataFrame(rows).to_string())
