import sys, numpy as np, pandas as pd
from engine import Engine
from signals import anchored_drive
SPLIT = pd.Timestamp("2025-07-01")
def summ(tr):
    r = {}
    for tag, m in (("IS", tr.date < SPLIT), ("OOS", tr.date >= SPLIT)):
        t = tr[m]
        d = t.groupby("date").ret.sum()
        days = pd.bdate_range("2024-01-15", "2025-06-30") if tag == "IS" else pd.bdate_range("2025-07-01", "2026-10-16")
        d = d.reindex(days, fill_value=0)
        r[f"{tag}_n"] = len(t); r[f"{tag}_bp"] = round(t.ret.mean() * 1e4, 1)
        r[f"{tag}_sh"] = round(d.mean() / d.std() * np.sqrt(252), 2)
    return r
for n in ["Si", "CR"]:
    for delay in (0, 2, 5):
        E = Engine(n, delay=delay)
        tr = E.run(anchored_drive(n, "msk10", k=2.0))
        print(n, "delay", delay, summ(tr), tr.reason.value_counts().to_dict())
