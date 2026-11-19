"""Дайджест брокерского рынка: Telegram-каналы + новости СМИ -> сводка ChatGPT -> Telegram.

python3 -m brokernews run [--hours N] [--dry-run] [--web DIR]
python3 -m brokernews channels

Настройки — brokernews.toml в корне репозитория. Без внешних зависимостей: HTTP через curl
(как robot/telegram.py — urllib не умеет SOCKS), сначала через выходы VPN на сервере
(TELEGRAM_PROXY, иначе 127.0.0.1:10810/10811), затем напрямую. Xray сам отправляет российские
сайты напрямую, остальное — через зарубежный сервер.
Окружение (/etc/trading-robot.env): TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, OPENAI_API_KEY.
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


def text_hash(text: str) -> str:
    norm = re.sub(r"\W+", "", text.lower())[:160]
    return "h:" + hashlib.md5(norm.encode()).hexdigest()


# ---- фильтр по ключевым словам ------------------------------------------------
class Matcher:
    def __init__(self, cfg: dict):
        f = re.IGNORECASE
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
    t = cfg.get("telegram", {})
    out, seen = [], set()
    for names, take_all in ((t.get("channels_all", []), True), (t.get("channels", []), False)):
        for n in names:
            n = str(n).rsplit("/", 1)[-1].lstrip("@").strip()
            if n and n.lower() not in seen:
                seen.add(n.lower())
                out.append((n, take_all))
    return out


def collect_telegram(net, cfg, since, store, matcher) -> list[dict]:
    pages_max = int(cfg.get("telegram", {}).get("max_pages", 3))
    items, bad = [], []
    for name, take_all in channel_names(cfg):
        try:
            title, posts, before = None, [], None
            for _ in range(pages_max):
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
            if not posts:
                bad.append(name)
                continue
            for p in posts:
                if p["date"] < since or len(p["text"]) < 20:
                    continue
                key = f"tg:{name.lower()}:{p['id']}"
                if store.seen(key):
                    continue
                groups = matcher.match(p["text"]) or (["Прочее"] if take_all else [])
                if groups:
                    items.append(dict(key=key, source=f"TG: {title or name}", text=p["text"],
                                      url=f"https://t.me/{name}/{p['id']}", date=p["date"], groups=groups))
        except Exception as e:  # noqa: BLE001
            log("warn", f"t.me/s/{name}: {e}")
        time.sleep(0.4)
    if bad:
        log("warn", "каналы не найдены или без веб-версии: " + ", ".join(bad))
    log("info", f"Telegram: {len(items)} подходящих постов")
    return items


# ---- новости: RSS и Google Новости -------------------------------------------------
def strip_html(s: str | None) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def parse_feed(body: bytes) -> tuple[str, list[dict]]:
    """RSS 2.0 и Atom -> (название ленты, [{title, summary, link, date, source}])."""
    root = ET.fromstring(body)
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
                        source=(e.findtext("source") or "").strip()))
    return title, out


def news_urls(cfg: dict) -> list[str]:
    n = cfg.get("news", {})
    urls = list(n.get("feeds", []))
    for q in n.get("google_news", []):
        urls.append("https://news.google.com/rss/search?q=" + urllib.parse.quote_plus(q + " when:2d")
                    + "&hl=ru&gl=RU&ceid=RU:ru")
    return urls


def collect_news(net, cfg, since, store, matcher) -> list[dict]:
    items = []
    for url in news_urls(cfg):
        try:
            code, body = net.get(url)
            if code != 200:
                raise RuntimeError(f"HTTP {code}")
            feed_title, entries = parse_feed(body)
        except Exception as e:  # noqa: BLE001
            log("warn", f"лента недоступна {url[:80]}: {e}")
            continue
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
                items.append(dict(key=key, source=en["source"] or feed_title[:60] or "СМИ", text=text,
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


def ask_llm(net: Net, cfg: dict, user: str) -> str | None:
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        return None
    llm = cfg.get("llm", {})
    code, r = net.post_json(llm.get("url", "https://api.openai.com/v1/chat/completions"),
                            {"model": llm.get("model", "gpt-5-mini"),
                             "messages": [{"role": "system", "content": llm.get("prompt", "")},
                                          {"role": "user", "content": user}]},
                            {"Authorization": f"Bearer {key}"}, timeout=int(llm.get("timeout", 300)))
    if code != 200:
        raise RuntimeError(f"OpenAI HTTP {code}: {str(r.get('error') or r)[:300]}")
    text = r["choices"][0]["message"]["content"].strip()
    return re.sub(r"^```\w*\n?|```$", "", text).strip()


def sort_items(cfg: dict, items: list[dict]) -> list[dict]:
    order = list((cfg.get("keywords") or {}).keys())
    rank = lambda it: min(order.index(g) if g in order else 99 for g in it["groups"])  # noqa: E731
    return sorted(items, key=lambda it: (rank(it), -it["date"].timestamp()))


def fallback_digest(items: list[dict], limit: int = 80) -> str:
    out, cur = [], None
    for it in items[:limit]:
        g = it["groups"][0]
        if g != cur:
            out.append(f"\n<b>{html.escape(g)}</b>")
            cur = g
        t = html.escape(re.sub(r"\s+", " ", it["text"])[:220])
        out.append(f"• {t} — <a href=\"{html.escape(it['url'])}\">{html.escape(it['source'])}</a>")
    if len(items) > limit:
        out.append(f"\n…и ещё {len(items) - limit}")
    return "\n".join(out).strip()


def build_digest(net, cfg, items, since, now) -> tuple[str, str]:
    period = f"{since.astimezone(MSK):%d.%m %H:%M} – {now.astimezone(MSK):%d.%m %H:%M} МСК"
    items = sort_items(cfg, items)
    d = cfg.get("digest", {})
    lines = []
    for i, it in enumerate(items[:int(d.get("max_items", 120))], 1):
        t = re.sub(r"\s+", " ", it["text"])[:int(d.get("max_chars", 600))]
        lines.append(f"[{i}] {it['date'].astimezone(MSK):%d.%m %H:%M} | {it['source']} | "
                     f"{', '.join(it['groups'])} | {it['url']}\n{t}")
    body = None
    try:
        body = ask_llm(net, cfg, f"Период: {period}\n\n" + "\n\n".join(lines))
    except Exception as e:  # noqa: BLE001
        log("warn", f"сводка ChatGPT не получилась, отправляю список: {e}")
    return period, body or fallback_digest(items)


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


def send_telegram(net: Net, text: str) -> None:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN", ""), os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        raise RuntimeError("нет TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for part in split_message(text):
        _, r = net.post_json(url, {"chat_id": chat, "text": part, "parse_mode": "HTML",
                                   "disable_web_page_preview": True})
        if not r.get("ok"):
            log("warn", f"Telegram не принял HTML ({r.get('description')}), отправляю текстом")
            _, r = net.post_json(url, {"chat_id": chat, "text": plain(part), "disable_web_page_preview": True})
            if not r.get("ok"):
                raise RuntimeError(f"Telegram: {r.get('description') or r}")
        time.sleep(0.5)


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

    items = collect_telegram(net, cfg, since, store, matcher) + collect_news(net, cfg, since, store, matcher)
    items = [i for i in dedup(items) if not store.seen(i["hash"])]
    log("info", f"итого без дублей: {len(items)}")
    if not items and not dry_run:
        log("info", "ничего нового — дайджест не отправляю")
        store.put("last_run", now.isoformat())
        return 0
    period, body = build_digest(net, cfg, items, since, now)
    text = f"<b>📊 Брокерский рынок · {period}</b>\nМатериалов: {len(items)}\n\n{body}"
    if dry_run:
        print(plain(text))
        return 0
    send_telegram(net, text)
    store.mark([i["key"] for i in items] + [i["hash"] for i in items])
    store.put("last_run", now.isoformat())
    if web:
        publish_web(web, {"at": now.isoformat(timespec="seconds"), "period": period,
                          "count": len(items), "html": body})
    log("info", "дайджест отправлен")
    return 0


def cmd_channels() -> int:
    cfg, net, ok = load_config(), Net(), 0
    names = channel_names(cfg)
    for name, _ in names:
        try:
            title, posts = tg_page(net, name)
        except Exception as e:  # noqa: BLE001
            title, posts = str(e)[:60], []
        if posts:
            ok += 1
            last = max(p["date"] for p in posts).astimezone(MSK)
            print(f"  OK   @{name:<26} {title} (последний пост {last:%d.%m.%Y})")
        else:
            print(f"  НЕТ  @{name:<26} не найден или без публичной веб-версии")
        time.sleep(0.4)
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
