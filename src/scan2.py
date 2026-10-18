"""Broad scan of alternative intraday families with latency-aware execution."""
import itertools, sys, time
import numpy as np, pandas as pd
from engine import Engine
from signals import anchored_drive, vol_breakout
from common import ROOT
SPLIT = pd.Timestamp("2025-07-01")
DELAY = int(sys.argv[1]) if len(sys.argv) > 1 else 2
PERPS = {"GLDRUBF", "IMOEXF"}
QUART = ["Si", "CR", "Eu", "MX", "RI", "GD", "SV", "SR", "GZ", "NA", "SF", "BR", "NG"]
US = ["NA", "SF", "GD", "SV", "BR", "NG", "Si", "MX", "RI"]

def summ(tr):
    r = {}
    for tag, m, days in (("is", tr.date < SPLIT, pd.bdate_range("2024-01-15", "2025-06-30")),
                         ("oos", tr.date >= SPLIT, pd.bdate_range("2025-07-01", "2026-10-16"))):
        t = tr[m]; d = t.groupby("date").ret.sum().reindex(days, fill_value=0)
        r[f"{tag}_n"] = len(t); r[f"{tag}_bp"] = t.ret.mean() * 1e4 if len(t) else 0
        r[f"{tag}_sh"] = d.mean() / d.std() * np.sqrt(252) if d.std() > 0 else 0
    return r

def main():
  jobs = []
  for n in QUART + list(PERPS):
      flat = 1120 if n in PERPS else 1420
      for k in (1.5, 2.0, 2.5):
          jobs.append((n, "drive10", dict(k=k), flat))
      for k in (0.3, 0.5, 0.7):
          for le in (900, 1080):
              jobs.append((n, "vb", dict(k=k, last_entry_tod=le), flat))
  for n in US:
      for k in (1.5, 2.0, 2.5):
          jobs.append((n, "us_open", dict(k=k), 1420))
  for n, ev in [("BR", "eia_oil"), ("NG", "eia_gas")] + [(x, "nfp") for x in ["GD", "NA", "SF", "Si", "BR", "SV"]]:
      for k in (1.0, 1.5, 2.0):
          jobs.append((n, ev, dict(k=k), 1420))

  rows, engines = [], {}
  t0 = time.time()
  for n, fam, p, flat in jobs:
      E = engines.get((n, flat)) or engines.setdefault((n, flat), Engine(n, delay=DELAY, flat_tod=flat))
      cut = flat - 5
      if fam == "drive10":
          it = anchored_drive(n, "msk10", checks=(60, 120, 180, 240, 300), cutoff_tod=cut, **p)
      elif fam == "us_open":
          it = anchored_drive(n, "us_open", checks=(15, 30, 60, 90, 120), cutoff_tod=cut, **p)
      elif fam in ("eia_oil", "eia_gas", "nfp"):
          it = anchored_drive(n, fam, checks=(5, 10, 15, 30, 60), lookback=8, min_hist=4, ref="prev", cutoff_tod=cut, **p)
      else:
          it = vol_breakout(n, **p)
      tr = E.run(it)
      rows.append(dict(inst=n, fam=fam, params=str(p), **summ(tr)))
  R = pd.DataFrame(rows)
  R.to_csv(ROOT / f"results/scan2_delay{DELAY}.csv", index=False)
  print(f"{time.time()-t0:.0f}s")
  pd.set_option("display.width", 220); pd.set_option("display.max_rows", 300)
  R["min_sh"] = R[["is_sh", "oos_sh"]].min(axis=1)
  print(R.sort_values("min_sh", ascending=False).head(45).round(2).to_string())


if __name__ == "__main__":
    main()
