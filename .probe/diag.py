import sys, json, re, time, datetime as dt
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, "placements"); import fetch_ratings as f
h = f.Http(); ISS = "https://iss.moex.com/iss"
R = json.load(open("placements/ratings.json", encoding="utf-8"))
start = (dt.date.today() - dt.timedelta(days=92)).isoformat()
j = json.loads(h.get(f"{ISS}/engines/stock/markets/bonds/securities.json?iss.only=securities&iss.meta=off"))["securities"]
c = {k:i for i,k in enumerate(j["columns"])}
secs = {}
for r in j["data"]:
    secs.setdefault(r[c["SECID"]], r)
cand = [r for r in secs.values() if (r[c["FACEUNIT"]] in ("SUR","RUB",None)) and (r[c["ISSUESIZE"]] or 0)*(r[c["FACEVALUE"]] or 0) >= 1e9 and (not r[c["MATDATE"]] or r[c["MATDATE"]] >= dt.date.today().isoformat())]
def desc(r):
    try:
        d = json.loads(h.get(f"{ISS}/securities/{r[c['SECID']]}.json?iss.only=description&iss.meta=off&description.columns=name,value"))["description"]["data"]
        return {k:v for k,v in d}
    except Exception: return {}
with ThreadPoolExecutor(8) as ex: ds = list(ex.map(desc, cand))
rows = []
for r, d in zip(cand, ds):
    if d.get("ISSUEDATE","") >= start:
        isin = r[c["ISIN"]]; inn = R["isin_inn"].get(isin, "")
        rows.append({"isin": isin, "short": r[c["SHORTNAME"]], "issued": d.get("ISSUEDATE"), "inn": inn,
                     "issuer": (R["by_inn"].get(inn) or {}).get("name") or d.get("EMITTER_ID",""),
                     "rated": bool(inn and R["by_inn"].get(inn)), "type": d.get("TYPENAME","") + " " + d.get("TYPE","")})
# issuer names for unrated via q
for x in rows:
    if not x["rated"]:
        try:
            q = json.loads(h.get(f"{ISS}/securities.json?q={x['isin']}&iss.meta=off&securities.columns=isin,emitent_title,emitent_inn"))["securities"]["data"]
            for a,b,i in q:
                if a == x["isin"]: x["issuer"], x["inn"] = b, i
        except Exception: pass
un = [x for x in rows if not x["rated"]]
out = [f"window from {start}: bonds {len(rows)}, rated {len(rows)-len(un)}, unrated {len(un)}"]
from collections import Counter
iss = Counter((x["issuer"], x["inn"]) for x in un)
out.append(f"unrated issuers: {len(iss)}")
for (n, i), k in iss.most_common(): out.append(f"{k:3d} | {i} | {n} | {[x['short'] for x in un if x['inn']==i][:3]} | {un[[x['inn'] for x in un].index(i)]['type']}")
open("placements/run.log","w").write("\n".join(out)); print("\n".join(out[:5]))
