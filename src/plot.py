import pandas as pd, matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from common import ROOT
e = pd.read_csv(ROOT / "results/equity.csv", index_col=0, parse_dates=True)
INK, MUTED, GRID, S1 = "#0b0b0b", "#52514e", "#e6e5e0", "#2a78d6"
fig, (a, b) = plt.subplots(2, 1, figsize=(10, 6), sharex=True, gridspec_kw=dict(height_ratios=[3, 1]))
fig.patch.set_facecolor("#fcfcfb")
for ax in (a, b):
    ax.set_facecolor("#fcfcfb"); ax.grid(color=GRID, lw=0.8); ax.set_axisbelow(True)
    for s in ax.spines.values(): s.set_visible(False)
    ax.tick_params(colors=MUTED, length=0)
    ax.axvline(pd.Timestamp("2025-07-01"), color=MUTED, lw=1, ls="--")
a.plot(e.index, e.equity / 1e6, color=S1, lw=2)
a.set_title("Opening Drive Momentum, Si + CR: капитал, млн ₽ (старт 1 млн, риск 0.5%/сделку)", color=INK, loc="left", fontsize=11)
a.text(pd.Timestamp("2025-07-08"), e.equity.min() / 1e6, "← обучение | тест →", color=MUTED, fontsize=9)
b.fill_between(e.index, e.drawdown * 100, 0, color=S1, alpha=0.35, lw=0)
b.set_ylabel("просадка, %", color=MUTED)
fig.tight_layout(); fig.savefig(ROOT / "results/equity.png", dpi=130)
