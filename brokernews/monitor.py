"""Дайджест брокерского рынка: Telegram-каналы + новости СМИ -> сводка ChatGPT -> Telegram.

python3 -m brokernews run [--hours N] [--dry-run] [--web DIR]
python3 -m brokernews channels

Настройки — brokernews.toml в корне репозитория. Без внешних зависимостей: HTTP через curl
(как robot/telegram.py — urllib не умеет SOCKS), сначала через выходы VPN на сервере
(TELEGRAM_PROXY, иначе 127.0.0.1:10810/10811), затем напрямую. Xray сам отправляет российские
сайты напрямую, остальное — через зарубежный сервер.
Окружение (/etc/trading-robot.env): TELEGRAM_BOT_TOKEN, OPENAI_API_KEY и получатели —
BROKERNEWS_CHAT_IDS (канал для коллег и т.п., через запятую), иначе TELEGRAM_CHAT_ID робота.
Без OPENAI_API_KEY дайджест — список ссылок по разделам. Состояние — state/brokernews.db.
"""
from __future__ import annotations

import argparse
import email.utils
import hashlib
import html
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "brokernews.toml"
STATE = ROOT / "state"
MSK = timezone(timedelta(hours=3))
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"


def log(level: str, msg: str) -> None:
    print(f"[{level}] {msg}", flush=True)


# ---- сеть -------------------------------------------------------------------
class Net:
    """HTTP через curl с перебором маршрутов: выходы VPN, затем напрямую. Рабочий маршрут запоминается."""

    def __init__(self) -> None:
        proxies = [p for p in (os.environ.get("TELEGRAM_PROXY") or "").split(",") if p] or \
            ["socks5h://127.0.0.1:10810", "socks5h://127.0.0.1:10811"]
        self.routes: list[str | None] = (proxies if shutil.which("curl") else []) + [None]
        self.good = 0

    def request(self, url: str, data: bytes | None = None, headers: dict | None = None,
                timeout: int = 30) -> tuple[int, bytes]:
        errors = []
        order = self.routes[self.good:] + self.routes[:self.good]
        for route in order:
            try:
                code, body = self._one(route, url, data, headers or {}, timeout)
            except Exception as e:  # noqa: BLE001
                errors.append(f"{route or 'direct'}: {e}")
                continue
            self.good = self.routes.index(route)
            return code, body
        raise RuntimeError("; ".join(errors))

    def _one(self, route, url, data, headers, timeout):
        if route is None:
            req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **headers})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return r.status, r.read()
            except urllib.error.HTTPError as e:
                return e.code, e.read()
        cmd = ["curl", "-sS", "-L", "--compressed", "-m", str(timeout), "-x", route, "-A", UA,
               "-w", "\n%{http_code}", url]
        for k, v in headers.items():
            cmd += ["-H", f"{k}: {v}"]
        if data is not None:
            cmd += ["--data-binary", "@-"]
        r = subprocess.run(cmd, input=data, capture_output=True, timeout=timeout + 10)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.decode(errors="replace").strip()[:200] or f"curl {r.returncode}")
        body, _, code = r.stdout.rpartition(b"\n")
        return int(code or 0), body

    def get(self, url: str, timeout: int = 30) -> tuple[int, bytes]:
        return self.request(url, timeout=timeout)

    def post_json(self, url: str, payload: dict, headers: dict | None = None, timeout: int = 60):
        code, body = self.request(url, json.dumps(payload).encode(),
                                  {"Content-Type": "application/json", **(headers or {})}, timeout)
        try:
            return code, json.loads(body or b"{}")
        except json.JSONDecodeError:
            return code, {"raw": body[:300].decode(errors="replace")}


# ---- состояние: что уже отправлено, когда был прошлый запуск -------------------
class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS seen (key TEXT PRIMARY KEY, ts INTEGER)")
        self.db.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
        self.db.commit()

    def seen(self, key: str) -> bool:
        return self.db.execute("SELECT 1 FROM seen WHERE key=?", (key,)).fetchone() is not None

    def mark(self, keys: list[str]) -> None:
        now = int(time.time())
        self.db.executemany("INSERT OR IGNORE INTO seen VALUES (?,?)", [(k, now) for k in keys])
        self.db.execute("DELETE FROM seen WHERE ts < ?", (now - 60 * 86400,))
        self.db.commit()

    def get(self, k: str) -> str | None:
        r = self.db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return r[0] if r else None

    def put(self, k: str, v: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (k, v))
        self.db.commit()

    def close(self) -> None:
        self.db.close()


class Health:
    """Состояние источников. Сбой отдельного источника — только в журнал и в команду sources.
    Владельцу — одно сообщение, когда сбор ломается целиком (не читается большинство отраслевых
    источников) и когда снова работает."""

    def __init__(self, db: sqlite3.Connection, cfg: dict):
        self.db = db
        self.db.execute("""CREATE TABLE IF NOT EXISTS health (src TEXT PRIMARY KEY, label TEXT,
            fails INTEGER DEFAULT 0, last_ok TEXT, last_post TEXT, problem TEXT DEFAULT '')""")
        self.run: dict[str, tuple[bool, str]] = {}

    def record(self, src: str, label: str, ok: bool, last_post: datetime | None = None, error: str = "") -> None:
        now = datetime.now(timezone.utc).isoformat()
        row = self.db.execute("SELECT fails, last_post FROM health WHERE src=?", (src,)).fetchone()
        fails = 0 if ok else (row[0] if row else 0) + 1
        lp = last_post.isoformat() if last_post else (row[1] if row else None)
        self.db.execute("""INSERT INTO health (src, label, fails, last_ok, last_post) VALUES (?,?,?,?,?)
            ON CONFLICT(src) DO UPDATE SET label=excluded.label, fails=excluded.fails,
            last_ok=COALESCE(?, health.last_ok), last_post=excluded.last_post""",
                        (src, label, fails, now if ok else None, lp, now if ok else None))
        self.run[src] = (ok, error)
        if not ok:
            log("warn", f"источник не ответил: {label}: {error[:120]}")

    def _flag(self, key: str, on: bool) -> bool:
        """Запомнить состояние флага; True — если оно изменилось."""
        row = self.db.execute("SELECT problem FROM health WHERE src=?", (key,)).fetchone()
        was = bool(row and row[0])
        self.db.execute("INSERT OR REPLACE INTO health (src, label, problem) VALUES (?, ?, ?)",
                        (key, key, "1" if on else ""))
        self.db.commit()
        return was != on

    def problems(self) -> tuple[list[str], list[str]]:
        """-> (новые критические проблемы, восстановления). Отдельные источники не сообщаются."""
        for (src,) in self.db.execute("SELECT src FROM health").fetchall():
            if not src.startswith("_") and src not in self.run:   # источник убрали из настроек
                self.db.execute("DELETE FROM health WHERE src=?", (src,))
        key = [ok for src, (ok, _) in self.run.items() if not src.startswith("tgp:")]
        down = key.count(False)
        critical = len(key) >= 4 and down >= len(key) / 2
        new, fixed = [], []
        if self._flag("_critical", critical):
            if critical:
                new.append(f"• не отвечают {down} из {len(key)} отраслевых источников — проверьте VPN-выход "
                           "и доступ сервера в интернет")
            else:
                fixed.append("• сбор источников снова работает")
        return new, fixed


