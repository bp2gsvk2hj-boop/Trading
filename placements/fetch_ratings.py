#!/usr/bin/env python3
"""Сбор кредитных рейтингов эмитентов (национальная шкала) АКРА, Эксперт РА, НКР и НРА
и привязка их к эмитентам облигаций MOEX по ИНН.

Результат — placements/ratings.json, его читает страница placements/index.html.
Только стандартная библиотека Python 3.11+. Запуск: python3 placements/fetch_ratings.py
"""
from __future__ import annotations

import html
import http.cookiejar
import json
import os
import re
import ssl
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "ratings.json")
ALIASES = os.path.join(HERE, "rating_aliases.json")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
PAUSE = 0.25


def log(*a):
    print(*a, flush=True)


# ---------------------------------------------------------------- http
def acra_ssl_context() -> ssl.SSLContext:
    """acra-ratings.ru не отдаёт промежуточный сертификат GlobalSign — докачиваем его по AIA."""
    ctx = ssl.create_default_context()
    try:
        txt = subprocess.run(
            "openssl s_client -connect www.acra-ratings.ru:443 -servername www.acra-ratings.ru </dev/null 2>/dev/null"
            " | openssl x509 -noout -text", shell=True, capture_output=True, text=True, timeout=60).stdout
        m = re.search(r"CA Issuers - URI:(\S+)", txt)
        if m:
            der = urllib.request.urlopen(m.group(1), timeout=30).read()
            pem = ssl.DER_cert_to_PEM_cert(der) if not der.startswith(b"-----") else der.decode()
            ctx.load_verify_locations(cadata=pem)
            log("АКРА: промежуточный сертификат загружен", m.group(1))
    except Exception as e:  # noqa: BLE001
        log("АКРА: не удалось загрузить промежуточный сертификат:", e)
    return ctx


class Http:
    def __init__(self, ctx: ssl.SSLContext | None = None, cookies: bool = False):
        handlers = [urllib.request.HTTPSHandler(context=ctx or ssl.create_default_context())]
        if cookies:
            handlers.append(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.op = urllib.request.build_opener(*handlers)

    def get(self, url: str, data: bytes | None = None, headers: dict | None = None, enc: str | None = None) -> str:
        last = None
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **(headers or {})})
                with self.op.open(req, timeout=25) as r:
                    body = r.read()
                    cs = enc or r.headers.get_content_charset() or "utf-8"
                time.sleep(PAUSE)
                return body.decode(cs, "replace")
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None
                last = e
                time.sleep(2 * (attempt + 1))
            except Exception as e:  # noqa: BLE001
                last = e
                time.sleep(2 * (attempt + 1))
        raise RuntimeError(f"{url}: {last}")


