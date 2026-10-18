import pandas as pd, matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from common import ROOT
e = pd.read_csv(ROOT / "results/equity_v2.csv", index_col=0, parse_dates=True)
t = pd.read_csv(ROOT / "results/trades_v2.csv", parse_dates=["date"])
INK, MUTED, GRID = "#0b0b0b", "#52514e", "#e6e5e0"
C = {"Валюта (Si, CR, Eu)": "#2a78d6", "Индексы и акции (RI, MX, GZ)": "#eb6834"}
grp = t.inst.map(lambda x: "Валюта (Si, CR, Eu)" if x in ("Si", "CR", "Eu") else "Индексы и акции (RI, MX, GZ)")
days = e.index
fig, (a, b) = plt.subplots(2, 1, figsize=(10, 6.4), sharex=True, gridspec_kw=dict(height_ratios=[3, 1]))
fig.patch.set_facecolor("#fcfcfb")
for ax in (a, b):
    ax.set_facecolor("#fcfcfb"); ax.grid(color=GRID, lw=0.8); ax.set_axisbelow(True)
    for s in ax.spines.values(): s.set_visible(False)
    ax.tick_params(colors=MUTED, length=0)
    ax.axvline(pd.Timestamp("2025-07-01"), color=MUTED, lw=1, ls="--")
a.plot(days, e.equity / 1e6, color=INK, lw=2, label="Портфель")
for g, col in C.items():
    s = t[grp == g].groupby("date").pnl.sum().reindex(days, fill_value=0).cumsum()
    a.plot(days, (3e6 + s) / 1e6, color=col, lw=1.5, label=g)
a.legend(frameon=False, labelcolor=MUTED, loc="upper left")
a.set_title("VB + ODM: капитал, млн ₽ (старт 3 млн, риск 0.3%/сделку, задержка 2 мин)", color=INK, loc="left", fontsize=11)
a.text(pd.Timestamp("2025-07-08"), 2.95, "обучение | тест →", color=MUTED, fontsize=9)
dd = e.equity / e.equity.cummax() - 1
b.fill_between(days, dd * 100, 0, color=MUTED, alpha=0.35, lw=0)
b.set_ylabel("просадка, %", color=MUTED)
fig.tight_layout(); fig.savefig(ROOT / "results/equity_v2.png", dpi=130)
