import sys, time, numpy as np, pandas as pd
from engine import Engine
from signals import vol_breakout, range_fade, anchored_drive
from scan2 import summ
from common import ROOT
INST = ["Si", "CR", "Eu", "MX", "RI", "SR", "GZ", "GD", "BR", "NG", "IMOEXF", "GLDRUBF"]
PERPS = {"IMOEXF", "GLDRUBF"}
rows = []
for n in INST:
    flat = 1120 if n in PERPS else 1420
    E = Engine(n, delay=2, flat_tod=flat)
    for k in (0.6, 0.8, 1.0, 1.2):
        for nr in (5, 10, 20):
            for le in (900, 1080):
                tr = E.run(vol_breakout(n, k=k, n_range=nr, last_entry_tod=le))
                rows.append(dict(inst=n, fam="vb", params=f"k={k} nr={nr} le={le}", **summ(tr)))
    for k in (0.8, 1.0, 1.3):
        for sk in (0.3, 0.6):
            for tg in ("open", "half"):
                tr = E.run(range_fade(n, k=k, stop_k=sk, target=tg))
                rows.append(dict(inst=n, fam="fade", params=f"k={k} sk={sk} tg={tg}", **summ(tr)))
    print(n, flush=True)
R = pd.DataFrame(rows); R.to_csv(ROOT / "results/scan3.csv", index=False)
pd.set_option("display.width", 220); pd.set_option("display.max_rows", 300)
print(R.groupby(["inst", "fam"])[["is_sh", "oos_sh"]].agg(["median", "max"]).round(2).to_string())
