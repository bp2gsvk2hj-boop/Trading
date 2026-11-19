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
    """Контроль источников: молчащие, недоступные, пустые. Владельцу — только новые проблемы
    и восстановления, не каждый запуск."""

    def __init__(self, db: sqlite3.Connection, cfg: dict):
        self.db = db
        self.db.execute("""CREATE TABLE IF NOT EXISTS health (src TEXT PRIMARY KEY, label TEXT,
            fails INTEGER DEFAULT 0, last_ok TEXT, last_post TEXT, problem TEXT DEFAULT '')""")
        self.silence = timedelta(days=int(cfg.get("health", {}).get("silence_days", 7)))
        self.min_fails = int(cfg.get("health", {}).get("fail_runs", 2))
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

    def problems(self) -> tuple[list[str], list[str]]:
        """-> (новые проблемы, восстановившиеся). Запоминает текущее состояние."""
        now = datetime.now(timezone.utc)
        new, fixed = [], []
        for src, label, fails, last_post, prev in self.db.execute(
                "SELECT src, label, fails, last_post, problem FROM health").fetchall():
            if src == "_tg_mass":
                continue
            if src not in self.run:          # источник убрали из настроек
                self.db.execute("DELETE FROM health WHERE src=?", (src,))
                continue
            problem = ""
            if fails >= self.min_fails:
                problem = f"не загружается {fails} запуска подряд ({self.run[src][1][:80] or 'нет данных'})"
            elif src.startswith("tg:") and last_post and now - datetime.fromisoformat(last_post) > self.silence:
                days = (now - datetime.fromisoformat(last_post)).days
                problem = f"молчит {days} дн."
            if problem and not prev:
                new.append(f"• {label}: {problem}")
            elif prev and not problem:
                fixed.append(f"• {label}")
            self.db.execute("UPDATE health SET problem=? WHERE src=?", (problem, src))
        tg = [ok for src, (ok, _) in self.run.items() if src.startswith("tg:")]
        mass = len(tg) >= 4 and tg.count(False) >= len(tg) / 2
        row = self.db.execute("SELECT problem FROM health WHERE src='_tg_mass'").fetchone()
        if mass and not (row and row[0]):
            new.insert(0, f"• Telegram: не читаются {tg.count(False)} из {len(tg)} каналов сразу — "
                          "t.me недоступен через VPN или сменилась вёрстка страниц")
        elif row and row[0] and not mass:
            fixed.insert(0, "• Telegram: каналы снова читаются")
        self.db.execute("INSERT OR REPLACE INTO health (src, label, problem) VALUES ('_tg_mass', 'Telegram', ?)",
                        ("mass" if mass else "",))
        self.db.commit()
        return new, fixed


def text_hash(text: str) -> str:
    norm = re.sub(r"\W+", "", text.lower())[:160]
    return "h:" + hashlib.md5(norm.encode()).hexdigest()


# ---- фильтр по ключевым словам ------------------------------------------------
class Matcher:
    def __init__(self, cfg: dict):
        f = re.IGNORECASE | re.DOTALL   # .{0,80} ловит слова и на соседних строках поста
        self.groups = {g: [re.compile(p, f) for p in pats] for g, pats in (cfg.get("keywords") or {}).items()}
        self.exclude = [re.compile(p, f) for p in cfg.get("exclude", {}).get("patterns", [])]

    def match(self, text: str) -> list[str]:
        if any(p.search(text) for p in self.exclude):
            return []
        return [g for g, pats in self.groups.items() if any(p.search(text) for p in pats)]


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


def channel_names(cfg: dict) -> list[tuple[str, bool]]:
    """Каналы из настроек: [(имя, брать_всё)]. Имя может быть списком вариантов «a|b|c» —
    если точное @имя неизвестно; читается первый вариант, у которого есть посты."""
    t = cfg.get("telegram", {})
    out, seen = [], set()
    for names, take_all in ((t.get("channels_all", []), True), (t.get("channels", []), False)):
        for n in names:
            alts = [a.rsplit("/", 1)[-1].lstrip("@").strip() for a in str(n).split("|")]
            n = "|".join(a for a in alts if a)
            if n and n.lower() not in seen:
                seen.add(n.lower())
                out.append((n, take_all))
    return out


def tg_resolve(net: Net, store: "Store | None", entry: str, before=None):
    """Вариант @имени с постами -> (имя, заголовок, посты). Рабочий вариант запоминается."""
    alts = entry.split("|")
    known = store.get("tgname:" + entry.lower()) if store and len(alts) > 1 else None
    if known in alts:
        alts.remove(known)
        alts.insert(0, known)
    for name in alts:
        title, posts = tg_page(net, name, before)
        if posts:
            if store and len(entry.split("|")) > 1:
                store.put("tgname:" + entry.lower(), name)
            return name, title, posts
        time.sleep(0.3)
    return alts[0], None, []