def text_hash(text: str) -> str:
    norm = re.sub(r"\W+", "", text.lower())[:160]
    return "h:" + hashlib.md5(norm.encode()).hexdigest()


# ---- фильтр по ключевым словам ------------------------------------------------
class Matcher:
    """Первичное сито. Группы [keywords] — по чему материал вообще может быть интересен;
    [exclude] patterns — рыночный контент (аналитика, котировки, размещения), он отбрасывается,
    кроме продуктовых новостей СберИнвестиций и собственных бумаг Сбера ([exclude] keep_if)."""

    def __init__(self, cfg: dict):
        f = re.IGNORECASE | re.DOTALL   # .{0,80} ловит слова и на соседних строках поста
        self.groups = {g: [re.compile(p, f) for p in pats] for g, pats in (cfg.get("keywords") or {}).items()}
        ex = cfg.get("exclude", {})
        self.exclude = [re.compile(p, f) for p in ex.get("patterns", [])]
        self.keep_if = re.compile(ex["keep_if"], f) if ex.get("keep_if") else None

    def excluded(self, text: str) -> bool:
        return any(p.search(text) for p in self.exclude) and not (self.keep_if and self.keep_if.search(text))

    def match(self, text: str) -> list[str]:
        if self.excluded(text):
            return []
        return [g for g, pats in self.groups.items() if any(p.search(text) for p in pats)]


SBER = "СберИнвестиции"   # группа [keywords], обязательная для «наблюдения за промо» и аналитических сайтов


