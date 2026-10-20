"""Instruments and candles."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .api import ROOT, Client, q2f

DATA_DIR = ROOT / "data"

# Max request window per candle interval (API limits).
INTERVALS = {
    "1m": ("CANDLE_INTERVAL_1_MIN", timedelta(days=1), timedelta(minutes=1)),
    "5m": ("CANDLE_INTERVAL_5_MIN", timedelta(days=7), timedelta(minutes=5)),
    "15m": ("CANDLE_INTERVAL_15_MIN", timedelta(days=21), timedelta(minutes=15)),
    "1h": ("CANDLE_INTERVAL_HOUR", timedelta(days=90), timedelta(hours=1)),
    "4h": ("CANDLE_INTERVAL_4_HOUR", timedelta(days=90), timedelta(hours=4)),
    "1d": ("CANDLE_INTERVAL_DAY", timedelta(days=365 * 5), timedelta(days=1)),
}


@dataclass(frozen=True)
class Instrument:
    ticker: str
    class_code: str
    uid: str
    name: str
    basic_asset: str
    step: float          # min price increment, points
    step_rub: float      # rubles per one step
    lot: int
    go_buy: float        # initial margin per contract, rub
    go_sell: float
    expiration: str      # YYYY-MM-DD; 2099-12-31 for perpetuals

    @property
    def point_value(self) -> float:
        """Rubles per 1.0 point of price, per contract."""
        return self.step_rub / self.step

    @property
    def go(self) -> float:
        return max(self.go_buy, self.go_sell)

    @property
    def perpetual(self) -> bool:
        return self.expiration.startswith("2099")

    def round_price(self, price: float) -> float:
        return round(round(price / self.step) * self.step, 9)


@dataclass(frozen=True)
class Candle:
    t: datetime
    o: float
    h: float
    l: float
    c: float
    v: int


def _instrument(f: dict) -> Instrument:
    return Instrument(
        ticker=f["ticker"],
        class_code=f["classCode"],
        uid=f["uid"],
        name=f.get("name", ""),
        basic_asset=f.get("basicAsset", ""),
        step=q2f(f["minPriceIncrement"]),
        step_rub=q2f(f["minPriceIncrementAmount"]),
        lot=int(f.get("lot", 1)),
        go_buy=q2f(f.get("initialMarginOnBuy")),
        go_sell=q2f(f.get("initialMarginOnSell")),
        expiration=f.get("expirationDate", "2099-12-31")[:10],
    )


def resolve(client: Client, spec: str, roll_days: int = 5, today: datetime | None = None) -> Instrument:
    """Resolve an instrument spec to a tradeable futures contract.

    "IMOEXF"     -> that ticker on SPBFUT (perpetuals, or a specific contract)
    "front:GAZP" -> nearest contract on basic asset GAZP expiring > roll_days from now
    "front:Brent:BR" -> same, only tickers starting with BR (skips the BM mini contracts)
    """
    today = today or datetime.now(timezone.utc)
    if spec.startswith("front:"):
        asset, prefix = (spec.split(":")[1:] + [""])[:2]
        futs = client.call("InstrumentsService/Futures", {"instrumentStatus": "INSTRUMENT_STATUS_BASE"})["instruments"]
        cutoff = (today + timedelta(days=roll_days)).strftime("%Y-%m-%d")
        cands = [
            f for f in futs
            if f.get("basicAsset") == asset and f.get("apiTradeAvailableFlag") and f["ticker"].startswith(prefix)
            and f["expirationDate"][:10] > cutoff and not f["expirationDate"].startswith("2099")
        ]
        if not cands:
            raise LookupError(f"no front contract for {asset}")
        f = min(cands, key=lambda f: f["expirationDate"])
    else:
        f = client.call(
            "InstrumentsService/FutureBy",
            {"idType": "INSTRUMENT_ID_TYPE_TICKER", "classCode": "SPBFUT", "id": spec},
        )["instrument"]
    inst = _instrument(f)
    # FutureBy margins can be stale; GetFuturesMargin is authoritative.
    m = client.call("InstrumentsService/GetFuturesMargin", {"instrumentId": inst.uid})
    return Instrument(**{
        **asdict(inst),
        "go_buy": q2f(m.get("initialMarginOnBuy")) or inst.go_buy,
        "go_sell": q2f(m.get("initialMarginOnSell")) or inst.go_sell,
        "step": q2f(m.get("minPriceIncrement")) or inst.step,
        "step_rub": q2f(m.get("minPriceIncrementAmount")) or inst.step_rub,
    })


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_candles(client: Client, instrument_id: str, interval: str, start: datetime, end: datetime,
                  complete_only: bool = True) -> list[Candle]:
    api_interval, window, _ = INTERVALS[interval]
    out: dict[datetime, Candle] = {}
    t = start
    while t < end:
        t2 = min(t + window, end)
        resp = client.call("MarketDataService/GetCandles", {
            "instrumentId": instrument_id, "from": _iso(t), "to": _iso(t2),
            "interval": api_interval, "candleSourceType": "CANDLE_SOURCE_EXCHANGE",
        })
        for c in resp.get("candles", []):
            if complete_only and not c.get("isComplete", True):
                continue
            ts = datetime.fromisoformat(c["time"].replace("Z", "+00:00"))
            out[ts] = Candle(ts, q2f(c["open"]), q2f(c["high"]), q2f(c["low"]), q2f(c["close"]), int(c.get("volume", 0)))
        t = t2
    return [out[k] for k in sorted(out)]


def load_history(client: Client | None, ticker: str, interval: str, days: int, refresh: bool = False) -> list[Candle]:
    """Candles for backtests, cached in data/<ticker>_<interval>.json."""
    DATA_DIR.mkdir(exist_ok=True)
    path = DATA_DIR / f"{ticker}_{interval}.json"
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    cached: list[Candle] = []
    if path.exists():
        cached = [Candle(datetime.fromisoformat(r[0]), *r[1:]) for r in json.loads(path.read_text())]
    if client is not None and (refresh or not cached or cached[0].t > start + timedelta(days=3)
                               or cached[-1].t < end - INTERVALS[interval][2] * 2):
        fetch_from = cached[-1].t if cached and cached[0].t <= start + timedelta(days=3) else start
        inst = f"{ticker}_SPBFUT"
        fresh = fetch_candles(client, inst, interval, fetch_from, end)
        merged = {c.t: c for c in cached}
        merged.update({c.t: c for c in fresh})
        cached = [merged[k] for k in sorted(merged)]
        path.write_text(json.dumps([[c.t.isoformat(), c.o, c.h, c.l, c.c, c.v] for c in cached]))
    return [c for c in cached if c.t >= start]