def collect_telegram(net, cfg, since, store, matcher, health: "Health | None" = None) -> list[dict]:
    pages_max = int(cfg.get("telegram", {}).get("max_pages", 3))
    official = {n.lower().lstrip("@") for n in cfg.get("telegram", {}).get("official", [])}
    items, bad = [], []
    for entry, take_all in channel_names(cfg):
        name = entry.split("|")[0]
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
                health.record(f"tg:{entry.lower()}", label, bool(posts),
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
                groups = matcher.match(p["text"]) or (["Прочее"] if take_all else [])
                if groups:
                    items.append(dict(key=key, source=f"TG: {title or name}", text=p["text"], kind=kind,
                                      url=f"https://t.me/{name}/{p['id']}", date=p["date"], groups=groups))
        except Exception as e:  # noqa: BLE001
            log("warn", f"t.me/s/{name}: {e}")
            if health:
                health.record(f"tg:{entry.lower()}", f"@{name}", False, error=str(e))
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


def news_urls(cfg: dict) -> list[tuple[str, str]]:
    """-> [(url, подпись для отчёта об источниках)]."""
    n = cfg.get("news", {})
    urls = [(u, urllib.parse.urlparse(u).netloc.removeprefix("www.")) for u in n.get("feeds", [])]
    for q in n.get("google_news", []):
        urls.append(("https://news.google.com/rss/search?q=" + urllib.parse.quote_plus(q + " when:2d")
                     + "&hl=ru&gl=RU&ceid=RU:ru", f"Google Новости «{q}»"))
    return urls


def domain(url: str) -> str:
    return urllib.parse.urlparse(url or "").netloc.lower().removeprefix("www.")


def collect_news(net, cfg, since, store, matcher, health: "Health | None" = None) -> list[dict]:
    official = [d.lower() for d in cfg.get("news", {}).get("official_domains", [])]
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
            if groups:
                d = domain(en.get("source_url") or en["link"])
                kind = "официально" if any(d == o or d.endswith("." + o) for o in official) else "СМИ"
                items.append(dict(key=key, source=en["source"] or feed_title[:60] or "СМИ", text=text, kind=kind,
                                  url=en["link"], date=en["date"], groups=groups))
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
    "sber": "🟢 СБЕР",
    "reputation": "⚠️ РЕПУТАЦИЯ СБЕРА",
    "competitors": "🏦 КОНКУРЕНТЫ",
    "market": "📈 РЫНОК И РЕГУЛИРОВАНИЕ",
    "events": "🎤 КОНФЕРЕНЦИИ И ФОРУМЫ",
    "telegram": "👀 ЧТО ОБСУЖДАЮТ В TELEGRAM",
}
GROUP_SECTION = {"Сбер": "sber", "Конкуренты": "competitors", "Конференции": "events"}  # остальное -> market
RULE = "━━━━━━━━━━━━━━━"
KIND_MARK = {"official": "🏛 ", "opinion": "💬 ", "ad": "📣 Посев: ", "media": ""}
LEVEL_MARK = {"mass": "массовая проблема", "repeated": "повторяющиеся жалобы", "single": "единичная претензия"}
LEGEND = "<i>🏛 официально · 💬 мнение или слух · 📣 посев (реклама) · без значка — СМИ</i>"

JSON_FORMAT = """
Ответ — строго JSON-объект без пояснений:
{"main": [пункт, ...], "sections": {"<ключ раздела>": [пункт, ...]}}
пункт = {"text": "...", "src": [номера материалов], "kind": "official|media|opinion|ad",
         "level": "mass|repeated|single" (только в разделе reputation),
         "why": "..." (только у 2–3 самых важных пунктов "main")}.
Ключи разделов: %s.
- "main" — 3–5 самых значимых событий. КАЖДОЕ событие встречается в ответе РОВНО ОДИН раз:
  событие из "main" не повторяй в разделах; одно событие из разных источников — один пункт,
  все его номера в "src" (самые информативные первыми).
- "text" — 1–2 предложения: только факты из материалов, с цифрами. Обычный текст, без HTML,
  Markdown и ссылок (ссылки программа добавит по номерам).
- "kind": official — сообщение самой компании, регулятора, биржи; media — публикация СМИ;
  opinion — мнение, оценка, слух или прогноз автора канала; ad — посев, реклама, промо.
  Тип материала указан в списке, но решай по сути: пересказ пресс-релиза в СМИ — official,
  колонка аналитика в СМИ — opinion.
- "level" для reputation: mass — подтверждённый массовый сбой или проблема; repeated — несколько
  независимых жалоб на одно и то же; single — единичная публичная претензия. Не преувеличивай масштаб.
- "why" — одно предложение: чем событие важно для брокерского бизнеса Сбера (конкуренция,
  продукт, регулирование, клиенты, репутация). Это оценка, а не факт; не пиши, если связь неочевидна.
"""