# ---- Telegram: публичные каналы через t.me/s/<канал> ----------------------------
class TgWebPage(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = None
        self.posts: list[dict] = []
        self._cur = None
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class") or ""
        if tag == "meta" and a.get("property") == "og:title" and not self.title:
            self.title = a.get("content")
        if tag == "div" and "tgme_widget_message " in cls + " " and a.get("data-post"):
            pid = a["data-post"].rsplit("/", 1)[-1]
            self._cur = {"id": int(pid) if pid.isdigit() else 0, "parts": [], "date": None}
            self.posts.append(self._cur)
            return
        if self._depth:
            if tag == "div":
                self._depth += 1
            elif tag == "br":
                self._cur["parts"].append("\n")
            return
        if self._cur is not None:
            if tag == "div" and "js-message_text" in cls:
                self._depth = 1
            elif tag == "time" and a.get("datetime") and not self._cur["date"]:
                try:
                    self._cur["date"] = datetime.fromisoformat(a["datetime"])
                except ValueError:
                    pass

    def handle_endtag(self, tag):
        if self._depth and tag == "div":
            self._depth -= 1
            self._cur["parts"].append("\n")

    def handle_data(self, data):
        if self._depth and self._cur is not None:
            self._cur["parts"].append(data)

    def result(self) -> list[dict]:
        out = []
        for p in self.posts:
            text = re.sub(r"[ \t]+", " ", "".join(p["parts"]))
            text = re.sub(r"\n\s*\n+", "\n", text).strip()
            if p["date"]:
                out.append({"id": p["id"], "text": text, "date": p["date"]})
        return out


def tg_page(net: Net, name: str, before: int | None = None) -> tuple[str | None, list[dict]]:
    code, body = net.get(f"https://t.me/s/{name}" + (f"?before={before}" if before else ""))
    if code != 200:
        return None, []
    page = TgWebPage()
    page.feed(body.decode("utf-8", errors="replace"))
    return page.title, page.result()


def channel_names(cfg: dict) -> list[tuple[str, str]]:
    """Каналы из настроек: [(имя, уровень)]. Уровни: "industry" — отраслевые каналы, все посты
    проходят тематический фильтр; "promo" — рыночные каналы, из них берём только посты про
    СберИнвестиции (посевы, промо). Имя может быть списком вариантов «a|b|c»."""
    t = cfg.get("telegram", {})
    out, seen = [], set()
    for names, tier in ((t.get("channels", []) + t.get("channels_all", []), "industry"),
                        (t.get("promo_watch", []), "promo")):
        for n in names:
            alts = [a.rsplit("/", 1)[-1].lstrip("@").strip() for a in str(n).split("|")]
            n = "|".join(a for a in alts if a)
            if n and n.lower() not in seen:
                seen.add(n.lower())
                out.append((n, tier))
    return out


def tg_resolve(net: Net, store: "Store | None", entry: str, before=None):
    """Вариант @имени с постами -> (имя, заголовок, посты). Рабочий вариант запоминается."""
    alts = entry.split("|")
    known = store.get("tgname:" + entry.lower()) if store and len(alts) > 1 else None
    if known in alts:
        alts.remove(known)
        alts.insert(0, known)
    fresh_after = datetime.now(timezone.utc) - timedelta(days=30)
    stale = None                      # вариант с постами, но заброшенный: берём, только если живых нет
    for name in alts:
        title, posts = tg_page(net, name, before)
        if posts:
            if len(alts) > 1 and max(p["date"] for p in posts) < fresh_after:
                stale = stale or (name, title, posts)
                time.sleep(0.3)
                continue
            if store and len(alts) > 1:
                store.put("tgname:" + entry.lower(), name)
            return name, title, posts
        time.sleep(0.3)
    return stale or (alts[0], None, [])


def collect_telegram(net, cfg, since, store, matcher, health: "Health | None" = None) -> list[dict]:
    pages_max = int(cfg.get("telegram", {}).get("max_pages", 3))
    official = {n.lower().lstrip("@") for n in cfg.get("telegram", {}).get("official", [])}
    items, bad = [], []
    for entry, tier in channel_names(cfg):
        name = entry.split("|")[0]
        hkey = ("tg:" if tier == "industry" else "tgp:") + entry.lower()
        try:
            title, posts, before = None, [], None
            for page in range(pages_max):
                if page == 0:
                    name, t, batch = tg_resolve(net, store, entry)
                else:
                    t, batch = tg_page(net, name, before)
                title = title or t
                if not batch:
                    break
                posts += batch
                oldest = min(batch, key=lambda p: p["id"])
                if oldest["date"] < since or oldest["id"] <= 1:
                    break
                before = oldest["id"]
                time.sleep(0.4)
            label = f"@{name}" if posts or "|" not in entry else "@" + " / @".join(entry.split("|"))
            if health:
                health.record(hkey, label, bool(posts),
                              max((p["date"] for p in posts), default=None), "не найден или пустая страница")
            if not posts:
                bad.append(label)
                continue
            aliases = {a.lower() for a in entry.split("|")}
            kind = "официально" if aliases & official else "Telegram-канал"
            for p in posts:
                if p["date"] < since or len(p["text"]) < 20:
                    continue
                key = f"tg:{name.lower()}:{p['id']}"
                if store.seen(key):
                    continue
                groups = matcher.match(p["text"])
                if tier == "promo" and SBER not in groups:
                    continue
                if groups:
                    items.append(dict(key=key, source=f"TG: {title or name}", text=p["text"], kind=kind,
                                      url=f"https://t.me/{name}/{p['id']}", date=p["date"], groups=groups))
        except Exception as e:  # noqa: BLE001
            log("warn", f"t.me/s/{name}: {e}")
            if health:
                health.record(hkey, f"@{name}", False, error=str(e))
        time.sleep(0.4)
    if bad:
        log("warn", "каналы не найдены или без веб-версии: " + ", ".join(bad))

    log("info", f"Telegram: {len(items)} подходящих постов")
    return items


# ---- новости: RSS и Google Новости -------------------------------------------------
def strip_html(s: str | None) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def xml_root(body: bytes) -> ET.Element:
    """Разбор XML с починкой типичных ошибок лент: голый «&», управляющие символы, мусор до <?xml."""
    try:
        return ET.fromstring(body)
    except ET.ParseError:
        text = body.decode("utf-8", errors="replace")
        text = text[text.find("<"):] if "<" in text else text
        text = re.sub(r"&(?!(?:[a-zA-Z][a-zA-Z0-9]*|#[0-9]+|#x[0-9a-fA-F]+);)", "&amp;", text)
        text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
        text = re.sub(r"^<\?xml[^>]*\?>", "", text.lstrip())
        return ET.fromstring(text)


def parse_feed(body: bytes) -> tuple[str, list[dict]]:
    """RSS 2.0 и Atom -> (название ленты, [{title, summary, link, date, source}])."""
    root = xml_root(body)
    atom = "{http://www.w3.org/2005/Atom}"
    out = []
    if root.tag == atom + "feed":
        title = root.findtext(atom + "title") or ""
        for e in root.iter(atom + "entry"):
            link = e.find(atom + "link")
            d = e.findtext(atom + "updated") or e.findtext(atom + "published")
            try:
                dt = datetime.fromisoformat(d.replace("Z", "+00:00")) if d else None
            except ValueError:
                dt = None
            out.append(dict(title=strip_html(e.findtext(atom + "title")),
                            summary=strip_html(e.findtext(atom + "summary")),
                            link=link.get("href") if link is not None else "", date=dt, source=""))
        return title, out
    ch = root.find("channel")
    title = (ch.findtext("title") if ch is not None else "") or ""
    for e in root.iter("item"):
        d = e.findtext("pubDate")
        try:
            dt = email.utils.parsedate_to_datetime(d) if d else None
            if dt and dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            dt = None
        out.append(dict(title=strip_html(e.findtext("title")), summary=strip_html(e.findtext("description")),
                        link=(e.findtext("link") or "").strip(), date=dt,
                        source=(e.findtext("source") or "").strip(),
                        source_url=(e.find("source").get("url", "") if e.find("source") is not None else "")))
    return title, out


def yandex_search_on() -> bool:
    return bool(os.environ.get("YANDEX_SEARCH_API_KEY") and os.environ.get("YANDEX_FOLDER_ID"))


def news_urls(cfg: dict) -> list[tuple[str, str]]:
    """-> [(url, подпись для отчёта об источниках)]. Поисковые запросы идут в Google Новости,
    только если не настроен Yandex Search API."""
    n = cfg.get("news", {})
    urls = [(u, urllib.parse.urlparse(u).netloc.removeprefix("www.")) for u in n.get("feeds", [])]
    queries = [] if yandex_search_on() else n.get("search", []) + n.get("google_news", [])
    for q in queries:
        urls.append(("https://news.google.com/rss/search?q=" + urllib.parse.quote_plus(q + " when:2d")
                     + "&hl=ru&gl=RU&ceid=RU:ru", f"Google Новости «{q}»"))
    return urls


def parse_yandex_xml(xml: bytes) -> list[dict]:
    """Ответ Yandex Search (формат XML) -> [{title, summary, link, date}]."""
    root = xml_root(xml)
    err = root.find(".//error")
    if err is not None:
        raise RuntimeError("Yandex Search: " + "".join(err.itertext()).strip()[:200])
    out = []
    for doc in root.iter("doc"):
        t = doc.find("modtime")
        try:
            dt = datetime.strptime(t.text.strip(), "%Y%m%dT%H%M%S").replace(tzinfo=MSK) if t is not None else None
        except ValueError:
            dt = None
        passages = " ".join("".join(p.itertext()) for p in doc.iter("passage"))
        title = doc.find("title")
        out.append(dict(title=strip_html("".join(title.itertext()) if title is not None else ""),
                        summary=strip_html(passages), link=(doc.findtext("url") or "").strip(),
                        date=dt, source=(doc.findtext("domain") or "").strip(), source_url=""))
    return out


def yandex_search(net: Net, query: str, since: datetime) -> list[dict]:
    """Yandex Search API v2 (Яндекс Облако): свежие документы по запросу."""
    code, r = net.post_json(
        "https://searchapi.api.cloud.yandex.net/v2/web/search",
        {"query": {"searchType": "SEARCH_TYPE_RU",
                   "queryText": f"{query} date:>{since.astimezone(MSK):%Y%m%d}"},
         "sortSpec": {"sortMode": "SORT_MODE_BY_TIME", "sortOrder": "SORT_ORDER_DESC"},
         "groupSpec": {"groupMode": "GROUP_MODE_FLAT", "groupsOnPage": "30", "docsInGroup": "1"},
         "maxPassages": "2", "region": "225", "l10N": "LOCALIZATION_RU",
         "folderId": os.environ["YANDEX_FOLDER_ID"], "responseFormat": "FORMAT_XML"},
        {"Authorization": f"Api-Key {os.environ['YANDEX_SEARCH_API_KEY']}"}, timeout=40)
    if code != 200 or "rawData" not in r:
        raise RuntimeError(f"HTTP {code}: {str(r.get('message') or r)[:200]}")
    import base64
    return parse_yandex_xml(base64.b64decode(r["rawData"]))


def domain(url: str) -> str:
    return urllib.parse.urlparse(url or "").netloc.lower().removeprefix("www.")


def domain_in(d: str, domains: list[str]) -> bool:
    return any(d == o or d.endswith("." + o) for o in domains)


def collect_news(net, cfg, since, store, matcher, health: "Health | None" = None) -> list[dict]:
    """RSS-ленты и поиск. С сайтов рыночной аналитики ([news] analytics_domains) берём только
    материалы про СберИнвестиции — остальное там прогнозы и обзоры, не новости отрасли."""
    official = [d.lower() for d in cfg.get("news", {}).get("official_domains", [])]
    analytics = [d.lower() for d in cfg.get("news", {}).get("analytics_domains", [])]
    items = []
    for url, label in news_urls(cfg):
        try:
            code, body = net.get(url)
            if code != 200:
                raise RuntimeError(f"HTTP {code}")
            feed_title, entries = parse_feed(body)
        except Exception as e:  # noqa: BLE001
            log("warn", f"лента недоступна {label}: {e}")
            if health:
                health.record("rss:" + url, label, False, error=str(e))
            continue
        if health:
            health.record("rss:" + url, label, True)
        for en in entries:
            if not en["date"] or en["date"] < since or not en["link"]:
                continue
            key = "rss:" + en["link"]
            if store.seen(key):
                continue
            title, summary = en["title"], en["summary"]
            text = f"{title}. {summary}" if summary and summary[:40] not in title else title
            groups = matcher.match(text)
            d = domain(en.get("source_url") or en["link"])
            if domain_in(d, analytics) and SBER not in groups:
                continue
            if groups:
                kind = "официально" if domain_in(d, official) else "СМИ"
                items.append(dict(key=key, source=en["source"] or feed_title[:60] or "СМИ", text=text, kind=kind,
                                  url=en["link"], date=en["date"], groups=groups))
    if yandex_search_on():
        for q in cfg.get("news", {}).get("search", []):
            label = f"Яндекс «{q}»"
            try:
                entries = yandex_search(net, q, since)
            except Exception as e:  # noqa: BLE001
                log("warn", f"поиск недоступен {label}: {e}")
                if health:
                    health.record("ya:" + q, label, False, error=str(e))
                continue
            if health:
                health.record("ya:" + q, label, True)
            for en in entries:
                if not en["link"] or (en["date"] and en["date"] < since):
                    continue
                key = "rss:" + en["link"]
                if store.seen(key):
                    continue
                text = f"{en['title']}. {en['summary']}" if en["summary"] else en["title"]
                groups = matcher.match(text)
                d = domain(en["link"])
                if domain_in(d, analytics) and SBER not in groups:
                    continue
                if groups:
                    kind = "официально" if domain_in(d, official) else "СМИ"
                    items.append(dict(key=key, source=en["source"] or d, text=text, kind=kind, url=en["link"],
                                      date=en["date"] or datetime.now(timezone.utc), groups=groups))
    log("info", f"Новости: {len(items)} подходящих")
    return items


# ---- сводка ------------------------------------------------------------------
def dedup(items: list[dict]) -> list[dict]:
    out, hashes = [], set()
    for it in sorted(items, key=lambda x: x["date"]):
        h = text_hash(it["text"])
        if h not in hashes:
            hashes.add(h)
            it["hash"] = h
            out.append(it)
    return out


DEFAULT_SECTIONS = {
    "sber": "🟢 СБЕРИНВЕСТИЦИИ",
    "brokers": "🏦 БРОКЕРЫ",
    "regulation": "⚖️ РЕГУЛИРОВАНИЕ",
    "infrastructure": "📊 БИРЖИ И ИНФРАСТРУКТУРА",
    "events": "🎤 ОТРАСЛЕВЫЕ СОБЫТИЯ",
}
# группа первичного фильтра -> раздел (для выпуска без ChatGPT)
GROUP_SECTION = {"СберИнвестиции": "sber", "Брокеры": "brokers", "Регулирование": "regulation",
                 "Инфраструктура": "infrastructure", "Отраслевые события": "events"}
RULE = "━━━━━━━━━━━━━━━"
KIND_MARK = {"official": "🏛 ", "opinion": "💬 ", "ad": "📣 ", "media": ""}
LEGEND = "<i>🏛 официальное сообщение · 📣 промо · 💬 мнение</i>"

SELECT_FORMAT = """
ЗАДАЧА: строгий редакторский отбор. Ниже пронумерованные материалы-кандидаты (и список уже
опубликованного за последние дни). Для КАЖДОГО кандидата мысленно ответь на три вопроса:
  1) Это непосредственно о брокерских услугах, брокерском бизнесе, регулировании брокеров
     и их клиентов-инвесторов или о СберИнвестициях? (аналитика рынка, прогнозы, обзоры
     бумаг, торговые идеи — НЕТ, даже если их выпустил брокер);
  2) Это новое существенное событие или решение — не мнение, прогноз, пересказ старого
     и не повтор уже опубликованного?
  3) Можешь ли ты одной фразой сказать, зачем это знать руководителю СберИнвестиций?
Берёшь материал, только если на все три вопроса ответ уверенное «да». Сомневаешься — не бери.
Совершенно нормально взять 0–3 материала из сотни.
Ответ — строго JSON:
{"keep": [{"n": номер, "why": "зачем знать руководителю СберИнвестиций, до 15 слов"}],
 "near": [{"n": номер, "why": "почему не взят, до 10 слов"}]}
"near" — до 5 самых близких к отбору отклонённых материалов (для контроля пропусков).
"""

WRITE_FORMAT = """
ЗАДАЧА: собери выпуск канала из отобранных материалов — как профессиональный отраслевой
журналист: коротко, по делу, с фактами и цифрами, без воды, оценок и восклицаний.
Ответ — строго JSON:
{"main": [пункт, ...], "sections": {"<ключ раздела>": [пункт, ...]}}
пункт = {"title": "заголовок 3–7 слов, суть новости",
         "text": "одно предложение до 20 слов: главная цифра или деталь, без повтора заголовка",
         "src": [номера материалов], "kind": "official|media|ad|opinion",
         "why": "..." (только у пунктов "main")}
Ключи разделов: %s.
Правила:
- Не больше %d пунктов вместе с "main"; "main" — 1–3 самых важных события. Минимума нет:
  если отобрано 2 события — в выпуске 2 пункта.
- Перед ответом ещё раз проверь каждый пункт тремя вопросами отбора; не проходит — убери.
- КАЖДОЕ событие — РОВНО ОДИН пункт во всём выпуске: событие из "main" не повторяй в разделах;
  одно событие из разных источников — один пункт, все номера в "src".
- Только факты из материалов; оговорки («планирует», «по данным источников») сохраняй;
  мнения — kind = opinion с указанием автора; промо СберИнвестиций — kind = ad;
  сообщения компании, регулятора, биржи (в т.ч. пересказанные СМИ) — official.
- "why" — до 12 слов: что это значит для брокерского бизнеса Сбера. Только если связь
  очевидна; это оценка.
- Обычный текст без HTML, Markdown и ссылок: ссылки программа добавит по номерам.
"""


def sections(cfg: dict) -> dict:
    return cfg.get("sections") or DEFAULT_SECTIONS


def policy(cfg: dict) -> str:
    llm = cfg.get("llm", {})
    return llm.get("policy") or llm.get("prompt", "")


def chat_json(net: Net, cfg: dict, system: str, user: str, model: str | None = None) -> dict | None:
    """JSON-ответ модели. Если указанная модель недоступна аккаунту — запасная [llm] model."""
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        return None
    llm = cfg.get("llm", {})
    models = [m for m in dict.fromkeys([model or llm.get("model", "gpt-5-mini"), llm.get("model", "gpt-5-mini")]) if m]
    err = ""
    for mdl in models:
        code, r = net.post_json(llm.get("url", "https://api.openai.com/v1/chat/completions"),
                                {"model": mdl, "response_format": {"type": "json_object"},
                                 "messages": [{"role": "system", "content": system},
                                              {"role": "user", "content": user}]},
                                {"Authorization": f"Bearer {key}"}, timeout=int(llm.get("timeout", 300)))
        if code == 200:
            text = r["choices"][0]["message"]["content"].strip()
            text = re.sub(r"^```\w*\n?|```$", "", text).strip()
            return json.loads(text)
        err = f"OpenAI HTTP {code} ({mdl}): {str(r.get('error') or r)[:300]}"
        if code not in (400, 403, 404):        # не «нет такой модели» — запасная не поможет
            break
        log("warn", err)
    raise RuntimeError(err)


def material_lines(items: list[dict], max_chars: int) -> str:
    lines = []
    for i, it in enumerate(items, 1):
        t = re.sub(r"\s+", " ", it["text"])[:max_chars]
        lines.append(f"[{i}] {it['date'].astimezone(MSK):%d.%m %H:%M} | {it['source']} | "
                     f"тип: {it.get('kind', 'СМИ')}\n{t}")
    return "\n\n".join(lines)


def select_items(net: Net, cfg: dict, items: list[dict], published: list[str] | None = None) -> dict | None:
    """Этап 1: редактор отбирает материалы по трём вопросам. None — ключа OpenAI нет.
    -> {"keep": [(материал, зачем)], "near": [(материал, почему нет)]}"""
    d = cfg.get("digest", {})
    user = material_lines(items, int(d.get("max_chars", 600)))
    if published:
        user = "УЖЕ ОПУБЛИКОВАНО (не брать повторно):\n" + "\n".join(f"- {t}" for t in published) + \
            "\n\nКАНДИДАТЫ:\n" + user
    data = chat_json(net, cfg, policy(cfg) + SELECT_FORMAT, user, cfg.get("llm", {}).get("select_model"))
    if data is None:
        return None

    def pick(key):
        out, seen = [], set()
        for x in data.get(key) or []:
            n = x.get("n") if isinstance(x, dict) else x
            if str(n).isdigit() and 0 < int(n) <= len(items) and int(n) not in seen:
                seen.add(int(n))
                out.append((items[int(n) - 1], str(x.get("why", "")) if isinstance(x, dict) else ""))
        return out
    return {"keep": pick("keep"), "near": pick("near")}


def ask_llm(net: Net, cfg: dict, user: str) -> dict | None:
    """Этап 2: выпуск из отобранных материалов."""
    secs = sections(cfg)
    fmt = WRITE_FORMAT % (", ".join(f'"{k}" ({v})' for k, v in secs.items()),
                          int(cfg.get("digest", {}).get("max_points", 10)))
    return chat_json(net, cfg, policy(cfg) + fmt, user)


def sort_items(cfg: dict, items: list[dict]) -> list[dict]:
    order = list((cfg.get("keywords") or {}).keys())
    rank = lambda it: min(order.index(g) if g in order else 99 for g in it["groups"])  # noqa: E731
    return sorted(items, key=lambda it: (rank(it), -it["date"].timestamp()))


def header(title: str) -> str:
    return f"{RULE}\n<b>{html.escape(title)}</b>\n{RULE}"


def source_links(nums, items: list[dict], limit: int = 2) -> str:
    links, seen = [], set()
    for n in nums or []:
        try:
            it = items[int(n) - 1]
        except (ValueError, IndexError, TypeError):
            continue
        if it["url"] in seen:
            continue
        seen.add(it["url"])
        name = it["source"].removeprefix("TG: ")
        links.append(f'<a href="{html.escape(it["url"])}">{html.escape(name[:40])}</a>')
        if len(links) == limit:
            break
    return " · ".join(links)


def render(cfg: dict, data: dict, items: list[dict]) -> str:
    """JSON модели -> Telegram HTML. Дубли отсекаются ещё раз: пункт, все источники которого
    уже были в выпуске, — повтор того же события. Лимит пунктов соблюдается и здесь."""
    used: set[int] = set()
    limit = int(cfg.get("digest", {}).get("max_points", 10))
    count = 0
    blocks = []
    has_kind = False

    def points(lst, key: str = "") -> list[str]:
        nonlocal count, has_kind
        out = []
        for p in lst or []:
            if count >= limit:
                break
            if not isinstance(p, dict) or not str(p.get("text") or p.get("title") or "").strip():
                continue
            src = {int(n) for n in p.get("src") or [] if str(n).isdigit() and 0 < int(n) <= len(items)}
            if src and src <= used:
                continue
            used.update(src)
            title = str(p.get("title") or "").strip().rstrip(".")
            text = str(p.get("text") or "").strip()
            mark = KIND_MARK.get(str(p.get("kind", "")), "")
            has_kind = has_kind or bool(mark)
            line = f"{mark}<b>{html.escape(title)}</b>" if title else mark
            if text:
                line += ("\n" if title else "") + html.escape(text)
            links = source_links(p.get("src"), items)
            if links:
                line += f"\n→ {links}"
            why = str(p.get("why") or "").strip()
            if why and key == "main":
                line += f"\n<i>Для Сбера: {html.escape(why)}</i>"
            out.append(line)
            count += 1
        return out

    main = points(data.get("main"), "main")
    if main:
        blocks.append(header("🔥 ГЛАВНОЕ") + "\n\n" + "\n\n".join(main))
    for key, title in sections(cfg).items():
        pts = points((data.get("sections") or {}).get(key), key)
        if pts:
            blocks.append(header(title) + "\n\n" + "\n\n".join(pts))
    if blocks and has_kind:
        blocks.append(LEGEND)
    return "\n\n".join(blocks)


class EditorUnavailable(RuntimeError):
    """Модель не ответила: выпуск не публикуется (в канал — только редакторский выпуск)."""


def build_digest(net, cfg, items, since, now, published: list[str] | None = None) -> tuple[str, str, dict]:
    """-> (период, тело выпуска, решения редактора). Пустое тело — значимых новостей нет.
    Без редактора не публикуем ничего: EditorUnavailable."""
    period = f"{since.astimezone(MSK):%d.%m %H:%M} – {now.astimezone(MSK):%d.%m %H:%M} МСК"
    d = cfg.get("digest", {})
    items = sort_items(cfg, items)[:int(d.get("max_items", 250))]
    try:
        sel = select_items(net, cfg, items, published)
        if sel is None:
            raise EditorUnavailable("нет ключа OpenAI (OPENAI_API_KEY)")
        log("info", f"редактор отобрал {len(sel['keep'])} из {len(items)}")
        for it, why in sel["keep"]:
            short = re.sub(r"\s+", " ", it["text"])[:90]
            log("info", f"  + {it['source']}: {short} — {why}")
        if not sel["keep"]:
            return period, "", sel
        chosen = [it for it, _ in sel["keep"]]
        lines = material_lines(chosen, int(d.get("max_chars", 600)))
        notes = "\n".join(f"[{i}] зачем: {why}" for i, (_, why) in enumerate(sel["keep"], 1) if why)
        data = ask_llm(net, cfg, f"Период: {period}\n\n{lines}\n\nЗАМЕТКИ РЕДАКТОРА:\n{notes}")
        return period, render(cfg, data or {}, chosen), sel
    except EditorUnavailable:
        raise
    except Exception as e:  # noqa: BLE001
        raise EditorUnavailable(str(e)[:300]) from e




# ---- отправка ------------------------------------------------------------------
def split_message(text: str, limit: int = 3900) -> list[str]:
    parts, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(line[:limit])
            line = line[limit:]
        if cur and len(cur) + len(line) + 1 > limit:
            parts.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        parts.append(cur)
    return [p for p in parts if p.strip()]


def plain(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def news_chats() -> list[str]:
    """Куда слать: BROKERNEWS_CHAT_IDS (через запятую: канал для коллег, личные чаты), иначе чат робота."""
    ids = [c.strip() for c in (os.environ.get("BROKERNEWS_CHAT_IDS") or "").split(",") if c.strip()]
    return ids or [c for c in [os.environ.get("TELEGRAM_CHAT_ID", "").strip()] if c]


def send_telegram(net: Net, text: str) -> None:
    token, chats = os.environ.get("TELEGRAM_BOT_TOKEN", ""), news_chats()
    if not token or not chats:
        raise RuntimeError("нет TELEGRAM_BOT_TOKEN или получателей (BROKERNEWS_CHAT_IDS / TELEGRAM_CHAT_ID)")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    failed = []
    for chat in chats:
        try:
            for part in split_message(text):
                _, r = net.post_json(url, {"chat_id": chat, "text": part, "parse_mode": "HTML",
                                           "disable_web_page_preview": True})
                if not r.get("ok"):
                    log("warn", f"{chat}: Telegram не принял HTML ({r.get('description')}), отправляю текстом")
                    _, r = net.post_json(url, {"chat_id": chat, "text": plain(part),
                                               "disable_web_page_preview": True})
                    if not r.get("ok"):
                        raise RuntimeError(r.get("description") or str(r))
                time.sleep(0.5)
        except Exception as e:  # noqa: BLE001
            log("warn", f"не отправлено в {chat}: {e}")
            failed.append(chat)
    if len(failed) == len(chats):
        raise RuntimeError("дайджест не отправлен ни одному получателю")


def send_owner(net: Net, text: str) -> None:
    """Служебные предупреждения — только владельцу (чат робота), не в канал коллег."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("BROKERNEWS_OWNER_CHAT") or os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        log("warn", "предупреждение владельцу не отправлено: нет TELEGRAM_CHAT_ID")
        return
    try:
        net.post_json(f"https://api.telegram.org/bot{token}/sendMessage",
                      {"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True})
    except Exception as e:  # noqa: BLE001
        log("warn", f"предупреждение владельцу не отправлено: {e}")


def health_report(new: list[str], fixed: list[str]) -> str | None:
    parts = []
    if new:
        parts.append("Проблема:\n" + "\n".join(html.escape(x) for x in new))
    if fixed:
        parts.append("Снова работает:\n" + "\n".join(html.escape(x) for x in fixed))
    if not parts:
        return None
    return "<b>⚠️ Дайджест рынка — служебное</b>\n\n" + "\n\n".join(parts)


def publish_web(web: Path, entry: dict, keep: int = 90) -> None:
    """Архив дайджестов для страницы http://<сервер>:8080/news/ (рядом с «Помощником трейдера»)."""
    archive = STATE / "brokernews-digests.json"
    try:
        digests = json.loads(archive.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        digests = []
    digests = ([entry] + digests)[:keep]
    archive.write_text(json.dumps(digests, ensure_ascii=False), encoding="utf-8")
    web.mkdir(parents=True, exist_ok=True)
    shutil.copy(Path(__file__).with_name("index.html"), web / "index.html")
    tmp = web / "data.json.new"
    tmp.write_text(json.dumps({"updated": entry["at"], "digests": digests}, ensure_ascii=False), encoding="utf-8")
    tmp.replace(web / "data.json")


# ---- команды -------------------------------------------------------------------
def load_config(path: Path = CONFIG) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def cmd_run(hours: int | None, dry_run: bool, web: Path | None, review: bool = False) -> int:
    store = Store(STATE / "brokernews.db")
    try:
        return _run(store, hours, dry_run, web, review)
    finally:
        store.close()


def weekend_skip(cfg: dict, now: datetime) -> bool:
    """Плановый запуск в субботу или воскресенье (МСК) — пропустить. last_run не трогаем,
    поэтому выпуск понедельника соберёт всё с вечера пятницы."""
    return bool(cfg.get("digest", {}).get("weekdays_only", True)) and now.astimezone(MSK).weekday() >= 5


def published_titles(store: "Store", days: int = 7) -> list[str]:
    store.db.execute("CREATE TABLE IF NOT EXISTS published (title TEXT, ts INTEGER)")
    since = int(time.time()) - days * 86400
    return [t for (t,) in store.db.execute("SELECT title FROM published WHERE ts >= ? ORDER BY ts", (since,))]


def remember_published(store: "Store", body: str) -> None:
    store.db.execute("CREATE TABLE IF NOT EXISTS published (title TEXT, ts INTEGER)")
    now = int(time.time())
    titles = re.findall(r"<b>([^<]{4,120})</b>", body)
    titles = [t for t in titles if not t.startswith(("🔥", "🟢", "🏦", "⚖", "📊", "🎤"))]
    store.db.executemany("INSERT INTO published VALUES (?, ?)", [(html.unescape(t), now) for t in titles])
    store.db.execute("DELETE FROM published WHERE ts < ?", (now - 30 * 86400,))
    store.db.commit()


def review_text(period: str, body: str, sel: dict, total: int) -> str:
    """Разбор для владельца: черновик выпуска и решения редактора."""
    def row(it, why):
        t = html.escape(re.sub(r"\s+", " ", it["text"])[:110])
        return f'• <a href="{html.escape(it["url"])}">{html.escape(it["source"].removeprefix("TG: ")[:30])}</a>: {t}' + \
            (f"\n  <i>{html.escape(why)}</i>" if why else "")
    parts = [f"<b>🔎 Разбор выпуска · {period}</b>\nКандидатов после фильтра: {total}, взято: {len(sel['keep'])}"]
    if sel["keep"]:
        parts.append("<b>Взято</b>\n" + "\n".join(row(it, why) for it, why in sel["keep"]))
    if sel["near"]:
        parts.append("<b>Близко, но отклонено</b>\n" + "\n".join(row(it, why) for it, why in sel["near"]))
    parts.append("<b>Черновик выпуска</b>\n\n" + (body or "<i>значимых новостей нет — выпуск не отправлялся бы</i>"))
    return "\n\n".join(parts)


def _run(store: "Store", hours: int | None, dry_run: bool, web: Path | None, review: bool = False) -> int:
    cfg = load_config()
    if hours is None and not (dry_run or review) and weekend_skip(cfg, datetime.now(timezone.utc)):
        log("info", "выходной — выпуск не отправляю, новости войдут в выпуск понедельника")
        return 0
    matcher = Matcher(cfg)
    net = Net()
    now = datetime.now(timezone.utc)
    last = store.get("last_run")
    since = now - timedelta(hours=hours) if hours else \
        datetime.fromisoformat(last) if last else now - timedelta(hours=int(cfg.get("digest", {}).get("first_hours", 24)))
    final = not (dry_run or review)          # настоящий выпуск: отправка и пометки «отправлено»

    health = Health(store.db, cfg)
    items = collect_telegram(net, cfg, since, store, matcher, health) + \
        collect_news(net, cfg, since, store, matcher, health)
    items = [i for i in dedup(items) if not store.seen(i["hash"])]
    log("info", f"кандидатов после фильтра: {len(items)}")

    alerts_new, alerts_fixed = health.problems() if final else ([], [])
    sel = {"keep": [], "near": []}
    body, period = "", ""
    editor_ok = True
    if items:
        try:
            period, body, sel = build_digest(net, cfg, items, since, now, published_titles(store))
        except EditorUnavailable as e:
            editor_ok = False
            log("error", f"редактор недоступен, выпуск не публикуется: {e}")
    if final and health._flag("_editor", not editor_ok):
        (alerts_new if not editor_ok else alerts_fixed).append(
            "• редактор ChatGPT не отвечает — выпуски не публикуются" if not editor_ok else "• редактор ChatGPT снова отвечает")
    if final:
        report = health_report(alerts_new, alerts_fixed)
        if report:
            send_owner(net, report)
    if not editor_ok:
        return 0                              # материалы не помечаем: войдут в следующий выпуск

    text = f"<b>💼 Брокерский бизнес · {period}</b>\n\n{body}" if body.strip() else ""
    if review:
        send_owner(net, review_text(period or "—", text, sel, len(items)))
        log("info", "разбор отправлен владельцу")
        return 0
    if dry_run:
        print(plain(review_text(period or "—", text, sel, len(items))))
        return 0
    if text:
        send_telegram(net, text)
        remember_published(store, body)
        if web:
            publish_web(web, {"at": now.isoformat(timespec="seconds"), "period": period,
                              "count": len(items), "html": body})
        log("info", "выпуск отправлен")
    else:
        log("info", "значимых новостей нет — выпуск не отправляю")
    store.mark([i["key"] for i in items] + [i["hash"] for i in items])
    store.put("last_run", now.isoformat())
    return 0


def cmd_candidates(hours: int) -> int:
    """Кандидаты после первичного фильтра — без ChatGPT и без отправки (для настройки фильтра)."""
    cfg = load_config()
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "tmp.db")
        net, matcher = Net(), Matcher(cfg)
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        health = Health(store.db, cfg)
        items = dedup(collect_telegram(net, cfg, since, store, matcher, health) +
                      collect_news(net, cfg, since, store, matcher, health))
        for it in sort_items(cfg, items):
            t = re.sub(r"\s+", " ", it["text"])[:160]
            print(f"- [{', '.join(it['groups'])}] {it['source']} | {t}")
        print(f"\nКандидатов: {len(items)}")
        bad = [f"{lbl}: {err[:60]}" for src, (ok, err) in health.run.items() if not ok
               for (lbl,) in store.db.execute("SELECT label FROM health WHERE src=?", (src,))]
        print("Не ответили: " + ("; ".join(bad) if bad else "нет"))
        store.close()
    return 0


def cmd_channels() -> int:
    cfg, net, ok = load_config(), Net(), 0
    store = Store(STATE / "brokernews.db")
    names = channel_names(cfg)
    try:
        for entry, _ in names:
            try:
                name, title, posts = tg_resolve(net, store, entry)
            except Exception as e:  # noqa: BLE001
                name, title, posts = entry, str(e)[:60], []
            if posts:
                ok += 1
                last = max(p["date"] for p in posts).astimezone(MSK)
                print(f"  OK   @{name:<26} {title} (последний пост {last:%d.%m.%Y})")
            else:
                print(f"  НЕТ  @{entry.replace('|', ' / @'):<26} не найден или без публичной веб-версии")
            time.sleep(0.4)
    finally:
        store.close()
    print(f"Работают {ok} из {len(names)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="brokernews")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="собрать и отправить выпуск в канал")
    r.add_argument("--hours", type=int, help="за сколько часов (по умолчанию — с прошлого запуска)")
    r.add_argument("--dry-run", action="store_true", help="показать разбор здесь, ничего не отправлять")
    r.add_argument("--web", type=Path, help="папка страницы архива (необязательно)")
    v = sub.add_parser("review", help="разбор выпуска владельцу в Telegram, в канал ничего не уходит")
    v.add_argument("--hours", type=int, default=24)
    c = sub.add_parser("candidates", help="кандидаты после фильтра, без ChatGPT и отправки")
    c.add_argument("--hours", type=int, default=72)
    sub.add_parser("channels", help="проверить Telegram-каналы")
    a = p.parse_args(argv)
    if a.cmd == "channels":
        return cmd_channels()
    if a.cmd == "candidates":
        return cmd_candidates(a.hours)
    if a.cmd == "review":
        return cmd_run(a.hours, False, None, review=True)
    return cmd_run(a.hours, a.dry_run, a.web)


if __name__ == "__main__":
    sys.exit(main())
