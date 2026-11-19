#!/usr/bin/env python3
"""Мониторинг брокерского рынка: Telegram-каналы + новости -> дайджест в Telegram.

Публичные каналы читаются через веб-версию t.me/s/<канал> — без входа в Telegram.
Если выполнен вход (команда login), дополнительно читаются все подписки аккаунта.

Команды:
  python monitor.py run [--hours N] [--dry-run]   собрать и отправить дайджест
  python monitor.py channels                      проверить каналы из config.yaml
  python monitor.py login                         вход в Telegram (необязательно)
Рабочая папка (настройки, ключи, состояние): $BM_HOME, по умолчанию папка скрипта.
"""
import argparse
import asyncio
import hashlib
import html
import logging
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import feedparser
import httpx
import yaml

BASE = Path(os.environ.get("BM_HOME") or Path(__file__).resolve().parent)
ENV_PATH = BASE / ".env"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}
MSK = ZoneInfo("Europe/Moscow")
log = logging.getLogger("monitor")


# ---------- настройки ----------

def load_env():
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def set_env_value(key, value):
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    lines = [l for l in lines if not l.startswith(key + "=")]
    lines.append(f"{key}={value}")
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(ENV_PATH, 0o600)
    os.environ[key] = value


def env(key, default=""):
    return os.environ.get(key, default).strip()


def http_proxy(key=""):
    """Прокси для httpx: свой (LLM_PROXY и т.п.) или общий PROXY. socks5h -> socks5."""
    p = (env(key) if key else "") or env("PROXY")
    return p.replace("socks5h://", "socks5://") if p else None


def http_client(timeout=30, key=""):
    return httpx.Client(proxy=http_proxy(key), timeout=timeout, follow_redirects=True, headers=UA)


def load_config():
    return yaml.safe_load((BASE / "config.yaml").read_text(encoding="utf-8"))


# ---------- хранилище (дубли и время последнего запуска) ----------

class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS seen (key TEXT PRIMARY KEY, ts INTEGER)")
        self.db.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
        self.db.commit()

    def seen(self, key):
        return self.db.execute("SELECT 1 FROM seen WHERE key=?", (key,)).fetchone() is not None

    def mark(self, keys):
        now = int(time.time())
        self.db.executemany("INSERT OR IGNORE INTO seen VALUES (?,?)", [(k, now) for k in keys])
        self.db.execute("DELETE FROM seen WHERE ts < ?", (now - 60 * 86400,))
        self.db.commit()

    def get(self, k):
        r = self.db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return r[0] if r else None

    def put(self, k, v):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (k, v))
        self.db.commit()


def text_hash(text):
    norm = re.sub(r"\W+", "", text.lower())[:160]
    return "h:" + hashlib.md5(norm.encode()).hexdigest()


# ---------- фильтр по ключевым словам ----------

class Matcher:
    def __init__(self, conf):
        flags = re.IGNORECASE | re.UNICODE
        self.groups = {g: [re.compile(p, flags) for p in pats]
                       for g, pats in (conf.get("keywords") or {}).items()}
        self.exclude = [re.compile(p, flags) for p in (conf.get("exclude") or [])]

    def match(self, text):
        if any(p.search(text) for p in self.exclude):
            return []
        return [g for g, pats in self.groups.items() if any(p.search(text) for p in pats)]


# ---------- сбор: Telegram ----------

