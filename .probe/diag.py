import sys, json, datetime as dt
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, "placements"); import fetch_ratings as f
h = f.Http(); ISS = "https://iss.moex.com/iss"
R = json.load(open("placements/ratings.json", encoding="utf-8"))
start = (dt.date.today() - dt.timedelta(days=92)).isoformat()
j = json.loads(h.get(f"{ISS}/engines/stock/markets/bonds/securities.json?iss.only=securities&iss.meta=off"))["securities"]
c = {k:i for i,k in enumerate(j["columns"])}
secs = {}
for r in j["data"]: secs.setdefault(r[c["SECID"]], r)
cand = [r for r in secs.values() if r[c["BOARDID"]] != "TQOB" and (r[c["FACEUNIT"]] in ("SUR","RUB",None)) and (r[c["ISSUESIZE"]] or 0)*(r[c["FACEVALUE"]] or 0) >= 1e9]
def desc(r):
    try: return dict(json.loads(h.get(f"{ISS}/securities/{r[c['SECID']]}.json?iss.only=description&iss.meta=off&description.columns=name,value"))["description"]["data"])
    except Exception: return {}
with ThreadPoolExecutor(8) as ex: ds = list(ex.map(desc, cand))
rows = []
for r, d in zip(cand, ds):
    if d.get("ISSUEDATE","") >= start:
        isin = r[c["ISIN"]]; inn = R["isin_inn"].get(isin, "")
        src = "issuer" if R["by_inn"].get(inn) else "issue" if R.get("by_isin",{}).get(isin) else "smartlab" if (R.get("fallback",{}).get(isin) or {}).get("r") else ""
        rows.append((isin, r[c["SHORTNAME"]], src))
from collections import Counter
out = [f"window from {start}: corporate+regional bonds {len(rows)}", str(Counter(x[2] or "NONE" for x in rows))]
out += [f"NONE {x[0]} {x[1]}" for x in rows if not x[2]]
out += [f"SMARTLAB {x[0]} {x[1]} {R['fallback'][x[0]]['r']}" for x in rows if x[2]=="smartlab"]
out += [f"ISSUE {x[0]} {x[1]} {R['by_isin'][x[0]]}" for x in rows if x[2]=="issue"][:40]
open("placements/diag.log","w").write("\n".join(out))