def text(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


# ---------------------------------------------------------------- rating scale
SCALE = ["D", "RD", "C", "CC", "CCC", "B", "BB", "BBB", "A", "AA", "AAA"]
LAT = str.maketrans("АВСДР", "ABCDR")


def grade(raw: str):
    s = (raw or "").strip().translate(LAT).replace("–", "-").replace("−", "-").upper()
    s = re.sub(r"\(RU\)|\|RU\||\.RU$", "", s)
    s = re.sub(r"^RU", "", s).replace(" ", "")
    m = re.fullmatch(r"(AAA|AA|A|BBB|BB|B|CCC|CC|C|RD|SD|D)([+-]?)", s)
    return (m.group(1) + m.group(2)) if m else None


# ---------------------------------------------------------------- agencies
ACRA = "https://www.acra-ratings.ru"


def fetch_acra(prev_inn: dict, match) -> list[dict]:
    """Список АКРА — POST в /local/ajax/get_rate_person.php с плоским JSON в теле (другие форматы
    параметров сервер игнорирует). ИНН — со страницы эмитента, но только для тех, кого не удалось
    сопоставить по названию, не больше 300 страниц за запуск и с паузой: сайт банит частые запросы."""
    h = Http(acra_ssl_context())
    url = f"{ACRA}/local/ajax/get_rate_person.php"
    hdr = {"X-Requested-With": "XMLHttpRequest", "Referer": f"{ACRA}/ratings/issuers/",
           "Content-Type": "application/json"}
    base_req = {"searchType": "emits", "text": "", "sectors": [], "activities": [], "countries": [], "forecasts": [],
                "on_revision": 0, "rating_scale": 0, "rate_from": 0, "rate_to": 0, "sort": "name", "count": 100}
    items = []
    for sort in ("name", "-name"):  # сервер отдаёт не больше ~600 записей, поэтому второй проход с конца алфавита
        items += acra_list(h, url, hdr, {**base_req, "sort": sort})
    log(f"АКРА: получено записей {len(items)} (с повторами из двух проходов)")
    return acra_rows(h, items, prev_inn, match)


def acra_list(h, url, hdr, base_req) -> list:
    items, page, pages, prev_first = [], 0, 1, None
    while page < pages and page < 150:
        d = json.loads(h.get(url, json.dumps({**base_req, "page": page}).encode(), hdr))["data"]
        pages = int(d.get("count_pages") or 1)
        first = d["items"][0]["title"] if d["items"] else None
        if page > 0 and first == prev_first:   # нумерация с 1: страница 1 совпала со страницей 0
            if page == 1:
                pages += 1
                page += 1
                continue
            raise RuntimeError(f"страница {page} повторяет предыдущую")
        prev_first = first
        items += d["items"]
        page += 1
        time.sleep(0.7)
    return items


def acra_rows(h, items, prev_inn, match) -> list[dict]:
    out, seen = [], set()
    for it in items:
        for inf in it.get("info", []):
            rp = inf.get("ratePerson", {}).get("value", {}) or {}
            rt = inf.get("rate", {}).get("value", {}) or {}
            em = inf.get("emission", {}).get("value", {}) or {}
            if em.get("id") or rt.get("is_withdrawn") or "национальн" not in (rt.get("rating_scale") or "").lower():
                continue
            if (rt.get("rate_type") or "Credit Rating") != "Credit Rating" or not grade(rt.get("title") or ""):
                continue
            key = rp.get("url") or ""
            if key in seen:
                continue
            seen.add(key)
            raw = rt["title"].strip()
            out.append({"name": text(rp.get("title", "")), "rating": raw if raw.upper().endswith("(RU)") else raw + "(RU)",
                        "outlook": rt.get("forecast") or "",
                        "date": (inf.get("pressRelease", {}).get("value", {}) or {}).get("date") or "",
                        "url": ACRA + key, "key": key, "inn": prev_inn.get(key, "")})
    log(f"АКРА: эмитентов с действующим рейтингом по нац. шкале {len(out)}")
    if len(out) < 100:
        raise RuntimeError(f"подозрительно мало эмитентов: {len(out)}")
    todo = [r for r in out if not r["inn"] and not match(r["name"])][:300]
    got = 0
    for r in todo:
        try:
            p = h.get(r["url"]) or ""
        except Exception as e:  # noqa: BLE001
            log("  АКРА: остановил догрузку ИНН:", e)
            break
        m = re.search(r"ИНН\s*</small>\s*<p>\s*(\d{10}|\d{12})", p)
        if m:
            r["inn"] = m.group(1)
            got += 1
        time.sleep(1.5)
    log(f"АКРА: ИНН догружен для {got} из {len(todo)} несопоставленных по названию")
    return out


def fetch_nkr(prev_inn: dict) -> list[dict]:
    h = Http()
    base = "https://www.ratings.ru"
    page = h.get(base + "/ratings/issuers/")
    out = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S):
        m = re.search(r'href="(/ratings/issuers/[^"/]+/)"[^>]*>(.*?)</a>', tr, re.S)
        if not m:
            continue
        spans = [text(x) for x in re.findall(r'<span class="mt-descr">(.*?)</span>', tr, re.S)]
        raw = spans[0] if spans else ""
        if not grade(raw):
            continue
        dm = re.search(r"(\d{2}\.\d{2}\.\d{4})", tr)
        out.append({"name": text(m.group(2)), "rating": raw, "outlook": spans[1] if len(spans) > 1 else "",
                    "date": dm.group(1) if dm else "", "url": base + m.group(1), "key": m.group(1)})
    log(f"НКР: эмитентов с рейтингом: {len(out)}")
    def inn_of(r):
        if prev_inn.get(r["key"]):
            return prev_inn[r["key"]]
        try:
            p = h.get(r["url"]) or ""
            m = re.search(r">ИНН</span>\s*<span[^>]*>\s*(\d{10}|\d{12})", p)
            return m.group(1) if m else ""
        except Exception as e:  # noqa: BLE001
            log("  НКР: нет страницы", r["url"], e)
            return ""
    with ThreadPoolExecutor(6) as ex:
        for r, inn in zip(out, ex.map(inn_of, out)):
            r["inn"] = inn
    return out


