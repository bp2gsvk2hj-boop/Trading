import numpy as np, pandas as pd
from common import SPECS, COMMISSION
SPLIT = pd.Timestamp("2025-07-01")
F = pd.read_parquet("../data/features.parquet")
X = ["gap", "morn", "h30", "pmain", "peve", "mom5", "mom20"]
Y = "y30"
pd.set_option("display.width", 220)

def fit(g, feats, lam=1.0, intercept=True):
    A = g[feats].values
    if intercept: A = np.c_[np.ones(len(A)), A]
    P = lam * len(A) * 0.01 * np.eye(A.shape[1]);
    if intercept: P[0, 0] = 0
    return np.linalg.solve(A.T @ A + P, A.T @ g[Y].values)

def predict(g, feats, w, intercept=True):
    A = g[feats].values
    if intercept: A = np.c_[np.ones(len(A)), A]
    return A @ w

rows = []
for n, g in F.groupby("inst"):
    g = g.dropna(subset=X + [Y, "vol20"]).copy()
    cost = 2 * COMMISSION + 2 * SPECS[n]["tick"] / g.price
    # normalize everything by vol20 (vol-scaled units)
    for c in X + [Y]: g[c] = g[c] / g["vol20"]
    g["cost"] = cost / g["vol20"]
    IS, OOS = g[g.index < SPLIT], g[g.index >= SPLIT]
    for feats_name, feats in [("all", X), ("core", ["gap", "h30", "peve", "pmain"])]:
        for icpt in (True, False):
            w = fit(IS, feats, intercept=icpt)
            for tag, s in (("IS", IS), ("OOS", OOS)):
                pred = predict(s, feats, w, icpt)
                for th in (0.0, 0.5, 1.0, 1.5):
                    take = np.abs(pred) > th * s["cost"]
                    pnl = (np.sign(pred) * s[Y] - s["cost"]) * s["vol20"]  # back to return units
                    pnl = pnl.where(take, 0.0)
                    rows.append(dict(inst=n, feats=feats_name, icpt=icpt, th=th, per=tag,
                                     n=int(take.sum()), bps=pnl[take].mean() * 1e4 if take.any() else 0,
                                     sharpe=pnl.mean() / pnl.std() * np.sqrt(252) if pnl.std() > 0 else 0))
R = pd.DataFrame(rows)
print(R.pivot_table(index=["feats", "icpt", "th", "inst"], columns="per", values=["n", "bps", "sharpe"]).round(2).to_string())
