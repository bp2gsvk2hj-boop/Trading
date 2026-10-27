import sys, json
sys.path.insert(0, "placements"); import fetch_ratings as f
h = f.Http(); out = []
for isin in ["RU000A10FZB2","RU000A10FWR5","RU000A10FQV9","RU000A10FTZ4","RU000A10G1R9","RU000A10F892","RU000A10FQF2","RU000A10G643","RU000A10G0Z4","RU000A10G7P0","RU000A10F9D3"]:
    try:
        d = json.loads(h.get(f"https://iss.moex.com/iss/securities/{isin}.json?iss.only=description&iss.meta=off&description.columns=name,title,value", tries=5, timeout=60))["description"]["data"]
        out.append(isin + " :: " + " | ".join(f"{n}={v}" for n,t,v in d if n not in ("ISIN","REGNUMBER","LATNAME","FACEVALUE","INITIALFACEVALUE","FACEUNIT","ISSUESIZE","MATDATE","COUPONFREQUENCY","COUPONDATE","COUPONPERCENT","DAYSTOREDEMPTION","LISTLEVEL","STARTDATEMOEX","PROGRAMREGISTRYNUMBER","EARLYREPAYMENT","SECSUBTYPE","EVENINGSESSION","MORNINGSESSION","WEEKENDSESSION")))
        out.append("   titles: " + " | ".join(f"{n}:{t}" for n,t,v in d if n not in ("ISIN",))[:600])
    except Exception as e: out.append(f"{isin} ERR {e}")
open("placements/diag.log","w").write("\n".join(out))