class TgWebPage(HTMLParser):
    """Разбор страницы https://t.me/s/<канал>: посты, тексты, даты."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = None
        self.posts = []          # dict(id, text, date)
        self._cur = None
        self._depth = 0          # глубина div внутри текста поста
        self._in_title = False

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

    def result(self):
        out = []
        for p in self.posts:
            text = re.sub(r"[ \t]+", " ", "".join(p["parts"])).strip()
            if p["date"]:
                out.append({"id": p["id"], "text": text, "date": p["date"]})
        return out


def tg_web_fetch(c, name, before=None):
    url = f"https://t.me/s/{name}" + (f"?before={before}" if before else "")
    r = c.get(url)
    if r.status_code != 200 or "/s/" not in str(r.url):
        return None, []          # канал не найден, закрыт или без веб-версии
    page = TgWebPage()
    page.feed(r.text)
    return page.title, page.result()


def collect_telegram_web(conf, since, store, matcher, skip_names=()):
    tconf = conf.get("telegram") or {}
    lists = [(n, False) for n in tconf.get("channels") or []] + [(n, True) for n in tconf.get("channels_all") or []]
    items, bad = [], []
    with http_client(30) as c:
        for raw, take_all in lists:
            name = str(raw).lstrip("@").strip()
            if not name or name.lower() in skip_names:
                continue
            try:
                title, posts, pages = None, [], 0
                before = None
                while pages < int(tconf.get("web_max_pages", 5)):
                    t, batch = tg_web_fetch(c, name, before)
                    title = title or t
                    if not batch:
                        break
                    posts += batch
                    pages += 1
                    oldest = min(batch, key=lambda p: p["id"])
                    if oldest["date"] < since or oldest["id"] <= 1:
                        break
                    before = oldest["id"]
                    time.sleep(0.5)
                if not posts:
                    bad.append(name)
                    continue
                for p in posts:
                    if p["date"] < since or len(p["text"]) < 20:
                        continue
                    key = f"tgw:{name.lower()}:{p['id']}"
                    if store.seen(key):
                        continue
                    groups = matcher.match(p["text"]) or (["Прочее"] if take_all else [])
                    if groups:
                        items.append(dict(key=key, source=f"TG: {title or name}", text=p["text"],
                                          url=f"https://t.me/{name}/{p['id']}", date=p["date"], groups=groups))
            except Exception as ex:
                log.warning("Ошибка чтения t.me/s/%s: %s", name, ex)
            time.sleep(0.5)
    if bad:
        log.warning("Каналы без веб-версии или не найдены: %s", ", ".join(bad))
    log.info("Telegram (веб): %d подходящих постов", len(items))
    return items


def tg_proxy():
    p = env("TG_PROXY") or env("PROXY")
    if not p:
        return None
    m = re.match(r"(socks5h?|socks4|http)://(?:([^:@]+):([^@]+)@)?([^:/]+):(\d+)", p)
    if not m:
        log.warning("Прокси не распознан: %s", p)
        return None
    scheme, user, pwd, host, port = m.groups()
    return (scheme.rstrip("h"), host, int(port), True, user, pwd)


def tg_logged_in():
    return bool(env("TG_API_ID") and env("TG_API_HASH") and (BASE / "tg.session").exists())


def tg_client():
    from telethon import TelegramClient
    api_id, api_hash = env("TG_API_ID"), env("TG_API_HASH")
    if not api_id or not api_hash:
        sys.exit("Нет TG_API_ID / TG_API_HASH в .env")
    client = TelegramClient(str(BASE / "tg"), int(api_id), api_hash, proxy=tg_proxy())
    client.flood_sleep_threshold = 180
    return client


async def collect_telegram(client, conf, since, store, matcher):
    from telethon.tl.types import Channel
    tconf = conf.get("telegram") or {}
    limit = int(tconf.get("max_posts_per_channel", 300))
    entities = {}  # id -> (entity, take_all)

    if tconf.get("read_subscribed", True):
        async for d in client.iter_dialogs():
            e = d.entity
            if isinstance(e, Channel) and (e.broadcast or (tconf.get("include_groups") and e.megagroup)):
                entities[e.id] = (e, False)

    for names, take_all in ((tconf.get("channels") or [], False), (tconf.get("channels_all") or [], True)):
        for name in names:
            try:
                e = await client.get_entity(str(name).lstrip("@"))
                prev = entities.get(e.id)
                entities[e.id] = (e, take_all or (prev[1] if prev else False))
            except Exception as ex:
                log.warning("Канал %s не найден: %s", name, ex)

    log.info("Telegram: %d каналов", len(entities))
    items = []
    for e, take_all in entities.values():
        uname = getattr(e, "username", None)
        title = getattr(e, "title", uname or str(e.id))
        try:
            async for m in client.iter_messages(e, limit=limit):
                if m.date < since:
                    break
                text = (m.message or "").strip()
                if len(text) < 20:
                    continue
                key = f"tg:{e.id}:{m.id}"
                if store.seen(key):
                    continue
                groups = matcher.match(text)
                if not groups and take_all:
                    groups = ["Прочее"]
                if not groups:
                    continue
                url = f"https://t.me/{uname}/{m.id}" if uname else f"https://t.me/c/{e.id}/{m.id}"
                items.append(dict(key=key, source=f"TG: {title}", text=text, url=url,
                                  date=m.date, groups=groups))
        except Exception as ex:
            log.warning("Ошибка чтения %s: %s", title, ex)
        await asyncio.sleep(0.7)
    return items


# ---------- сбор: новости (RSS и Google News) ----------

def strip_html(s):
    return html.unescape(re.sub(r"<[^>]+>", " ", s or "")).strip()


def collect_news(conf, since, store, matcher):
    nconf = conf.get("news") or {}
    urls = list(nconf.get("feeds") or [])
    for q in nconf.get("google_news_queries") or []:
        urls.append(f"https://news.google.com/rss/search?q={quote_plus(q)}+when:2d&hl=ru&gl=RU&ceid=RU:ru")
    items = []
    with http_client(30) as c:
        for url in urls:
            try:
                feed = feedparser.parse(c.get(url).content)
            except Exception as ex:
                log.warning("RSS недоступен %s: %s", url, ex)
                continue
            src = feed.feed.get("title", url)[:60]
            for en in feed.entries:
                tp = en.get("published_parsed") or en.get("updated_parsed")
                if not tp:
                    continue
                dt = datetime(*tp[:6], tzinfo=timezone.utc)
                if dt < since:
                    continue
                link = en.get("link", "")
                key = "rss:" + link
                if not link or store.seen(key):
                    continue
                title = strip_html(en.get("title"))
                summary = strip_html(en.get("summary"))
                text = f"{title}. {summary}" if summary and summary not in title else title
                groups = matcher.match(text)
                if not groups:
                    continue
                source = src
                if "news.google.com" in url and en.get("source"):
                    source = en.source.get("title", src)
                items.append(dict(key=key, source=source, text=text, url=link, date=dt, groups=groups))
    log.info("Новости: %d подходящих", len(items))
    return items


# ---------- сводка ----------

def dedup(items):
    out, hashes = [], set()
    for it in sorted(items, key=lambda x: x["date"]):
        h = text_hash(it["text"])
        if h in hashes:
            continue
        hashes.add(h)
        it["hash"] = h
        out.append(it)
    return out


def call_llm(system, user):
    provider = env("LLM_PROVIDER", "none").lower()
    if provider == "none":
        return None
    with http_client(240, "LLM_PROXY") as c:
        if provider == "anthropic":
            r = c.post("https://api.anthropic.com/v1/messages",
                       headers={"x-api-key": env("ANTHROPIC_API_KEY"), "anthropic-version": "2023-06-01"},
                       json={"model": env("LLM_MODEL") or "claude-sonnet-5-5", "max_tokens": 6000,
                             "system": system, "messages": [{"role": "user", "content": user}]})
            r.raise_for_status()
            return "".join(b.get("text", "") for b in r.json()["content"])
        if provider == "yandex":
            folder = env("YANDEX_FOLDER_ID")
            base = "https://llm.api.cloud.yandex.net/v1"
            key = env("YANDEX_API_KEY")
            model = env("LLM_MODEL") or f"gpt://{folder}/yandexgpt/latest"
            headers = {"Authorization": f"Bearer {key}", "x-folder-id": folder}
        else:  # openai или любой OpenAI-совместимый API
            base = env("OPENAI_BASE_URL") or "https://api.openai.com/v1"
            model = env("LLM_MODEL") or "gpt-5-mini"
            headers = {"Authorization": f"Bearer {env('OPENAI_API_KEY')}"}
        r = c.post(f"{base.rstrip('/')}/chat/completions", headers=headers,
                   json={"model": model, "messages": [{"role": "system", "content": system},
                                                      {"role": "user", "content": user}]})
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]


def build_digest(conf, items, since, now):
    period = f"{since.astimezone(MSK):%d.%m %H:%M} – {now.astimezone(MSK):%d.%m %H:%M} МСК"
    header = f"<b>📊 Брокерский рынок · {period}</b>\nМатериалов: {len(items)}\n\n"
    if not items:
        return header + "Ничего нового по ключевым словам."

    order = list((conf.get("keywords") or {}).keys())
    items = sorted(items, key=lambda x: (min(order.index(g) if g in order else 99 for g in x["groups"]),
                                         -x["date"].timestamp()))
    max_items = int(conf.get("max_items_for_llm", 120))
    cut = int(conf.get("max_chars_per_item", 600))
    lines = []
    for i, it in enumerate(items[:max_items], 1):
        t = re.sub(r"\s+", " ", it["text"])[:cut]
        lines.append(f"[{i}] {it['date'].astimezone(MSK):%d.%m %H:%M} | {it['source']} | "
                     f"{', '.join(it['groups'])} | {it['url']}\n{t}")
    try:
        result = call_llm(conf.get("prompt", ""), f"Период: {period}\n\n" + "\n\n".join(lines))
        if result:
            result = re.sub(r"^```\w*\n?|```$", "", result.strip()).strip()
            return header + result
    except Exception as ex:
        log.error("Модель недоступна, отправляю список без сводки: %s", ex)

    # запасной вариант: просто список ссылок по разделам
    out, cur = [header.rstrip()], None
    for it in items[:80]:
        g = it["groups"][0]
        if g != cur:
            out.append(f"\n<b>{html.escape(g)}</b>")
            cur = g
        t = html.escape(re.sub(r"\s+", " ", it["text"])[:220])
        out.append(f"• {t} — <a href=\"{html.escape(it['url'])}\">{html.escape(it['source'])}</a>")
    if len(items) > 80:
        out.append(f"\n…и ещё {len(items) - 80}")
    return "\n".join(out)


# ---------- отправка ----------

def split_message(text, limit=3900):
    parts, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) + 1 > limit:
            parts.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        parts.append(cur)
    return [p for p in parts if p.strip()]


def plain(text):
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def send_bot(text):
    token, chat = env("BOT_TOKEN"), env("BOT_CHAT_ID")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    with http_client(30) as c:
        for part in split_message(text):
            r = c.post(url, json={"chat_id": chat, "text": part, "parse_mode": "HTML",
                                  "disable_web_page_preview": True}).json()
            if not r.get("ok"):
                log.warning("HTML не принят (%s), шлю простым текстом", r.get("description"))
                r = c.post(url, json={"chat_id": chat, "text": plain(part),
                                      "disable_web_page_preview": True}).json()
                if not r.get("ok"):
                    raise RuntimeError(f"Бот не отправил сообщение: {r}")
            time.sleep(0.5)


async def send_saved(client, text):
    for part in split_message(text):
        try:
            await client.send_message("me", part, parse_mode="html", link_preview=False)
        except Exception:
            await client.send_message("me", plain(part), link_preview=False)
        await asyncio.sleep(0.5)


# ---------- команды ----------

async def cmd_login():
    client = tg_client()
    await client.start(phone=lambda: input("Телефон Telegram (+7...): "))
    me = await client.get_me()
    print(f"Вход выполнен: {me.first_name} (@{me.username})")
    await client.disconnect()


def cmd_setup_bot():
    token = env("BOT_TOKEN")
    if not token:
        sys.exit("Нет BOT_TOKEN в .env")
    print("Откройте своего бота в Telegram и отправьте ему /start (жду до 3 минут)…")
    api = f"https://api.telegram.org/bot{token}"
    with http_client(40) as c:
        for _ in range(6):
            upd = c.get(f"{api}/getUpdates", params={"timeout": 30}).json()
            for u in reversed(upd.get("result", [])):
                msg = u.get("message") or u.get("channel_post")
                if msg:
                    chat_id = str(msg["chat"]["id"])
                    set_env_value("BOT_CHAT_ID", chat_id)
                    c.post(f"{api}/sendMessage", json={"chat_id": chat_id,
                           "text": "✅ Бот подключён. Дайджесты будут приходить сюда."})
                    print(f"Готово, chat_id = {chat_id}")
                    return
    sys.exit("Сообщение не получено. Запустите ещё раз: monitor.py setup-bot")


def since_time(conf, store, hours, now):
    if hours:
        return now - timedelta(hours=hours)
    last = store.get("last_run")
    if last:
        return datetime.fromisoformat(last)
    return now - timedelta(hours=int(conf.get("first_run_hours", 24)))


async def cmd_run(hours=None, dry_run=False):
    conf = load_config()
    store = Store(BASE / "state.db")
    matcher = Matcher(conf)
    now = datetime.now(timezone.utc)
    since = since_time(conf, store, hours, now)

    client = None
    if tg_logged_in():
        client = tg_client()
        await client.connect()
        if not await client.is_user_authorized():
            log.warning("Вход в Telegram устарел — читаю только публичные каналы. Повторите login.")
            await client.disconnect()
            client = None
    try:
        if client:
            items = await collect_telegram(client, conf, since, store, matcher)
        else:
            items = collect_telegram_web(conf, since, store, matcher)
        items += collect_news(conf, since, store, matcher)
        items = [i for i in dedup(items) if not store.seen(i["hash"])]
        log.info("Итого после удаления дублей: %d", len(items))

        if not items and not conf.get("send_empty_digest", False) and not dry_run:
            log.info("Ничего нового — дайджест не отправляю")
            store.put("last_run", now.isoformat())
            return
        digest = build_digest(conf, items, since, now)
        if dry_run:
            print(plain(digest))
            return
        if env("BOT_TOKEN") and env("BOT_CHAT_ID"):
            send_bot(digest)
        elif client:
            await send_saved(client, digest)
        else:
            sys.exit("Некуда отправить дайджест: нет BOT_TOKEN/BOT_CHAT_ID")
        store.mark([i["key"] for i in items] + [i["hash"] for i in items])
        store.put("last_run", now.isoformat())
        log.info("Дайджест отправлен")
    finally:
        if client:
            await client.disconnect()


def cmd_channels():
    conf = load_config()
    tconf = conf.get("telegram") or {}
    names = [str(n).lstrip("@") for n in (tconf.get("channels") or []) + (tconf.get("channels_all") or [])]
    ok = bad = 0
    with http_client(30) as c:
        for n in names:
            try:
                title, posts = tg_web_fetch(c, n)
            except Exception as ex:
                title, posts = None, []
                log.debug("%s: %s", n, ex)
            if posts:
                ok += 1
                last = max(p["date"] for p in posts).astimezone(MSK)
                print(f"  OK   @{n:<28} {title}  (последний пост {last:%d.%m.%Y})")
            else:
                bad += 1
                print(f"  НЕТ  @{n:<28} не найден или без публичной веб-версии")
            time.sleep(0.4)
    print(f"Работают: {ok}, не работают: {bad}")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    load_env()
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login")
    sub.add_parser("setup-bot")
    sub.add_parser("channels")
    r = sub.add_parser("run")
    r.add_argument("--hours", type=int)
    r.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if a.cmd == "login":
        asyncio.run(cmd_login())
    elif a.cmd == "setup-bot":
        cmd_setup_bot()
    elif a.cmd == "channels":
        cmd_channels()
    else:
        asyncio.run(cmd_run(a.hours, a.dry_run))


if __name__ == "__main__":
    main()
