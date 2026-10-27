import sys, json, re, time
sys.path.insert(0, "placements"); import fetch_ratings as f
out = []
def P(*a): out.append(" ".join(map(str,a)))
r = f.Http(cookies=True)
p = r.get("https://raexpert.ru/ratings/securitization_all/") or ""
P("RA sec tr html:", re.findall(r"<tr>(.*?)</tr>", p, re.S)[1][:1500])
b = r.get("https://raexpert.ru/database/securities/bonds/1000075609") or ""
P("RA bond page ISIN:", re.findall(r"RU000[A-Z0-9]{7}", b)[:3], [f.text(x)[:120] for x in re.findall(r".{0,200}ISIN.{0,200}", b)][:2])
for u in ["https://raexpert.ru/ratings/debt_inst/", "https://raexpert.ru/ratings/debt_inst_all/", "https://raexpert.ru/ratings/debt/"]:
    pp = r.get(u) or ""; P("RA", u, len(pp), "rows", len(re.findall(r"<tr>", pp)), [f.text(x)[:200] for x in re.findall(r"<tr>(.*?)</tr>", pp, re.S)[1:3]])
p = r.get("https://raexpert.ru/ratings/regioncredit_all/") or ""
P("RA region tr html:", re.findall(r"<tr>(.*?)</tr>", p, re.S)[1][:900])
n = f.Http(); p = n.get("https://www.ratings.ru/ratings/issues/") or ""
trs = re.findall(r"<tr[^>]*>(.*?)</tr>", p, re.S)
P("NKR issues rows", len(trs)); P("NKR th", [f.text(x) for x in re.findall(r"<th[^>]*>(.*?)</th>", p, re.S)][:10]); P("NKR tr html", trs[1][:1800])
s = f.Http(); p = s.get("https://smart-lab.ru/q/bonds/RU000A10C8H9/") or ""
i = p.find("Кредитный рейтинг"); P("SL html", p[i-200:i+900])
open("placements/run.log","w").write("\n\n".join(out))
