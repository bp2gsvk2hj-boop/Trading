"""Download 1-min candle archives from T-Invest history-data and build 1-min series.

Perpetual futures are used as is; for monthly contracts (BR, NG) the most
liquid contract is selected per trading day (intraday strategy -> no
back-adjustment needed, positions never cross days).
Usage: TINVEST_TOKEN=... CA_BUNDLE=... python src/download.py
"""
import io, json, os, sys, time, zipfile, urllib.request, ssl
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
RAW, DATA = ROOT / "data" / "raw", ROOT / "data"
URL = "https://invest-public-api.tbank.ru/history-data?instrumentId={}&year={}"
PERPETUAL = ["IMOEXF", "USDRUBF", "CNYRUBF", "GLDRUBF"]
MONTHLY = ["BR", "NG", "Si", "CR", "MX", "RI", "Eu", "GD", "SV", "SR", "GZ", "NA", "SF"]  # front contract chosen by daily volume
YEARS = [2024, 2025, 2026]
CTX = ssl.create_default_context(cafile=os.environ.get("CA_BUNDLE"))


def get_zip(uid, year):
    path = RAW / f"{uid}_{year}.zip"
    if path.exists():
        return path
    req = urllib.request.Request(URL.format(uid, year),
                                 headers={"Authorization": "Bearer " + os.environ["TINVEST_TOKEN"]})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, context=CTX, timeout=180) as r:
                path.write_bytes(r.read())
            return path
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            time.sleep(5 * (attempt + 1))  # 429 rate limit
        except Exception:
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"failed {uid} {year}")


def read_zip(path, ticker):
    frames = []
    with zipfile.ZipFile(path) as z:
        for n in z.namelist():
            with z.open(n) as f:
                frames.append(pd.read_csv(f, sep=";", header=None,
                                          usecols=range(7)))
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames)
    df.columns = ["uid", "time", "open", "close", "high", "low", "volume"]
    df["time"] = pd.to_datetime(df["time"], utc=True).dt.tz_convert("Europe/Moscow").dt.tz_localize(None)
    df["contract"] = ticker
    return df.drop(columns="uid")


def main():
    RAW.mkdir(parents=True, exist_ok=True)
    fs = json.load(open(RAW / "futures.json"))["instruments"]
    by_ticker = {f["ticker"]: f for f in fs}
    for name in sys.argv[1:] or PERPETUAL + MONTHLY:
        if name in PERPETUAL:
            uid = by_ticker[name]["uid"]
            parts = [read_zip(p, name) for y in YEARS if (p := get_zip(uid, y))]
        else:
            cs = [f for f in fs if f["ticker"].startswith(name) and len(f["ticker"]) == 4
                  and f["ticker"][2] in "FGHJKMNQUVXZ" and f["ticker"][3].isdigit()
                  and "2024-01-15" <= f["expirationDate"][:10] <= "2027-01-31"]
            parts = []
            for f in cs:
                exp_year = int(f["expirationDate"][:4])
                for y in {exp_year - 1, exp_year} & set(YEARS):
                    if p := get_zip(f["uid"], y):
                        parts.append(read_zip(p, f["ticker"]))
                print(name, f["ticker"], file=sys.stderr)
        df = pd.concat([p for p in parts if len(p)]).drop_duplicates(["time", "contract"])
        df["date"] = df["time"].dt.date
        vol = df.groupby(["date", "contract"])["volume"].sum().reset_index()
        best = vol.loc[vol.groupby("date")["volume"].idxmax(), ["date", "contract"]]
        df = df.merge(best, on=["date", "contract"]).drop(columns="date")
        df = df.sort_values("time").reset_index(drop=True)
        df.to_parquet(DATA / f"{name}.parquet")
        print(name, len(df), df["time"].min(), df["time"].max(), flush=True)


if __name__ == "__main__":
    main()