RAEX_LISTS = ["credits_all", "bankcredit_all", "regioncredit_all", "insurance_all", "mfi_credits_all"]


def raex_rows(page: str) -> list[dict]:
    rows = []
    for tr in re.findall(r"<tr>(.*?)</tr>", page, re.S):
        m = re.search(r'href="(/database/companies/[^"]+)">(.*?)</a>', tr, re.S)
        tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        if not m or len(tds) < 4:
            continue
        raw = text(tds[1])
        if not grade(raw):
            continue
        rows.append({"name": text(m.group(2)), "rating": raw, "outlook": text(tds[2]), "date": text(tds[3]),
                     "url": "https://raexpert.ru" + m.group(1), "key": m.group(1)})
    return rows


def fetch_raex() -> list[dict]:
    out, seen = [], set()
    for lst in RAEX_LISTS:
        h = Http(cookies=True)
        url = f"https://raexpert.ru/ratings/{lst}/"
        page = h.get(url)
        n, total = 0, 0
        while page:
            n += 1
            added = 0
            for r in raex_rows(page):
                total += 1
                if r["key"] not in seen:
                    seen.add(r["key"])
                    out.append(r)
                    added += 1
            cur = re.search(r'b-paginator__link -active">\s*(\d+)', page)
            cur = int(cur.group(1)) if cur else n
            links = re.findall(r"setRatingPageHash\('([^']+)'\);\" class=\"b-paginator__link[^\"]*\">([^<]+)</span>", page)
            nxt = next((hs for hs, lab in links if lab.strip() == str(cur + 1)), None) \
                or next((hs for hs, lab in links if "»" in lab), None)
            tok = re.search(r"CSRFAjaxTokenPageHash = '([^']+)'", page)
            if not nxt or not tok or n >= 150 or (n > 1 and added == 0):
                break
            h.get("https://raexpert.ru/ratings/index/ajax-set-rating-page-hash/",
                  urllib.parse.urlencode({"rating_page_hash": nxt, "CSRFAjaxToken": tok.group(1)}).encode(),
                  {"X-Requested-With": "XMLHttpRequest", "Referer": url,
                   "Content-Type": "application/x-www-form-urlencoded"})
            page = h.get(url)
        log(f"Эксперт РА {lst}: страниц {n}, строк {total}, всего уникальных объектов {len(out)}")
    return out


NRA_FINMARKET = [354, 362, 375, 297]  # нефинансовые, банки, лизинг, страховые — нац. шкала


