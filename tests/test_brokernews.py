"""Offline tests of the broker market digest (brokernews)."""
from __future__ import annotations

import contextlib
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

    def test_yandex_search_xml(self):
        xml = """<?xml version="1.0" encoding="utf-8"?><yandexsearch version="1.0"><response><results>
<grouping><group><doc><url>https://www.kommersant.ru/doc/1</url><domain>www.kommersant.ru</domain>
<title><hlword>СберИнвестиции</hlword> запустили автоследование</title><modtime>20261119T101500</modtime>
<passages><passage>Сервис стал доступен <hlword>клиентам</hlword></passage></passages></doc></group></grouping>
</results></response></yandexsearch>"""
        items = m.parse_yandex_xml(xml.encode())
        self.assertEqual(items[0]["title"], "СберИнвестиции запустили автоследование")
        self.assertEqual(items[0]["summary"], "Сервис стал доступен клиентам")
        self.assertEqual(items[0]["date"], datetime(2026, 11, 19, 7, 15, tzinfo=timezone.utc))
        with self.assertRaises(RuntimeError):
            m.parse_yandex_xml(b"<yandexsearch><response><error code='32'>limit</error></response></yandexsearch>")

    def test_broken_feed_is_repaired(self):
        bad = RSS.replace("<title>Google Новости</title>", "<title>Банки.ру & Co\x01</title>")
        title, items = m.parse_feed(bad.encode())
        self.assertEqual(title, "Банки.ру & Co")
        self.assertEqual(len(items), 1)

    def test_channel_names(self):
        cfg = {"telegram": {"channels": ["@a", "https://t.me/b", "A", "x|@y"], "promo_watch": ["c", "a"]}}
        self.assertEqual(m.channel_names(cfg), [("a", "industry"), ("b", "industry"), ("x|y", "industry"),
                                                ("c", "promo")])

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

    def test_alias_skips_abandoned_channel(self):
        old = TG_HTML.replace("2026-11-19T08:00:00", "2025-01-01T08:00:00").replace("2026-11-19T09:00:00", "2025-01-02T09:00:00")
        fresh = TG_HTML.replace("2026-11-19T08:00:00", (NOW - timedelta(hours=1)).isoformat()[:19])

        class Net:
            def get(self, url, timeout=30):
                return 200, (old if url.endswith("/s/dead") else fresh).encode()

        with unittest.mock.patch.object(m.time, "sleep", lambda s: None):
            self.assertEqual(m.tg_resolve(Net(), None, "dead|live")[0], "live")
            self.assertEqual(m.tg_resolve(Net(), None, "dead")[0], "dead")       # a single name is kept as is


class Filtering(unittest.TestCase):
    cfg = m.load_config()

    def test_config_keywords(self):
        mt = m.Matcher(self.cfg)
        self.assertEqual(mt.match("СберИнвестиции запустили сервис"), ["СберИнвестиции"])
        self.assertEqual(mt.match("Сбербанк повысил ставки по сбережениям"), [])
        self.assertIn("Брокерский бизнес", mt.match("Брокер Т-Инвестиции привлёк клиентов"))
        self.assertEqual(mt.match("Минфин о реформе ИИС-3"), ["Регулирование"])
        self.assertEqual(mt.match("Батон подорожал"), [])
        self.assertEqual(mt.match("Криптобиржа и брокер"), [])

    def test_split_message(self):
        parts = m.split_message("a" * 9000 + "\nb\n" + "c" * 100)
        self.assertTrue(all(len(p) <= 3900 for p in parts))
        self.assertEqual("".join(parts).replace("\n", ""), "a" * 9000 + "b" + "c" * 100)


