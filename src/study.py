import numpy as np, pandas as pd
from features import all_tables
from common import SPECS, COMMISSION
SPLIT = pd.Timestamp("2025-07-01")
F = all_tables()
F.to_parquet("../data/features.parquet")
cost = {n: 2 * COMMISSION + 2 * SPECS[n]["tick"] / F[F.inst == n].price.median() for n in SPECS}
print("round-trip cost bps", {k: round(v * 1e4, 1) for k, v in cost.items()})
X = ["gap", "morn", "h30", "pmain", "peve", "mom5", "mom20"]
pd.set_option("display.width", 220)
# univariate: standardized feature (by vol20) vs y30
for y in ["y30"]:
    rows = []
    for n, g in F.groupby("inst"):
        g = g.dropna(subset=X + [y, "vol20"])
        for x in X:
            for tag, m in (("IS", g.index < SPLIT), ("OOS", g.index >= SPLIT)):
                s = g[m]
                rows.append(dict(inst=n, x=x, per=tag, ic=s[x].corr(s[y], method="spearman")))
    R = pd.DataFrame(rows).pivot_table(index=["x"], columns=["per", "inst"], values="ic")
    print(y); print(R.round(3).to_string())