def fetch_nra() -> list[dict]:
    h = Http()
    out, seen = [], set()
    for i in NRA_FINMARKET:
        page = h.get(f"https://www.finmarket.ru/database/creditratings/?rt=0&id={i}", enc="cp1251")
        for tr in re.findall(r"<tr>(.*?)</tr>", page, re.S):
            m = re.search(r"organization/\?fid=(\d+)>(.*?)</a>", tr, re.S)
            tds = [text(x) for x in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
            if not m or len(tds) < 3:
                continue
            raw = tds[2].split("/")[0].strip()
            if not grade(raw) or m.group(1) in seen:
                continue
            seen.add(m.group(1))
            out.append({"name": text(m.group(2)), "rating": raw, "outlook": tds[2].split("/")[1] if "/" in tds[2] else "",
                        "date": tds[1], "url": f"https://www.finmarket.ru/database/organization/?fid={m.group(1)}",
                        "key": m.group(1)})
    log(f"НРА (Финмаркет): эмитентов с рейтингом: {len(out)}")
    return out


# ---------------------------------------------------------------- MOEX issuers
def fetch_moex_issuers():
    h = Http()
    isin_inn, inn_title, short = {}, {}, {}
    start = 0
    while True:
        j = json.loads(h.get("https://iss.moex.com/iss/securities.json?engine=stock&market=bonds&is_trading=1"
                             f"&iss.meta=off&limit=100&start={start}"))["securities"]
        if not j["data"]:
            break
        c = {k: i for i, k in enumerate(j["columns"])}
        for row in j["data"]:
            inn, isin = (row[c["emitent_inn"]] or "").strip(), row[c["isin"]]
            if inn and isin:
                isin_inn[isin] = inn
                inn_title.setdefault(inn, row[c["emitent_title"]] or "")
                short.setdefault(inn, set()).add(row[c["shortname"]] or "")
        start += len(j["data"])
    log(f"MOEX: облигаций {len(isin_inn)}, эмитентов {len(inn_title)}")
    return isin_inn, inn_title, short


LEGAL = [
    "публичное акционерное общество", "непубличное акционерное общество", "открытое акционерное общество",
    "закрытое акционерное общество", "акционерное общество", "общество с ограниченной ответственностью",
    "государственная корпорация", "государственная компания", "публично-правовая компания",
    "акционерный коммерческий банк", "коммерческий банк", "небанковская кредитная организация",
    "микрофинансовая компания", "специализированное финансовое общество", "международная компания",
    "акционерная компания",
]
LEGAL_ABBR = {"пао", "ао", "оао", "зао", "нао", "ооо", "гк", "мкпао", "мкао", "акб", "кб", "нко", "мфк", "сфо",
              "ппк", "ак", "pjsc", "jsc", "llc", "ltd", "госкорпорация"}


def tokens(name: str) -> tuple:
    s = (name or "").lower().replace("ё", "е")
    s = re.sub(r"[«»\"'“”„`]", " ", s)
    for p in LEGAL:
        s = s.replace(p, " ")
    return tuple(t for t in re.split(r"[^a-zа-я0-9+]+", s) if t and t not in LEGAL_ABBR)


SERIES = {"бо", "обл", "об", "р", "п", "боп", "пб", "бпиф", "суб", "сб", "sb", "ru", "r"}


def short_root(sn: str) -> str:
    """Корень короткого названия выпуска MOEX: «РЖД 1Р-35R» -> «ржд», «Апатит 1Р4» -> «апатит»."""
    t = [x for x in re.split(r"[^a-zа-я0-9+]+", (sn or "").lower().replace("ё", "е"))
         if x and not re.search(r"\d", x) and x not in SERIES]
    return "".join(t)


def build_matcher(inn_title: dict, aliases: dict, short: dict | None = None):
    """Сопоставление по названию: точное совпадение слов (без ОПФ и кавычек), затем — если одно название
    целиком совпадает с началом или концом другого по границе слов и кандидат единственный."""
    exact: dict[str, set] = {}
    toks = []
    for inn, t in inn_title.items():
        tk = tokens(t)
        if tk:
            exact.setdefault("".join(tk), set()).add(inn)
            toks.append((tk, inn))
    al = {"".join(tokens(k)): v for k, v in aliases.items()}
    roots: dict[str, set] = {}
    for inn, names in (short or {}).items():
        for sn in names:
            r = short_root(sn)
            if len(r) >= 3:
                roots.setdefault(r, set()).add(inn)

    def match(name: str) -> str:
        tk = tokens(name)
        key = "".join(tk)
        if not key:
            return ""
        if key in al:
            return al[key]
        if key in exact:
            return next(iter(exact[key])) if len(exact[key]) == 1 else ""
        if key in roots and len(roots[key]) == 1:
            return next(iter(roots[key]))
        if len(key) < 5:
            return ""
        cand = set()
        for mt, inn in toks:
            short, long_ = (tk, mt) if len(tk) <= len(mt) else (mt, tk)
            if len("".join(short)) < 5:
                continue
            if long_[:len(short)] == short or long_[-len(short):] == short:
                cand.add(inn)
        return cand.pop() if len(cand) == 1 else ""
    return match


# ---------------------------------------------------------------- main
def main() -> int:
    prev = {}
    if os.path.exists(OUT):
        with open(OUT, encoding="utf-8") as f:
            prev = json.load(f)
    aliases = {}
    if os.path.exists(ALIASES):
        with open(ALIASES, encoding="utf-8") as f:
            aliases = {k: v for k, v in json.load(f).items() if not k.startswith("_")}
    cache = prev.get("inn_cache", {})

    isin_inn, inn_title, short = fetch_moex_issuers()
    match = build_matcher(inn_title, aliases, short)

    agencies = {
        "АКРА": lambda: fetch_acra(cache.get("АКРА", {}), match),
        "Эксперт РА": fetch_raex,
        "НКР": lambda: fetch_nkr(cache.get("НКР", {})),
        "НРА": fetch_nra,
    }
    by_inn: dict[str, dict] = {}
    sources, unmatched, new_cache = {}, {}, {}
    prev_by_inn = prev.get("by_inn", {})
    for ag, fn in agencies.items():
        t0 = time.time()
        try:
            rows = fn()
            log(f"{ag}: {time.time() - t0:.0f} с")
            if not rows:
                raise RuntimeError("пустой список")
        except Exception as e:  # noqa: BLE001
            log(f"{ag}: ОШИБКА {e} — оставляю данные прошлого запуска")
            n = 0
            for inn, rec in prev_by_inn.items():
                if ag in rec.get("ratings", {}):
                    by_inn.setdefault(inn, {"name": inn_title.get(inn) or rec.get("name", ""), "ratings": {}})["ratings"][ag] = rec["ratings"][ag]
                    n += 1
            sources[ag] = {"ok": False, "error": str(e)[:300], "issuers": None, "matched": n,
                           "updated": prev.get("sources", {}).get(ag, {}).get("updated")}
            new_cache[ag] = cache.get(ag, {})
            continue
        new_cache[ag] = {r["key"]: r.get("inn", "") for r in rows if r.get("inn")}
        um, ok = [], 0
        for r in rows:
            inn = r.get("inn") or match(r["name"])
            if not inn:
                um.append(r["name"])
                continue
            ok += 1
            rec = by_inn.setdefault(inn, {"name": inn_title.get(inn, r["name"]), "ratings": {}})
            rec["ratings"][ag] = {"r": r["rating"], "o": r["outlook"], "d": r["date"], "src": r["name"], "url": r["url"]}
        unmatched[ag] = sorted(um)
        sources[ag] = {"ok": True, "error": None, "issuers": len(rows), "matched": ok,
                       "updated": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        log(f"{ag}: сопоставлено с эмитентами MOEX {ok} из {len(rows)}")

    if not any(s["ok"] for s in sources.values()):
        log("Ни одно агентство не ответило — файл не меняю")
        return 1

    data = {
        "_comment": "Собирается placements/fetch_ratings.py. by_inn — рейтинги эмитентов по ИНН; isin_inn — ИНН эмитента "
                    "по ISIN облигации (MOEX); ratings — ручные рейтинги выпусков по ISIN; inn_cache — служебный кэш.",
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sources": sources,
        "by_inn": dict(sorted(by_inn.items())),
        "isin_inn": dict(sorted(isin_inn.items())),
        "ratings": prev.get("ratings", {}),
        "unmatched": unmatched,
        "inn_cache": new_cache,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    log(f"Готово: эмитентов с рейтингом {len(by_inn)}, файл {os.path.getsize(OUT)//1024} КБ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
