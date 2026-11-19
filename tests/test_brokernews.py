"""Offline tests of the broker market digest (brokernews)."""
from __future__ import annotations

import os
import sqlite3
import tempfile
import tomllib
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
        cfg = {"telegram": {"channels": ["@a", "https://t.me/b", "A", "x|@y"], "channels_all": ["c"]}}
        self.assertEqual(m.channel_names(cfg), [("c", True), ("a", False), ("b", False), ("x|y", False)])

    def test_alias_resolution_is_remembered(self):
        calls = []

        class Net:
            def get(self, url, timeout=30):
                calls.append(url)
                return (200, TG_HTML.encode()) if url.endswith("/s/good") else (404, b"")

        with tempfile.TemporaryDirectory() as d, unittest.mock.patch.object(m.time, "sleep", lambda s: None):
            store = m.Store(Path(d) / "s.db")
            self.assertEqual(m.tg_resolve(Net(), store, "bad|good")[0], "good")
            calls.clear()
            self.assertEqual(m.tg_resolve(Net(), store, "bad|good")[0], "good")
            self.assertEqual(calls, ["https://t.me/s/good"])          # the working name is tried first
            store.close()


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


class Render(unittest.TestCase):
    items = [dict(text=f"t{i}", url=f"https://t.me/c/{i}", source=f"TG: Канал {i}", groups=["Сбер"], date=NOW)
             for i in range(1, 6)]

    def test_no_repeats_headers_and_real_links(self):
        data = {"main": [{"text": "Сбер выпустил дисконтные облигации", "src": [1, 2]}],
                "sections": {"sber": [{"text": "То же событие ещё раз", "src": [2]},
                                      {"text": "Посев: <b>СберИнвестиции</b>", "src": [3, 99]}],
                             "market": [{"text": "ЦБ о брокерах", "src": [4]}],
                             "unknown": [{"text": "чужой раздел", "src": [5]}]}}
        out = m.render(m.load_config(), data, self.items)
        self.assertEqual(out.count("Сбер выпустил"), 1)
        self.assertNotIn("То же событие", out)                    # all its sources were already used
        self.assertIn("<b>🔥 ГЛАВНОЕ</b>", out)
        self.assertIn("<b>🟢 СБЕР</b>", out)
        self.assertIn(m.RULE, out)
        self.assertIn("&lt;b&gt;СберИнвестиции", out)              # model text is escaped
        self.assertIn('<a href="https://t.me/c/1">Канал 1</a>', out)
        self.assertNotIn("99", out)
        self.assertNotIn("чужой раздел", out)
        self.assertLess(out.index("СБЕР"), out.index("РЫНОК"))

    def test_bond_keywords_across_lines(self):
        mt = m.Matcher(m.load_config())
        self.assertEqual(mt.match("Сбер разместил дисконтные\nоблигации"), ["Сбер"])
        self.assertEqual(mt.match("СФО СБ Секьюритизация 7: рейтинг ruAAA.sf"), ["Сбер"])


class Cases(unittest.TestCase):
    """Пропуски, которые уже случались (tests/brokernews_cases.toml), не должны повториться."""

    def test_known_cases(self):
        with open(Path(__file__).with_name("brokernews_cases.toml"), "rb") as f:
            cases = tomllib.load(f)
        mt = m.Matcher(m.load_config())
        for c in cases.get("case", []):
            with self.subTest(c["note"]):
                self.assertIn(c["section"], mt.match(c["text"]))
        for c in cases.get("must_not", []):
            with self.subTest(c["note"]):
                self.assertEqual(mt.match(c["text"]), [])


