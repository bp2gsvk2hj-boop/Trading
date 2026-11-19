"""Offline tests of the broker market digest (brokernews)."""
from __future__ import annotations

import os
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

from brokernews import monitor as m

NOW = datetime.now(timezone.utc)

TG_HTML = """<html><head><meta property="og:title" content="Банкста"></head><body>
<div class="tgme_widget_message_wrap"><div class="tgme_widget_message js-widget_message" data-post="banksta/101">
<div class="tgme_widget_message_reply"><div class="tgme_widget_message_metatext js-message_reply_text">ответ</div></div>
<div class="tgme_widget_message_text js-message_text" dir="auto">СберИнвестиции <b>снизили</b> комиссию<br/>для ИИС
<a href="x">ссылка</a></div>
<a class="tgme_widget_message_date"><time datetime="2026-11-19T08:00:00+00:00">11:00</time></a></div></div>
<div class="tgme_widget_message_wrap"><div class="tgme_widget_message js-widget_message" data-post="banksta/102">
<div class="tgme_widget_message_text js-message_text">Погода &amp; пробки</div>
<time datetime="2026-11-19T09:00:00+00:00"></time></div></div>
</body></html>"""

RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>Google Новости</title>
<item><title>Мосбиржа: число частных инвесторов выросло</title><link>https://ex.ru/1</link>
<pubDate>Thu, 19 Nov 2026 07:00:00 GMT</pubDate><description>&lt;b&gt;Рост&lt;/b&gt; на 2%</description>
<source url="https://rbc.ru">РБК</source></item>
</channel></rss>"""

ATOM = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>Лента</title>
<entry><title>Финам запустил тариф</title><link href="https://ex.ru/2"/>
<updated>2026-11-19T06:00:00Z</updated></entry></feed>"""


class Parsing(unittest.TestCase):
    def test_tg_page(self):
        p = m.TgWebPage()
        p.feed(TG_HTML)
        posts = p.result()
        self.assertEqual(p.title, "Банкста")
        self.assertEqual([x["id"] for x in posts], [101, 102])
        self.assertIn("СберИнвестиции снизили комиссию\nдля ИИС", posts[0]["text"])
        self.assertNotIn("ответ", posts[0]["text"])
        self.assertEqual(posts[1]["text"], "Погода & пробки")
        self.assertEqual(posts[0]["date"], datetime(2026, 11, 19, 8, tzinfo=timezone.utc))

    def test_rss_and_atom(self):
        title, items = m.parse_feed(RSS.encode())
        self.assertEqual(title, "Google Новости")
        self.assertEqual(items[0]["source"], "РБК")
        self.assertEqual(items[0]["summary"], "Рост на 2%")
        self.assertEqual(items[0]["date"], datetime(2026, 11, 19, 7, tzinfo=timezone.utc))
        _, items = m.parse_feed(ATOM.encode())
        self.assertEqual(items[0]["link"], "https://ex.ru/2")

    def test_channel_names(self):
        cfg = {"telegram": {"channels": ["@a", "https://t.me/b", "A"], "channels_all": ["c"]}}
        self.assertEqual(m.channel_names(cfg), [("c", True), ("a", False), ("b", False)])


class Filtering(unittest.TestCase):
    cfg = m.load_config()

    def test_config_keywords(self):
        mt = m.Matcher(self.cfg)
        self.assertEqual(mt.match("СберИнвестиции запустили сервис"), ["Сбер"])
        self.assertEqual(mt.match("Сбербанк повысил ставки по сбережениям"), [])
        self.assertIn("Конкуренты", mt.match("Брокер Т-Инвестиции привлёк клиентов"))
        self.assertEqual(mt.match("Минфин о реформе ИИС-3"), ["Рынок и регулирование"])
        self.assertEqual(mt.match("Батон подорожал"), [])
        self.assertEqual(mt.match("Криптобиржа и брокер"), [])

    def test_split_message(self):
        parts = m.split_message("a" * 9000 + "\nb\n" + "c" * 100)
        self.assertTrue(all(len(p) <= 3900 for p in parts))
        self.assertEqual("".join(parts).replace("\n", ""), "a" * 9000 + "b" + "c" * 100)

    def test_fallback_digest_escapes(self):
        items = [dict(text="A < B & Сбер", url="https://x/1?a=1&b=2", source="TG", groups=["Сбер"], date=NOW)]
        out = m.fallback_digest(items)
        self.assertIn("A &lt; B &amp; Сбер", out)
        self.assertIn('href="https://x/1?a=1&amp;b=2"', out)


class FakeNet:
    def __init__(self):
        self.sent = []

    def get(self, url, timeout=30):
        if url.startswith("https://t.me/s/banksta"):
            html = TG_HTML.replace("2026-11-19T08:00:00+00:00", (NOW - timedelta(hours=1)).isoformat())
            return 200, html.encode()
        return 404, b""

    def post_json(self, url, payload, headers=None, timeout=60):
        self.sent.append(payload)
        return 200, {"ok": True}


class Run(unittest.TestCase):
    def test_run_sends_once_and_publishes(self):
        with tempfile.TemporaryDirectory() as d:
            net = FakeNet()
            env = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1", "OPENAI_API_KEY": "",
                   "BROKERNEWS_CHAT_IDS": ""}
            with unittest.mock.patch.object(m, "STATE", Path(d)), \
                    unittest.mock.patch.object(m, "Net", lambda: net), \
                    unittest.mock.patch.object(m.time, "sleep", lambda s: None), \
                    unittest.mock.patch.dict(os.environ, env):
                m.cmd_run(24, False, Path(d) / "web")
                self.assertEqual(len(net.sent), 1)
                self.assertIn("СберИнвестиции", net.sent[0]["text"])
                self.assertIn("https://t.me/banksta/101", net.sent[0]["text"])
                self.assertTrue((Path(d) / "web" / "data.json").exists())
                m.cmd_run(24, False, None)       # the same post is not sent again
                self.assertEqual(len(net.sent), 1)

    def test_recipients(self):
        with unittest.mock.patch.dict(os.environ, {"TELEGRAM_CHAT_ID": "1", "BROKERNEWS_CHAT_IDS": ""}):
            self.assertEqual(m.news_chats(), ["1"])
        with unittest.mock.patch.dict(os.environ, {"TELEGRAM_CHAT_ID": "1",
                                                   "BROKERNEWS_CHAT_IDS": "-1001, @team "}):
            self.assertEqual(m.news_chats(), ["-1001", "@team"])
            net = FakeNet()
            with unittest.mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "t"}), \
                    unittest.mock.patch.object(m.time, "sleep", lambda s: None):
                m.send_telegram(net, "текст")
            self.assertEqual([p["chat_id"] for p in net.sent], ["-1001", "@team"])


if __name__ == "__main__":
    unittest.main()
