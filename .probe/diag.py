import sys, json, re, time
sys.path.insert(0, "placements"); import fetch_ratings as f
out = []
def P(*a): out.append(" ".join(map(str,a)))
# 1. ACRA: emission rows in list
h = f.Http(f.acra_ssl_context()); url = f.ACRA + "/local/ajax/get_rate_person.php"
hdr = {"X-Requested-With":"XMLHttpRequest","Referer":f.ACRA+"/ratings/issuers/","Content-Type":"application/json"}
for t in ["ИКС 5", "ВТБ РКС", "Евраз", "Ростовская"]:
    try:
        d = json.loads(h.get(url, json.dumps({"searchType":"emits","text":t,"page":0,"count":50}).encode(), hdr))["data"]
        for it in d["items"]:
            for inf in it.get("info",[]):
                rt = inf.get("rate",{}).get("value",{}) or {}; em = inf.get("emission",{}).get("value",{}) or {}
                P("ACRA", t, "|", it["title"], "|", rt.get("title"), rt.get("rating_scale"), "| em:", em.get("isin"), em.get("title"), em.get("code"))
    except Exception as e: P("ACRA", t, "ERR", e)
    time.sleep(1)
for st in ["securities", "emissions", "emission"]:
    try:
        d = json.loads(h.get(url, json.dumps({"searchType":st,"text":"","page":0,"count":10}).encode(), hdr))
        P("ACRA searchType", st, "total", d.get("data",{}).get("total"), json.dumps(d)[:600])
    except Exception as e: P("ACRA searchType", st, "ERR", e)
    time.sleep(1)
# 2. Expert RA debt instruments & securitization lists
r = f.Http(cookies=True)
for lst in ["debt_inst_all", "securitization_credit_all", "securitization_all", "regioncredit_all"]:
    try:
        p = r.get(f"https://raexpert.ru/ratings/{lst}/") or ""
        rows = re.findall(r"<tr>(.*?)</tr>", p, re.S)
        P("RA", lst, "rows", len(rows), "paginator", len(re.findall(r"b-paginator__link", p)))
        for tr in rows[1:4]: P("   ", f.text(tr)[:220], re.findall(r'href="(/[^"]+)"', tr)[:2])
    except Exception as e: P("RA", lst, "ERR", e)
# 3. NKR issue ratings page?
n = f.Http()
for u in ["https://www.ratings.ru/ratings/issues/", "https://www.ratings.ru/ratings/issuers/VTB/"]:
    try:
        p = n.get(u) or ""
        P("NKR", u, len(p), [f.text(x)[:150] for x in re.findall(r"<tr[^>]*>(.*?)</tr>", p, re.S)[1:4]], re.findall(r"RU000[A-Z0-9]{7}", p)[:5])
    except Exception as e: P("NKR", u, "ERR", e)
# 4. smart-lab bond page
s = f.Http()
for isin in ["RU000A10C8H9", "RU000A1068X9"]:
    try:
        p = s.get(f"https://smart-lab.ru/q/bonds/{isin}/") or ""
        i = p.find("ейтинг"); P("SMARTLAB", isin, len(p), f.text(p[i-300:i+700])[:500] if i > 0 else "no rating text")
    except Exception as e: P("SMARTLAB", isin, "ERR", e)
open("placements/run.log","w").write("\n".join(out))
