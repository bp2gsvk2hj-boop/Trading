import numpy as np, pandas as pd
from common import SPECS, COMMISSION
F = pd.read_parquet("../data/features.parquet")
pd.set_option("display.width", 220)
rows = []
for n, g in F.groupby("inst"):
    cost = 2 * COMMISSION + 2 * SPECS[n]["tick"] / g.price
    for x, y in [("gap", "main"), ("gap", "y30"), ("h30", "y30"), ("h1", "y60"), ("morn", "main")]:
        for th in (0.0, 0.25, 0.5, 0.75, 1.0):
            s = g.dropna(subset=[x, y, "vol20"])
            c = cost.loc[s.index]
            take = (s[x].abs() > th * s["vol20"])
            pnl = (np.sign(s[x]) * s[y] - c).where(take, 0.0)
            for yr, p in pnl.groupby(pnl.index.year):
                t = take.loc[p.index]
                rows.append(dict(inst=n, rule=f"{x}->{y}", th=th, yr=yr, n=int(t.sum()),
                                 bps=round(p[t].mean() * 1e4, 1) if t.any() else 0))
R = pd.DataFrame(rows)
print(R.pivot_table(index=["rule", "inst", "th"], columns="yr", values=["bps", "n"]).to_string())