def sections(cfg: dict) -> dict:
    return cfg.get("sections") or DEFAULT_SECTIONS


def ask_llm(net: Net, cfg: dict, user: str) -> dict | None:
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        return None
    llm = cfg.get("llm", {})
    secs = sections(cfg)
    system = llm.get("prompt", "") + JSON_FORMAT % ", ".join(f'"{k}" ({v})' for k, v in secs.items())
    code, r = net.post_json(llm.get("url", "https://api.openai.com/v1/chat/completions"),
                            {"model": llm.get("model", "gpt-5-mini"),
                             "response_format": {"type": "json_object"},
                             "messages": [{"role": "system", "content": system},
                                          {"role": "user", "content": user}]},
                            {"Authorization": f"Bearer {key}"}, timeout=int(llm.get("timeout", 300)))
    if code != 200:
        raise RuntimeError(f"OpenAI HTTP {code}: {str(r.get('error') or r)[:300]}")
    text = r["choices"][0]["message"]["content"].strip()
    text = re.sub(r"^```\w*\n?|```$", "", text).strip()
    return json.loads(text)


def sort_items(cfg: dict, items: list[dict]) -> list[dict]:
    order = list((cfg.get("keywords") or {}).keys())
    rank = lambda it: min(order.index(g) if g in order else 99 for g in it["groups"])  # noqa: E731
    return sorted(items, key=lambda it: (rank(it), -it["date"].timestamp()))


def header(title: str) -> str:
    return f"{RULE}\n<b>{html.escape(title)}</b>\n{RULE}"


