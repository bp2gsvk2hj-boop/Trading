import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from robot.api import Client
from robot import continuous
from research.fetch_global import ASSETS
name = sys.argv[1]; asset, prefix = ASSETS[name]; t = time.time()
bars, rolls, _ = continuous.build(Client(), asset, prefix, "1h", days=365 * 3)
print(f"{name:7s} {len(bars):6d} bars {bars[0].t:%Y-%m-%d} .. {bars[-1].t:%Y-%m-%d}, {len(rolls)} rolls, {time.time()-t:.0f}s", flush=True)