class Render(unittest.TestCase):
    items = [dict(text=f"t{i}", url=f"https://t.me/c/{i}", source=f"TG: Канал {i}", groups=["Сбер"], date=NOW)
             for i in range(1, 6)]

    def test_no_repeats_headers_and_real_links(self):
        data = {"main": [{"text": "Сбер выпустил дисконтные облигации", "src": [1, 2]}],
                "sections": {"sber": [{"text": "То же событие ещё раз", "src": [2]},
                                      {"text": "Посев: <b>СберИнвестиции</b>", "src": [3, 99]}],
                             "regulation": [{"text": "ЦБ о брокерах", "src": [4]}],
                             "unknown": [{"text": "чужой раздел", "src": [5]}]}}
        out = m.render(m.load_config(), data, self.items)
        self.assertEqual(out.count("Сбер выпустил"), 1)
        self.assertNotIn("То же событие", out)                    # all its sources were already used
        self.assertIn("<b>🔥 ГЛАВНОЕ</b>", out)
        self.assertIn("<b>🟢 СБЕРИНВЕСТИЦИИ</b>", out)
        self.assertIn("\nСбер выпустил", out)                     # no stray space without a title
        self.assertIn(m.RULE, out)
        self.assertIn("&lt;b&gt;СберИнвестиции", out)              # model text is escaped
        self.assertIn('<a href="https://t.me/c/1">Канал 1</a>', out)
        self.assertNotIn("99", out)
        self.assertNotIn("чужой раздел", out)
        self.assertIn("РЕГУЛИРОВАНИЕ", m.render(m.load_config(), {"sections": {"regulation": [
            {"title": "ЦБ о брокерах", "text": "x", "src": [4]}]}}, self.items))

    def test_bond_keywords_across_lines(self):
        mt = m.Matcher(m.load_config())
        self.assertEqual(mt.match("Сбер разместил дисконтные\nоблигации"), ["СберИнвестиции"])
        self.assertEqual(mt.match("СФО СБ Секьюритизация 7: рейтинг ruAAA.sf"), ["СберИнвестиции"])


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

    def test_titles_kinds_why_and_limit(self):
        data = {"main": [{"title": "ЦБ разрешил X.", "text": "Подробности", "src": [1], "kind": "official",
                          "why": "Новый продукт"}],
                "sections": {"sber": [{"title": "Дисконтные облигации", "text": "в 4 каналах", "src": [2],
                                       "kind": "ad"}],
                             "brokers": [{"title": "Брокер Y", "text": "мнение", "src": [3], "kind": "opinion",
                                          "why": "не для раздела"}]}}
        out = m.render(m.load_config(), data, self.items)
        self.assertIn("🏛 <b>ЦБ разрешил X</b>\nПодробности\n→ <a", out)
        self.assertIn("<i>Для Сбера: Новый продукт</i>", out)
        self.assertIn("📣 <b>Дисконтные облигации</b>", out)
        self.assertNotIn("не для раздела", out)
        self.assertIn("🏛 официальное сообщение", out)                     # legend
        cfg = m.load_config()
        cfg["digest"]["max_points"] = 2
        self.assertNotIn("Брокер Y", m.render(cfg, data, self.items))     # point limit


class Editor(unittest.TestCase):
    items = [dict(text=f"материал {i}", url=f"https://x/{i}", source="СМИ", groups=["Брокерский бизнес"],
                  date=NOW, kind="СМИ") for i in range(1, 5)]

    def test_three_questions_two_stages_and_empty_issue(self):
        calls = []

        def fake(net, cfg, system, user, model=None):
            calls.append((model, user))
            if "строгий редакторский отбор" in system:
                self.assertIn("три вопроса", system)
                self.assertIn("УЖЕ ОПУБЛИКОВАНО", user)
                return {"keep": [{"n": 2, "why": "новый тариф"}, {"n": 4, "why": "решение ЦБ"}, {"n": 99}],
                        "near": [{"n": 1, "why": "прогноз"}]}
            self.assertNotIn("материал 1", user)                     # only the 2 chosen materials
            self.assertIn("зачем: новый тариф", user)
            return {"main": [{"title": "Главное", "text": "t", "src": [1]}]}

        with unittest.mock.patch.object(m, "chat_json", fake):
            _, body, sel = m.build_digest(None, m.load_config(), self.items, NOW, NOW, ["старое"])
        self.assertEqual(calls[0][0], "gpt-5")                          # strong model selects
        self.assertEqual([it["url"] for it, _ in sel["keep"]], ["https://x/2", "https://x/4"])
        self.assertEqual(sel["near"][0][1], "прогноз")
        self.assertIn('href="https://x/2"', body)
        with unittest.mock.patch.object(m, "chat_json", lambda *a, **k: {"keep": []}):
            self.assertEqual(m.build_digest(None, m.load_config(), self.items, NOW, NOW)[1], "")

    def test_editor_failure_never_publishes(self):
        def boom(*a, **k):
            raise RuntimeError("HTTP 502")
        with unittest.mock.patch.object(m, "chat_json", boom):
            with self.assertRaises(m.EditorUnavailable):
                m.build_digest(None, m.load_config(), self.items, NOW, NOW)
        with unittest.mock.patch.object(m, "chat_json", lambda *a, **k: None):   # no OpenAI key
            with self.assertRaises(m.EditorUnavailable):
                m.build_digest(None, m.load_config(), self.items, NOW, NOW)

    def test_model_fallback(self):
        calls = []

        class Net:
            def post_json(self, url, payload, headers=None, timeout=60):
                calls.append(payload["model"])
                if payload["model"] == "gpt-5":
                    return 404, {"error": "model not found"}
                return 200, {"choices": [{"message": {"content": '{"keep": []}'}}]}

        with unittest.mock.patch.dict(os.environ, {"OPENAI_API_KEY": "k"}):
            self.assertEqual(m.chat_json(Net(), m.load_config(), "s", "u", "gpt-5"), {"keep": []})
        self.assertEqual(calls, ["gpt-5", "gpt-5-mini"])


