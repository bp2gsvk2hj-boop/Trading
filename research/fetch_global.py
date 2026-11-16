"""Hourly continuous history of MOEX futures on global assets (research only).
python3 research/fetch_global.py -> data/C_<prefix>_1h.json via robot.continuous.build."""
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from robot.api import Client
from robot import continuous

ASSETS = {   # name: (basicAsset, ticker prefix)
    "BR": ("Brent", "BR"), "WTI": ("Нефть WTI", "WT"), "NG": ("Газ (США)", "NG"),
    "GOLD": ("Золото в долларах", "GD"), "SILV": ("Серебро", "SV"), "PLT": ("Платина", "PT"),
    "COPPER": ("Медь", "CE"), "BTC": ("Индекс Bitcoin", "BT"), "NASD": ("Nasdaq 100", "NA"),
    "SPYF": ("SPDR S&P 500 ETF Trust", "SF"),
}

if __name__ == "__main__":
    c = Client()
    for name, (asset, prefix) in ASSETS.items():
        t = time.time()
        try:
            bars, rolls, _ = continuous.build(c, asset, prefix, "1h", days=365 * 3)
            print(f"{name:7s} {len(bars):6d} bars {bars[0].t:%Y-%m-%d} .. {bars[-1].t:%Y-%m-%d}, {len(rolls)} rolls, {time.time()-t:.0f}s", flush=True)
        except Exception as e:
            print(name, "ERR", type(e).__name__, e, flush=True)