class Marks(unittest.TestCase):
    items = Render.items

    def test_kind_level_why(self):
        data = {"main": [{"text": "ЦБ разрешил X", "src": [1], "kind": "official", "why": "Новый продукт"}],
                "sections": {"sber": [{"text": "Посев: дисконтные облигации", "src": [2], "kind": "ad"}],
                             "reputation": [{"text": "Сбой приложения", "src": [3], "level": "mass"}],
                             "telegram": [{"text": "Автор ждёт снижения", "src": [4], "kind": "opinion",
                                           "why": "не для этого раздела"}]}}
        out = m.render(m.load_config(), data, self.items)
        self.assertIn("• 🏛 ЦБ разрешил X", out)
        self.assertIn("↳ <i>Для Сбера (оценка): Новый продукт</i>", out)
        self.assertIn("📣 Посев: дисконтные облигации", out)
        self.assertNotIn("Посев: Посев", out)
        self.assertIn("<b>[массовая проблема]</b> Сбой", out)
        self.assertIn("💬 Автор", out)
        self.assertNotIn("не для этого раздела", out)
        self.assertIn("🏛 официально", out)                      # legend
        self.assertLess(out.index("РЕПУТАЦИЯ"), out.index("ЧТО ОБСУЖДАЮТ"))


class HealthTest(unittest.TestCase):
    def test_alerts_once_and_recovery(self):
        db = sqlite3.connect(":memory:")
        cfg = {"health": {"fail_runs": 2, "silence_days": 7}}
        old = NOW - timedelta(days=10)

        def run(ok_a, ok_b, last_c=NOW):
            h = m.Health(db, cfg)
            h.record("tg:a", "@a", ok_a, NOW if ok_a else None, "нет страницы")
            h.record("rss:b", "b.ru", ok_b, error="HTTP 500")
            h.record("tg:c", "@c", True, last_c)
            return h.problems()

        self.assertEqual(run(False, True), ([], []))             # one failure is not a problem yet
        new, fixed = run(False, False, old)
        self.assertEqual(len(new), 2)                             # @a twice in a row + @c silent
        self.assertTrue(any("@a: не загружается 2" in x for x in new))
        self.assertTrue(any("@c: молчит 10" in x for x in new))
        new, _ = run(False, False, old)
        self.assertFalse(any("@a" in x or "@c" in x for x in new))  # already reported
        self.assertTrue(any("b.ru" in x for x in new))
        new, fixed = run(True, True)
        self.assertEqual(new, [])
        self.assertEqual(sorted(fixed), ["• @a", "• @c", "• b.ru"])

    def test_mass_telegram_failure(self):
        h = m.Health(sqlite3.connect(":memory:"), {})
        for i in range(5):
            h.record(f"tg:{i}", f"@{i}", i == 0)
        new, _ = h.problems()
        self.assertIn("не читаются 4 из 5", new[0])
        h.run.clear()
        for i in range(5):
            h.record(f"tg:{i}", f"@{i}", i == 0)
        self.assertFalse(any("не читаются" in x for x in h.problems()[0]))   # not repeated

    def test_report_text(self):
        self.assertIsNone(m.health_report([], [], []))
        r = m.health_report(["• @a: молчит 8 дн."], [], ["HTTP 429"])
        self.assertIn("служебное", r)
        self.assertIn("HTTP 429", r)


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
                digests = [p for p in net.sent if "Брокерский рынок" in p["text"]]
                alerts = [p for p in net.sent if "служебное" in p["text"]]
                self.assertEqual(len(digests), 1)
                self.assertIn("СберИнвестиции", digests[0]["text"])
                self.assertIn("https://t.me/banksta/101", digests[0]["text"])
                self.assertEqual(len(alerts), 1)  # the other channels don't answer in this test
                self.assertIn("не читаются", alerts[0]["text"])
                self.assertTrue((Path(d) / "web" / "data.json").exists())
                m.cmd_run(24, False, None)       # nothing new: no digest, only "2 runs in a row" per channel
                self.assertEqual(len([p for p in net.sent if "Брокерский рынок" in p["text"]]), 1)
                self.assertEqual(len(net.sent), 3)
                self.assertIn("@frank_media: не загружается 2", net.sent[2]["text"])
                self.assertNotIn("не читаются", net.sent[2]["text"])

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