class HealthTest(unittest.TestCase):
    def test_single_failures_are_silent_mass_failure_once(self):
        db = sqlite3.connect(":memory:")

        def run(n_down, total=6, promo_down=0):
            h = m.Health(db, {})
            for i in range(total):
                h.record(f"tg:{i}", f"@{i}", i >= n_down)
            for i in range(promo_down):
                h.record(f"tgp:{i}", f"@p{i}", False)
            return h.problems()

        self.assertEqual(run(1), ([], []))                      # one source down: log only
        self.assertEqual(run(1, promo_down=5), ([], []))        # promo channels don't count
        new, _ = run(4)
        self.assertIn("не отвечают 4 из 6", new[0])
        self.assertEqual(run(5), ([], []))                      # still down: not repeated
        self.assertEqual(run(0)[1], ["• сбор источников снова работает"])

    def test_report_text(self):
        self.assertIsNone(m.health_report([], []))
        self.assertIn("служебное", m.health_report(["• x"], []))


class FakeNet:
    def __init__(self):
        self.sent = []

    def get(self, url, timeout=30):
        if url.startswith("https://t.me/s/sberinvestments"):
            html = TG_HTML.replace("2026-11-19T08:00:00+00:00", (NOW - timedelta(hours=1)).isoformat())
            return 200, html.encode()
        return 404, b""

    def post_json(self, url, payload, headers=None, timeout=60):
        self.sent.append(payload)
        return 200, {"ok": True}


class Run(unittest.TestCase):
    def env(self, d):
        return [unittest.mock.patch.object(m, "STATE", Path(d)),
                unittest.mock.patch.object(m.time, "sleep", lambda s: None),
                unittest.mock.patch.object(m, "weekend_skip", lambda *a: False),
                unittest.mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "owner",
                                                      "BROKERNEWS_CHAT_IDS": "channel", "OPENAI_API_KEY": "k"})]

    def run_with(self, d, net, editor):
        with contextlib.ExitStack() as st:
            for p in self.env(d):
                st.enter_context(p)
            st.enter_context(unittest.mock.patch.object(m, "Net", lambda: net))
            st.enter_context(unittest.mock.patch.object(m, "chat_json", editor))
            m.cmd_run(None, False, None)

    @staticmethod
    def editor(net, cfg, system, user, model=None):
        if "строгий редакторский отбор" in system:
            return {"keep": [{"n": 1, "why": "продукт"}]}
        return {"sections": {"sber": [{"title": "Снижение комиссии для ИИС", "text": "t", "src": [1]}]}}

    def test_publishes_once_then_nothing_new(self):
        with tempfile.TemporaryDirectory() as d:
            net = FakeNet()
            self.run_with(d, net, self.editor)
            to_channel = [p for p in net.sent if p["chat_id"] == "channel"]
            self.assertEqual(len(to_channel), 1)
            self.assertIn("Снижение комиссии для ИИС", to_channel[0]["text"])
            self.assertIn("https://t.me/sberinvestments/101", to_channel[0]["text"])
            alerts = [p for p in net.sent if p["chat_id"] == "owner"]
            self.assertEqual(len(alerts), 1)                    # most sources down in this test: one alert
            self.run_with(d, net, self.editor)
            self.assertEqual(len([p for p in net.sent if p["chat_id"] == "channel"]), 1)
            self.assertEqual(len(net.sent), 2)                  # nothing new, alert not repeated
            store = m.Store(Path(d) / "brokernews.db")
            self.assertIn("Снижение комиссии для ИИС", m.published_titles(store))
            store.close()

    def test_editor_down_publishes_nothing_and_keeps_items(self):
        def boom(*a, **k):
            raise RuntimeError("HTTP 502")
        with tempfile.TemporaryDirectory() as d:
            net = FakeNet()
            self.run_with(d, net, boom)
            self.assertEqual([p for p in net.sent if p["chat_id"] == "channel"], [])
            owner = " ".join(p["text"] for p in net.sent if p["chat_id"] == "owner")
            self.assertIn("редактор ChatGPT не отвечает", owner)
            self.run_with(d, net, self.editor)                  # items were not marked: published now
            self.assertEqual(len([p for p in net.sent if p["chat_id"] == "channel"]), 1)
            self.assertIn("снова отвечает", " ".join(p["text"] for p in net.sent if p["chat_id"] == "owner"))

    def test_review_goes_to_owner_only(self):
        with tempfile.TemporaryDirectory() as d, contextlib.ExitStack() as st:
            for p in self.env(d):
                st.enter_context(p)
            net = FakeNet()
            st.enter_context(unittest.mock.patch.object(m, "Net", lambda: net))
            st.enter_context(unittest.mock.patch.object(m, "chat_json", self.editor))
            m.cmd_run(24, False, None, review=True)
            self.assertTrue(net.sent and all(p["chat_id"] == "owner" for p in net.sent))
            self.assertIn("Разбор выпуска", net.sent[-1]["text"])
            self.assertIn("продукт", net.sent[-1]["text"])


if __name__ == "__main__":
    unittest.main()