def source_links(nums, items: list[dict], limit: int = 3) -> str:
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
    уже были в дайджесте, — повтор того же события."""
    used: set[int] = set()
    blocks = []

    def points(lst, key: str = "") -> list[str]:
        out = []
        for p in lst or []:
            if not isinstance(p, dict) or not str(p.get("text", "")).strip():
                continue
            src = {int(n) for n in p.get("src") or [] if str(n).isdigit() and 0 < int(n) <= len(items)}
            if src and src <= used:
                continue
            used.update(src)
            text = str(p["text"]).strip()
            mark = KIND_MARK.get(str(p.get("kind", "")), "")
            if mark.startswith("📣") and text.lower().startswith("посев"):
                text = text.split(":", 1)[-1].strip() or text
            level = LEVEL_MARK.get(str(p.get("level", ""))) if key == "reputation" else None
            line = f"• {mark}" + (f"<b>[{level}]</b> " if level else "") + html.escape(text)
            links = source_links(p.get("src"), items)
            if links:
                line += f" — {links}"
            why = str(p.get("why") or "").strip()
            if why and key == "main":
                line += f"\n   ↳ <i>Для Сбера (оценка): {html.escape(why)}</i>"
            out.append(line)
        return out

    main = points(data.get("main"), "main")
    if main:
        blocks.append(header("🔥 ГЛАВНОЕ") + "\n" + "\n\n".join(main))
    for key, title in sections(cfg).items():
        pts = points((data.get("sections") or {}).get(key), key)
        if pts:
            blocks.append(header(title) + "\n" + "\n\n".join(pts))
    if blocks:
        blocks.append(LEGEND)
    return "\n\n".join(blocks)


def fallback_digest(items: list[dict], limit: int = 80, cfg: dict | None = None) -> str:
    secs = sections(cfg or {})
    by_sec: dict[str, list[dict]] = {}
    for it in items[:limit]:
        by_sec.setdefault(GROUP_SECTION.get(it["groups"][0], "market"), []).append(it)
    blocks = []
    for key, title in secs.items():
        lst = by_sec.get(key)
        if not lst:
            continue
        pts = []
        for it in lst:
            text = html.escape(re.sub(r"\s+", " ", it["text"])[:220])
            link = f'<a href="{html.escape(it["url"])}">{html.escape(it["source"].removeprefix("TG: "))}</a>'
            pts.append(f"• {text} — {link}")
        blocks.append(header(title) + "\n" + "\n".join(pts))
    if len(items) > limit:
        blocks.append(f"…и ещё {len(items) - limit}")
    return "\n\n".join(blocks)


def build_digest(net, cfg, items, since, now) -> tuple[str, str]:
    period = f"{since.astimezone(MSK):%d.%m %H:%M} – {now.astimezone(MSK):%d.%m %H:%M} МСК"
    d = cfg.get("digest", {})
    items = sort_items(cfg, items)[:int(d.get("max_items", 150))]
    lines = []
    for i, it in enumerate(items, 1):
        t = re.sub(r"\s+", " ", it["text"])[:int(d.get("max_chars", 600))]
        lines.append(f"[{i}] {it['date'].astimezone(MSK):%d.%m %H:%M} | {it['source']} | "
                     f"тип: {it.get('kind', 'СМИ')} | {', '.join(it['groups'])}\n{t}")
    body = None
    try:
        data = ask_llm(net, cfg, f"Период: {period}\n\n" + "\n\n".join(lines))
        body = render(cfg, data, items) if data else None
    except Exception as e:  # noqa: BLE001
        log("warn", f"сводка ChatGPT не получилась, отправляю список: {e}")
        LLM_ERRORS.append(str(e)[:200])
    return period, body or fallback_digest(items, cfg=cfg)


LLM_ERRORS: list[str] = []


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


def health_report(new: list[str], fixed: list[str], llm_errors: list[str]) -> str | None:
    parts = []
    if new:
        parts.append("Проблемы с источниками:\n" + "\n".join(html.escape(x) for x in new))
    if llm_errors:
        parts.append("Сводка ChatGPT не получилась, дайджест ушёл списком ссылок:\n• "
                     + html.escape(llm_errors[-1]))
    if fixed:
        parts.append("Снова работают:\n" + "\n".join(html.escape(x) for x in fixed))
    if not parts:
        return None
    return "<b>⚠️ Дайджест рынка — служебное</b>\n\n" + "\n\n".join(parts) + \
        "\n\n<i>Проверить каналы: python3 -m brokernews channels</i>"


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


def cmd_run(hours: int | None, dry_run: bool, web: Path | None) -> int:
    store = Store(STATE / "brokernews.db")
    try:
        return _run(store, hours, dry_run, web)
    finally:
        store.close()


def _run(store: "Store", hours: int | None, dry_run: bool, web: Path | None) -> int:
    cfg = load_config()
    matcher = Matcher(cfg)
    net = Net()
    now = datetime.now(timezone.utc)
    last = store.get("last_run")
    since = now - timedelta(hours=hours) if hours else \
        datetime.fromisoformat(last) if last else now - timedelta(hours=int(cfg.get("digest", {}).get("first_hours", 24)))

    health = Health(store.db, cfg)
    LLM_ERRORS.clear()
    items = collect_telegram(net, cfg, since, store, matcher, health) + \
        collect_news(net, cfg, since, store, matcher, health)
    items = [i for i in dedup(items) if not store.seen(i["hash"])]
    log("info", f"итого без дублей: {len(items)}")
    if not items and not dry_run:
        log("info", "ничего нового — дайджест не отправляю")
        report = health_report(*health.problems(), [])
        if report:
            send_owner(net, report)
        store.put("last_run", now.isoformat())
        return 0
    period, body = build_digest(net, cfg, items, since, now)
    text = f"<b>📊 Брокерский рынок · {period}</b>\nМатериалов: {len(items)}\n\n{body}"
    if dry_run:
        print(plain(text))
        return 0
    send_telegram(net, text)
    report = health_report(*health.problems(), LLM_ERRORS)
    if report:
        send_owner(net, report)
    store.mark([i["key"] for i in items] + [i["hash"] for i in items])
    store.put("last_run", now.isoformat())
    if web:
        publish_web(web, {"at": now.isoformat(timespec="seconds"), "period": period,
                          "count": len(items), "html": body})
    log("info", "дайджест отправлен")
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
    r = sub.add_parser("run", help="собрать и отправить дайджест")
    r.add_argument("--hours", type=int, help="за сколько часов (по умолчанию — с прошлого запуска)")
    r.add_argument("--dry-run", action="store_true", help="показать, не отправлять")
    r.add_argument("--web", type=Path, help="папка страницы архива (TRADER_WEB/news)")
    sub.add_parser("channels", help="проверить Telegram-каналы")
    a = p.parse_args(argv)
    if a.cmd == "channels":
        return cmd_channels()
    return cmd_run(a.hours, a.dry_run, a.web)


if __name__ == "__main__":
    sys.exit(main())
