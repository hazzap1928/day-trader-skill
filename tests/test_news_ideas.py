"""The "news" strategy (trader-broker/news_ideas.py): ideas from a Claude run, checked by the pack's own rules,
SHADOW unless news.buy_enabled. A fake Claude answer, fake daily bars, a fake Trading 212 and phone:
no network, no real Claude run, no real order."""

import calendar
import datetime
import io
import json
import os
import subprocess
import unittest
from contextlib import redirect_stdout

from _load import load
import test_t212_place
import test_drift_buy
import test_autopilot
from test_drift_buy import DriftBase, bars, held, RATE
from test_t212_place import FakeResp

t212 = test_t212_place.t212
drift = test_drift_buy.drift
alloc = test_drift_buy.alloc
H = load("trader-broker", "holdings")
HT = load("trader-broker", "holdings_t212")
HT.t212, HT.holdings = t212, H
news = load("trader-broker", "news_ideas")
news.t212, news.drift, news.allocation, news.holdings, news.holdings_t212 = t212, drift, alloc, H, HT
M = t212.messages()

D = datetime.date
DAY = D(2026, 11, 5)                                     # Thursday
UTC_0700 = float(calendar.timegm((2026, 11, 5, 12, 0, 0)))          # 07:00 New York: the morning run
UTC_0840 = float(calendar.timegm((2026, 11, 5, 13, 40, 0)))          # 08:40 New York: the update before the open
UTC_0935 = float(calendar.timegm((2026, 11, 5, 14, 35, 0)))
NY_0935 = datetime.datetime(2026, 11, 5, 9, 35)
TICKER = "TSLA_US_EQ"
FULL = dict(test_t212_place.SUMMARY, cash={"availableToTrade": 5000.0, "reservedForOrders": 0, "inPies": 0})
SRC = [{"title": "Tesla sets the robotaxi launch date", "url": "https://example.com/tsla", "date": "2026-11-03"}]


def idea(**over):
    return dict({"symbol": "TSLA", "company": "Tesla", "summary": "Robotaxi launch in two weeks.",
                 "why": "The robotaxi launch could lift sales expectations.", "event": "Robotaxi launch event",
                 "event_date": "2026-11-19", "wrong_if": "The launch is delayed or cancelled.",
                 "sell_rule": "Sell a week after the launch.", "sell_date": "2026-11-27", "sources": list(SRC),
                 "confidence": "high", "confidence_why": "The date is confirmed by the company.",
                 "moved_pct": 1.0, "moved_note": "Up about 1% since the date was announced."}, **over)


def keep(*symbols, **extra):
    """An update answer that keeps each of these morning ideas."""
    return json.dumps(dict({"checks": [{"symbol": x, "keep": True, "why": "nothing new against it"} for x in symbols],
                            "ideas": [], "reviews": [], "notes": ""}, **extra))


def answer(*ideas, **extra):
    return json.dumps(dict({"ideas": list(ideas), "reviews": [], "notes": "one note"}, **extra))


class NewsBase(DriftBase):
    def setUp(self):
        super().setUp()
        self.clock[0] = UTC_0700
        self.cfg = dict(self.cfg, max_total_risk_pct=4, news={"buy_enabled": False})
        self.write("config.json", self.cfg)
        self.write("fx.json", {"usd_per_gbp": RATE})
        self._news_now = news._now
        news._now = lambda: self.clock[0]
        H._now = lambda: self.clock[0]
        self.asked, self.pushed_news, self.said_news = [], [], []
        self.write("universe/tradable.json", {"tickers": [{"symbol": "TSLA", "name": "Tesla, Inc."},
                                                          {"symbol": "AAPL", "name": "Apple Inc."}]})

    def tearDown(self):
        news._now = self._news_now
        M.use(None)
        super().tearDown()

    def reads(self, positions=(), pending=(), summary=None):
        r = self.router
        r.add("GET", "/equity/positions", FakeResp(list(positions)))
        r.add("GET", "/equity/orders", FakeResp(list(pending)))
        r.add("GET", "/equity/account/summary", FakeResp(summary or FULL))

    def claude(self, text):
        def fake(prompt, s):
            self.asked.append(prompt)
            if isinstance(text, Exception):
                raise text
            return text
        return fake

    def run_news(self, text, cfg=None, client="demo", today=DAY, force=False, bars_fn=None, kind="main"):
        return news.run(self.dir, cfg or self.cfg, client=self.client if client == "demo" else client, today=today,
                        now_utc=datetime.datetime.utcfromtimestamp(self.clock[0]), claude=self.claude(text),
                        bars_fn=bars_fn or (lambda sym, a, b: bars()),
                        push=lambda t, once_key=None: self.pushed_news.append((t, once_key)),
                        say=self.said_news.append, force=force, rate=RATE, kind=kind)

    def briefing_gone(self, day="2026-11-05"):
        """The 07:30 briefing has already started, so a main run's ideas get a message of their own."""
        self.write("state/briefing.json", {"started": day})

    def writes(self):
        return [c for c in self.router.calls if c[0] != "GET"]


class Settings(unittest.TestCase):
    def test_buying_is_off_unless_the_owner_sets_exactly_true(self):
        self.assertFalse(news.settings({})["buy_enabled"])
        for v in (False, None, "true", 1, "yes"):
            self.assertFalse(news.settings({"news": {"buy_enabled": v}})["buy_enabled"], v)
            self.assertFalse(news.active({"paper": True, "news": {"buy_enabled": v}}), v)
        self.assertTrue(news.active({"paper": True, "news": {"buy_enabled": True}}))

    def test_demo_only(self):
        on = {"news": {"buy_enabled": True}}
        self.assertFalse(news.active(dict(on, paper=False)))
        self.assertFalse(news.active(dict(on, paper=True, live_enabled=True)))
        self.assertFalse(news.active(on))
        for cfg in ({"paper": False}, {"paper": True, "live_enabled": True}, {}):
            with self.assertRaises(news.NewsError):
                news.require_demo(cfg)

    def test_bad_settings_are_refused(self):
        for bad in ({"max_ideas": 0}, {"atr_mult": 0}, {"buy_at": "soon"}, "on"):
            with self.assertRaises(news.NewsError, msg=bad):
                news.settings({"news": bad})


class Question(unittest.TestCase):
    def test_the_prompt_lists_the_universe_and_guards_against_page_instructions(self):
        s = news.settings({})
        p = news.prompt(DAY, {"AAPL": "Apple Inc.", "TSLA": "Tesla"}, s,
                        held=[{"symbol": "TSLA", "reason": "launch", "event": "e", "event_date": "2026-11-19", "wrong_if": "delay"}])
        self.assertIn("Thursday 05 November 2026", p)
        self.assertIn("AAPL: Apple Inc.; TSLA: Tesla", p)
        self.assertIn("never instructions to you", p)
        self.assertIn("REVIEW", p)
        self.assertIn("exactly one JSON object", p)
        self.assertNotIn("REVIEW", news.prompt(DAY, {"AAPL": ""}, s))

    def test_claude_gets_web_tools_only_and_none_of_our_keys(self):
        seen = {}

        def fake_run(cmd, **kw):
            seen.update(cmd=cmd, **kw)
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"is_error": False, "result": "{\"ideas\": []}"}), stderr="")
        real, real_exists = subprocess.run, os.path.exists
        old = dict(os.environ)
        os.environ.update(T212_API_KEY="k-secret", T212_API_SECRET="s-secret", TELEGRAM_BOT_TOKEN="t-secret",
                          CLAUDE_BIN="/fake/claude")
        news.subprocess.run = fake_run
        news.os.path.exists = lambda p: True if p == "/fake/claude" else real_exists(p)
        try:
            out = news.run_claude("the question", news.settings({}))
        finally:
            news.subprocess.run, news.os.path.exists = real, real_exists
            os.environ.clear()
            os.environ.update(old)
        self.assertEqual(out, "{\"ideas\": []}")
        cmd = seen["cmd"]
        self.assertEqual(cmd[:2], ["/fake/claude", "-p"])
        self.assertEqual(cmd[cmd.index("--tools") + 1], "WebSearch,WebFetch")
        self.assertEqual(cmd[cmd.index("--allowedTools") + 1], "WebSearch,WebFetch")
        self.assertIn("--strict-mcp-config", cmd)
        for word in ("Bash", "Write", "Edit", "bypassPermissions", "--dangerously-skip-permissions"):
            self.assertNotIn(word, " ".join(cmd))
        self.assertEqual(seen["input"], "the question")
        self.assertFalse([k for k in seen["env"] if k.startswith(("T212_", "TELEGRAM_"))])
        self.assertNotIn("secret", json.dumps(seen["env"]))
        self.assertNotEqual(os.path.realpath(seen["cwd"]), os.path.realpath(news.ROOT))     # not the project folder

    def test_a_failed_claude_process_is_an_error_never_an_answer(self):
        real, real_exists = subprocess.run, os.path.exists
        news.os.path.exists = lambda p: True
        try:
            for result in (subprocess.CompletedProcess([], 1, stdout="", stderr="not logged in"),
                           subprocess.CompletedProcess([], 0, stdout="not json", stderr=""),
                           subprocess.CompletedProcess([], 0, stdout=json.dumps({"is_error": True, "result": "limit"}), stderr="")):
                news.subprocess.run = lambda cmd, **kw: result
                with self.assertRaises(news.NewsError):
                    news.run_claude("q", news.settings({}))

            def slow(cmd, **kw):
                raise subprocess.TimeoutExpired(cmd, 1)
            news.subprocess.run = slow
            with self.assertRaises(news.NewsError):
                news.run_claude("q", news.settings({}))
        finally:
            news.subprocess.run, news.os.path.exists = real, real_exists

    def test_parse(self):
        self.assertEqual(news.parse(answer(idea()))["ideas"][0]["symbol"], "TSLA")
        self.assertEqual(len(news.parse("Here you go:\n```json\n%s\n```\nDone." % answer(idea()))["ideas"]), 1)
        self.assertEqual(news.parse("Sure. " + answer())["ideas"], [])
        for bad in ("", "no ideas today", "{\"ideas\": 3}", "{broken", "[1, 2]", "{\"ideas\": [], \"reviews\": 1}"):
            with self.assertRaises(news.NewsError, msg=bad):
                news.parse(bad)


class Words(unittest.TestCase):
    S = news.settings({})
    ALLOWED, BLOCKED = {"TSLA", "AAPL"}, {"AAPL"}

    def why(self, **over):
        return news.validate(idea(**over), DAY, self.S, self.ALLOWED, self.BLOCKED)[1]

    def test_a_complete_idea_passes_with_tidy_fields(self):
        got, why = news.validate(idea(why="  The   launch\ncould lift sales.  "), DAY, self.S, self.ALLOWED, self.BLOCKED)
        self.assertIsNone(why)
        self.assertEqual(got["why"], "The launch could lift sales.")
        self.assertEqual((got["event_date"], got["sell_date"]), ("2026-11-19", "2026-11-27"))
        self.assertEqual(got["sources"], SRC)

    def test_every_field_is_needed(self):
        self.assertIn("why the price should rise", self.why(why=""))
        self.assertIn("the event", self.why(event=None))
        self.assertIn("what would prove it wrong", self.why(wrong_if="?"))
        self.assertIn("the sell rule", self.why(sell_rule=""))
        self.assertIn("the event's date", self.why(event_date="next month"))
        self.assertIn("a sell date", self.why(sell_date=None))
        self.assertIn("no dated source", self.why(sources=[]))
        self.assertIn("no dated source", self.why(sources=[{"title": "t", "url": "https://x.example"}]))
        self.assertIn("no dated source", self.why(sources=[{"title": "t", "url": "javascript:alert(1)", "date": "2026-11-01"}]))
        self.assertIn("no dated source", self.why(sources=[{"title": "t", "url": "https://x.example", "date": "2026-12-25"}]))

    def test_it_must_look_forward(self):
        self.assertIn("in the past", self.why(event_date="2026-11-04"))
        self.assertIsNone(self.why(event_date="2026-11-05", sell_date="2026-11-12"))
        self.assertIn("after today", self.why(sell_date="2026-11-05"))
        self.assertIn("over 120 days", self.why(sell_date="2027-06-01"))

    def test_the_universe_and_the_blocked_list(self):
        self.assertIn("not in the tradable universe", self.why(symbol="NVDA"))
        self.assertIn("blocked list", self.why(symbol="AAPL"))
        self.assertIn("no usable ticker", self.why(symbol="buy this now!!"))
        self.assertEqual(news.validate("TSLA", DAY, self.S, self.ALLOWED, set())[1], "not an object")

    def test_a_weekend_or_holiday_sell_date_moves_to_the_trading_day_before(self):
        got, _ = news.validate(idea(sell_date="2026-11-28"), DAY, self.S, self.ALLOWED, self.BLOCKED)     # a Saturday
        self.assertEqual(got["sell_date"], "2026-11-27")
        got, _ = news.validate(idea(sell_date="2026-11-26"), DAY, self.S, self.ALLOWED, self.BLOCKED)     # Thanksgiving
        self.assertEqual(got["sell_date"], "2026-11-25")


class ShadowRun(NewsBase):
    def test_an_idea_is_logged_checked_and_told_and_nothing_is_bought(self):
        self.reads()
        self.briefing_gone()
        doc = self.run_news(answer(idea(), idea(symbol="NVDA"), idea(symbol="AAPL", sources=[])))
        self.assertEqual(doc["status"], "ok")
        self.assertEqual([r["symbol"] for r in doc["ideas"]], ["TSLA"])
        self.assertEqual(sorted((r["symbol"], r["why_not"]) for r in doc["rejected"]),
                         [("AAPL", "no dated source"), ("NVDA", "not in the tradable universe")])
        self.assertEqual(self.writes(), [])                             # not one order, cancel or change
        r = doc["ideas"][0]
        self.assertEqual((r["price"], r["stop"], r["id"], r["shadow"]), (366.0, 354.0, "2026-11-05-TSLA", True))
        self.assertGreater(r["qty"], 0)
        self.assertLessEqual(r["max_loss_gbp"], 50.0)                   # the max loss a trade
        self.assertLessEqual(r["cost_gbp"], 5000.0 * 0.22)              # the position cap
        self.assertEqual(r["risk"]["results"][0]["verdict"], "PASS")    # trader-risk saw this exact order
        rows = news.load_ideas(self.dir)
        self.assertEqual(sorted(x["verdict"] for x in rows), ["passed", "rejected", "rejected"])
        for k in ("company", "why", "event", "event_date", "wrong_if", "sell_rule", "sell_date", "sources"):
            self.assertTrue(rows[0].get(k), k)
        self.assertEqual(self.read("state/news/today.json")["day"], "2026-11-05")
        self.assertEqual(len(self.pushed_news), 1)
        text, key = self.pushed_news[0]
        self.assertEqual(key, "news-ideas-2026-11-05")
        self.assertIn("Tesla (TSLA)", text)
        self.assertIn("Practice only: nothing is bought", text)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "state", "news", "buys.jsonl")))

    def test_the_run_happens_once_a_day_unless_forced(self):
        self.reads()
        self.run_news(answer(idea()))
        self.run_news(answer(idea(), idea(symbol="AAPL")))
        self.assertEqual(len(self.asked), 1)
        self.assertEqual(len(news.load_ideas(self.dir)), 1)
        self.run_news(answer(), force=True)
        self.assertEqual(len(self.asked), 2)
        self.assertEqual(news.load_ideas(self.dir), [])                 # a forced re-run replaces today's rows

    def test_a_failed_claude_run_is_logged_told_once_and_leaves_nothing_to_buy(self):
        self.briefing_gone()
        for failure in (news.NewsError("the Claude run ended with code 1: not logged in"), "I could not do that."):
            self.pushed_news[:] = []
            doc = self.run_news(failure, force=True)
            self.assertEqual((doc["status"], doc["ideas"]), ("failed", []))
            self.assertEqual(len(self.pushed_news), 1)
            self.assertEqual(self.pushed_news[0][1], "news-failed-2026-11-05")       # once a day, however often it fails
            self.assertIn("failed", self.pushed_news[0][0])
            self.assertIsNone(news.read_today(self.dir, DAY))
        self.assertEqual(self.router.calls, [])
        with open(os.path.join(self.dir, "state", "news", "runs.jsonl")) as fh:
            runs = [json.loads(x) for x in fh]
        self.assertEqual([r["status"] for r in runs], ["failed", "failed"])
        on = dict(self.cfg, news={"buy_enabled": True})
        res = news.buy_today(self.client, self.dir, on, NY_0935, RATE, lambda s: 366.0, False, self.said.append,
                             self.pushed_db.append)
        self.assertEqual(res["bought"], [])
        self.assertIn("nothing is bought", res["status"])
        self.assertEqual(self.router.calls, [])

    def test_no_run_on_a_weekend_or_holiday_or_when_switched_off(self):
        self.assertIsNone(self.run_news(answer(idea()), today=D(2026, 11, 7)))
        self.assertIsNone(self.run_news(answer(idea()), today=D(2026, 11, 26)))
        self.assertIsNone(self.run_news(answer(idea()), cfg=dict(self.cfg, news={"enabled": False})))
        self.assertEqual((self.asked, self.pushed_news), ([], []))

    def test_live_is_refused(self):
        for cfg in (dict(self.cfg, paper=False), dict(self.cfg, live_enabled=True)):
            with self.assertRaises(news.NewsError):
                self.run_news(answer(idea()), cfg=cfg)
        self.assertEqual(self.asked, [])

    def test_with_the_universe_on_trader_risk_checks_the_universe_not_the_hand_list(self):
        # found by the first real run: ideas came from the universe but trader-risk still used allowed_tickers
        import time
        self.write("universe/tradable.json", {"built_at": time.time() - 3600, "built": "now", "source": "sp500", "count": 2,
                                              "tickers": [{"symbol": "TSLA", "t212": "TSLA_US_EQ", "name": "Tesla, Inc."},
                                                          {"symbol": "MSFT", "t212": "MSFT_US_EQ", "name": "Microsoft"}]})
        cfg = dict(self.cfg, allowed_tickers=["AAPL"], universe={"enabled": True})
        self.assertTrue(load("trader-scanner", "universe").status(cfg, self.dir)["active"])
        self.reads()
        doc = self.run_news(answer(idea(), idea(symbol="AAPL")), cfg=cfg)
        self.assertEqual([r["symbol"] for r in doc["ideas"]], ["TSLA"])
        self.assertEqual(doc["list_from"], "universe")
        self.assertEqual([(r["symbol"], r["why_not"]) for r in doc["rejected"]], [("AAPL", "not in the tradable universe")])

    def test_one_idea_a_company_and_no_more_than_max_ideas(self):
        self.reads()
        cfg = dict(self.cfg, allowed_tickers=["TSLA", "AAPL"], news={"max_ideas": 1})
        doc = self.run_news(answer(idea(), idea(), idea(symbol="AAPL")), cfg=cfg)
        self.assertEqual([r["symbol"] for r in doc["ideas"]], ["TSLA"])
        self.assertEqual(sorted(r["why_not"] for r in doc["rejected"]),
                         ["a second idea for the same company today", "over news.max_ideas (1) for one run"])

    def test_an_idea_over_the_total_risk_cap_is_rejected(self):
        naked = {"instrument": {"ticker": "AAPL_US_EQ"}, "quantity": 5, "currentPrice": 200.0, "averagePricePaid": 200.0}
        self.reads(positions=[naked])                                   # no stop: GBP 800 at risk against a GBP 200 cap
        doc = self.run_news(answer(idea()))
        self.assertEqual(doc["ideas"], [])
        self.assertIn("total risk", doc["rejected"][0]["why_not"])

    def test_todays_earlier_ideas_and_the_pretend_book_count_toward_the_cap(self):
        cfg = dict(self.cfg, max_total_risk_pct=1)                      # 1% of 5,000 = GBP 50; one idea risks about GBP 38
        self.reads()
        doc = self.run_news(answer(idea(), idea(symbol="AAPL")), cfg=cfg)
        self.assertEqual([r["symbol"] for r in doc["ideas"]], ["TSLA"])
        self.assertIn("total risk", doc["rejected"][0]["why_not"])
        # the next morning the idea from the day before is still held in the pretend book
        self.clock[0] += 86400
        doc = self.run_news(answer(idea(symbol="AAPL")), cfg=cfg, today=D(2026, 11, 6))
        self.assertEqual(doc["ideas"], [])
        self.assertIn("total risk", doc["rejected"][0]["why_not"])
        rows = news.load_ideas(self.dir)
        rows[0]["virtual"] = {"status": "closed", "how": "stop", "pl_gbp": -38.0}
        news.save_ideas(self.dir, rows)
        doc = self.run_news(answer(idea(symbol="AAPL")), cfg=cfg, today=D(2026, 11, 6), force=True)
        self.assertEqual([r["symbol"] for r in doc["ideas"]], ["AAPL"])  # sold in the pretend book: its risk is freed

    def test_news_only_uses_what_drift_and_the_rest_leave(self):
        big = {"instrument": {"ticker": "AAPL_US_EQ"}, "quantity": 28, "currentPrice": 200.0, "averagePricePaid": 200.0}
        stop = {"id": 1, "ticker": "AAPL_US_EQ", "type": "STOP", "quantity": -28, "stopPrice": 199.9, "status": "NEW"}
        rec = test_drift_buy.record("AAPL_US_EQ", stop=199.9, entry=200.0)
        self.write("state/broker.json", {"orders": [rec]})
        self.reads(positions=[big], pending=[stop], summary=dict(FULL, cash={"availableToTrade": 520.0}))
        doc = self.run_news(answer(idea()))                             # drift holds GBP 4,487 of the GBP 4,500 investable
        self.assertEqual(doc["ideas"], [])
        self.assertIn("no room", doc["rejected"][0]["why_not"])
        self.assertLess(doc["rejected"][0].get("room_gbp", 0), 20)

    def test_without_the_account_no_idea_passes_but_all_are_logged(self):
        doc = self.run_news(answer(idea()), client=None)
        self.assertEqual(doc["ideas"], [])                              # the cash rules and the cap were not checked
        self.assertIn("could not be read", doc["rejected"][0]["why_not"])
        self.assertEqual(len(news.load_ideas(self.dir)), 1)
        self.assertEqual(self.router.calls, [])
        self.router.add("GET", "/equity/positions", test_t212_place.http_error(500, "down"))
        doc = self.run_news(answer(idea()), force=True)
        self.assertEqual(doc["ideas"], [])

    def test_a_stock_with_no_price_history_is_rejected_not_guessed(self):
        self.reads()

        def no_bars(sym, a, b):
            raise OSError("Yahoo did not answer")
        doc = self.run_news(answer(idea()), bars_fn=no_bars)
        self.assertIn("no price or daily bars", doc["rejected"][0]["why_not"])

    def test_the_phone_message_is_plain_english(self):
        self.reads()
        self.briefing_gone()
        self.run_news(answer(idea(why="Margins & sales <up>")))
        text = self.pushed_news[0][0]
        for jargon in ("SHADOW", "Time Machine", "_US_EQ", "trader-risk", "ATR", "EVIDENCE", " R ", "fingerprint"):
            self.assertNotIn(jargon, text)
        self.assertIn("£", text)
        self.assertIn("not advice", text)
        empty = M.news_ideas({"ideas": [], "rejected": [{}], "buying": False})
        self.assertIn("No idea passed the checks today", empty)
        self.assertIn("&lt;up&gt;", M.news_ideas({"ideas": [dict(idea(summary="", why="Sales <up>"), cost_gbp=1.0)]}))
        self.assertIn("high confidence", text)


class VirtualResults(unittest.TestCase):
    S = news.settings({})
    ROW = {"day": "2026-11-05", "sell_date": "2026-11-10", "stop": 90.0, "qty": 10.0, "rate": 1.25, "symbol": "TSLA"}

    def bars(self, *days):
        return {D(2026, 11, d): (o, h, l, c, 1.0) for d, o, h, l, c in days}

    def pl(self, entry, exit_):
        k = 0.002
        return round(10 * (exit_ * (1 - k) - entry * (1 + k)) / 1.25, 2)

    def test_still_open_is_marked_at_the_last_final_close(self):
        v = news.virtual(self.ROW, self.bars((5, 100, 102, 99, 101), (6, 101, 104, 100, 103)), D(2026, 11, 7), self.S)
        self.assertEqual((v["status"], v["entry"], v["last"], v["days"]), ("open", 100, 103, 2))
        self.assertEqual(v["pl_gbp"], self.pl(100, 103))

    def test_todays_unfinished_bar_is_not_used(self):
        v = news.virtual(self.ROW, self.bars((5, 100, 102, 99, 101), (6, 101, 104, 80, 85)), D(2026, 11, 6), self.S)
        self.assertEqual((v["status"], v["last"]), ("open", 101))

    def test_sold_at_the_stop_and_a_gap_fills_at_the_open(self):
        v = news.virtual(self.ROW, self.bars((5, 100, 102, 99, 101), (6, 101, 101, 89, 95)), D(2026, 11, 9), self.S)
        self.assertEqual((v["status"], v["how"], v["last"]), ("closed", "stop", 90.0))
        self.assertEqual(v["pl_gbp"], self.pl(100, 90))
        v = news.virtual(self.ROW, self.bars((5, 100, 102, 99, 101), (6, 85, 86, 80, 82)), D(2026, 11, 9), self.S)
        self.assertEqual((v["how"], v["last"]), ("stop", 85))

    def test_sold_at_the_close_on_its_sell_date(self):
        b = self.bars((5, 100, 102, 99, 101), (6, 101, 104, 100, 103), (9, 103, 106, 102, 105), (10, 105, 109, 104, 108),
                      (11, 108, 120, 107, 119))
        v = news.virtual(self.ROW, b, D(2026, 11, 12), self.S)
        self.assertEqual((v["status"], v["how"], v["last"], v["days"]), ("closed", "sell date", 108, 4))

    def test_not_bought_when_it_opens_at_or_under_its_stop(self):
        v = news.virtual(self.ROW, self.bars((5, 89, 95, 88, 94)), D(2026, 11, 6), self.S)
        self.assertEqual((v["status"], v["pl_gbp"]), ("not bought", 0.0))

    def test_no_result_before_its_first_day_has_closed(self):
        self.assertIsNone(news.virtual(self.ROW, self.bars((4, 100, 101, 99, 100)), D(2026, 11, 5), self.S))

    def test_a_flat_idea_loses_the_fees(self):
        v = news.virtual(self.ROW, self.bars((5, 100, 100, 100, 100)), D(2026, 11, 6), self.S)
        self.assertLess(v["pl_gbp"], 0)

    def test_the_market_is_shown_for_information_only(self):
        spy = self.bars((5, 500, 0, 0, 501), (6, 501, 0, 0, 510))
        v = news.virtual(self.ROW, self.bars((5, 100, 102, 99, 101), (6, 101, 104, 100, 103)), D(2026, 11, 7), self.S, spy)
        self.assertEqual(v["market_pct"], 2.0)
        self.assertNotIn("status_vs_market", v)


class Resolve(NewsBase):
    def test_results_are_filled_in_and_tallied_in_pounds(self):
        self.reads()
        self.run_news(answer(idea()))
        after = bars()
        after.update({DAY: (366.0, 370.0, 365.0, 369.0, 1.0), D(2026, 11, 6): (369.0, 372.0, 368.0, 371.0, 1.0)})
        n = news.resolve(self.dir, self.cfg, today=D(2026, 11, 9), bars_fn=lambda sym, a, b: after)
        self.assertEqual(n, 1)
        row = news.load_ideas(self.dir)[0]
        self.assertEqual((row["virtual"]["status"], row["virtual"]["last"]), ("open", 371.0))
        t = news.tally(self.dir)
        self.assertEqual((t["ideas"], t["open"]), (1, 1))
        self.assertEqual(t["pl_gbp"], row["virtual"]["pl_gbp"])
        self.assertEqual(news.resolve(self.dir, self.cfg, today=D(2026, 11, 9), bars_fn=lambda sym, a, b: after), 0)


class Buying(NewsBase):
    def setUp(self):
        super().setUp()
        self.on = dict(self.cfg, news={"buy_enabled": True})
        self.write("config.json", self.on)

    def morning(self, *ideas, update=True, **kw):
        """The main review at 07:00 New York, then (unless update is False) the update at 08:40 that keeps it all."""
        self.reads()
        doc = self.run_news(answer(*(ideas or (idea(),))), cfg=self.on, **kw)
        if update is not False:
            self.clock[0] = UTC_0840
            doc = self.run_news(update if isinstance(update, str) else keep(*[r["symbol"] for r in doc["ideas"]]),
                                cfg=self.on, kind="update")
        self.router.routes.clear()
        self.router.calls.clear()
        self.clock[0] = UTC_0935
        return doc

    def buy(self, cfg=None, ny=NY_0935, paused=False, client=None, price=366.0):
        with redirect_stdout(io.StringIO()):
            return news.buy_today(client or self.client, self.dir, cfg or self.on, ny, RATE, lambda s: price, paused,
                                  self.said.append, self.pushed_db.append, bars_fn=lambda sym, a, b: bars())

    def fake_account(self, qty, positions=(), pending=()):
        self.account(positions=positions, pending=pending, summary=FULL, filled=qty,
                     stop_reply=FakeResp({"id": 5002, "status": "NEW", "side": "SELL"}))

    def test_off_means_no_buys_whatever_the_ideas(self):
        doc = self.morning()
        self.assertTrue(doc["ideas"])
        for cfg in (self.cfg, dict(self.cfg, news={}), dict(self.cfg, news={"buy_enabled": "true"})):
            self.assertEqual(self.buy(cfg)["status"], "off")
        self.assertEqual(self.router.calls, [])
        self.assertEqual(self.pushed_db, [])

    def test_on_it_buys_with_a_label_a_real_stop_a_sell_date_and_the_idea(self):
        qty = self.morning()["ideas"][0]["qty"]
        self.fake_account(qty)
        res = self.buy()
        self.assertEqual([b["symbol"] for b in res["bought"]], ["TSLA"], res)
        posts = self.router.posts()
        self.assertEqual([p[1] for p in posts], ["/equity/orders/market", "/equity/orders/stop"])
        self.assertEqual(posts[0][2]["quantity"], qty)
        self.assertEqual((posts[1][2]["quantity"], posts[1][2]["stopPrice"]), (-qty, 354.0))
        recs = [r for r in self.records() if r.get("side") == "buy"]
        self.assertEqual({r["strategy"] for r in recs}, {"news"})
        self.assertEqual({r["sell_date"] for r in recs}, {"2026-11-27"})
        self.assertEqual({r["idea"] for r in recs}, {"2026-11-05-TSLA"})
        self.assertEqual(recs[-1]["status"], "stop_placed")
        self.assertIn("Bought Tesla (TSLA)", self.pushed_db[0])
        self.assertIn("I'll ask you first", self.pushed_db[0])

    def test_the_same_step_twice_never_buys_twice(self):
        qty = self.morning()["ideas"][0]["qty"]
        self.fake_account(qty)
        self.assertEqual(len(self.buy()["bought"]), 1)
        n = len(self.router.posts())
        self.router.routes.clear()
        self.reads(positions=[held(qty)])
        res = self.buy()
        self.assertEqual((res["bought"], res["status"]), ([], "nothing to buy today"))
        self.assertEqual(len(self.router.posts()), n)

    def test_a_stock_already_held_or_waiting_is_never_bought_again(self):
        self.morning()
        self.reads(positions=[held(1)])
        res = self.buy()
        self.assertEqual(res["bought"], [])
        self.assertIn("already held", res["skipped"][0][1])
        self.router.routes.clear()
        waiting = {"id": 9, "ticker": TICKER, "type": "MARKET", "side": "BUY", "quantity": 2, "filledQuantity": 0, "status": "NEW"}
        self.write("state/broker.json", {"orders": [test_drift_buy.record(strategy="intraday", date="2026-11-05", status="placed")]})
        self.reads(pending=[waiting])
        self.assertIn("already held or waiting", self.buy()["skipped"][0][1])
        self.assertEqual(self.router.posts(), [])

    def test_a_buy_the_cash_does_not_cover_is_skipped_not_shrunk(self):
        self.morning()
        poor = dict(FULL, cash={"availableToTrade": 900.0, "reservedForOrders": 0, "inPies": 0})
        self.account(summary=poor)
        res = self.buy()
        self.assertEqual(res["bought"], [])
        self.assertEqual(self.router.posts(), [])
        self.assertTrue(res["skipped"])

    def test_a_buy_over_the_total_risk_cap_is_skipped(self):
        self.morning()
        naked = {"instrument": {"ticker": "AAPL_US_EQ"}, "quantity": 5, "currentPrice": 200.0, "averagePricePaid": 200.0}
        self.reads(positions=[naked])
        res = self.buy()
        self.assertIn("total risk", res["skipped"][0][1])
        self.assertEqual(self.router.posts(), [])

    def test_paused_too_late_or_already_under_its_stop(self):
        self.morning()
        self.assertEqual(self.buy(paused=True)["status"], "paused")
        self.assertEqual(self.buy(ny=datetime.datetime(2026, 11, 5, 15, 31))["status"], "too late")
        self.reads()
        self.assertIn("at or under the stop", self.buy(price=350.0)["skipped"][0][1])
        self.assertEqual(self.router.posts(), [])

    def test_yesterdays_ideas_are_never_bought_today(self):
        self.morning()
        res = self.buy(ny=datetime.datetime(2026, 11, 6, 9, 35))
        self.assertIn("nothing is bought", res["status"])
        self.assertEqual(self.router.calls, [])

    def test_live_refuses_news_buying(self):
        self.morning()
        live = t212.Client("live", "k", "s", state_dir=self.dir, log=lambda m: None)
        live.live_ok = True
        self.assertEqual(self.buy(client=live)["status"], "refused: news buying is demo only")
        self.assertEqual(self.buy(dict(self.on, paper=False))["status"], "off")

    def test_place_itself_refuses_a_news_order_with_buying_off_or_no_sell_date(self):
        doc = self.morning()
        row = doc["ideas"][0]
        got = {k: row[k] for k in ("order", "risk", "stop", "qty")}
        self.write("config.json", self.cfg)                             # buying off in the file place reads
        self.fake_account(row["qty"])
        res, why = news.place_one(self.client, self.dir, self.cfg, row, got, "2026-11-05")
        self.assertIsNone(res)
        self.assertIn("news.buy_enabled is off", why)
        self.write("config.json", self.on)
        res, why = news.place_one(self.client, self.dir, self.on, dict(row, sell_date="2026-11-05"), got, "2026-11-05")
        self.assertIn("needs a sell_date after today", why)
        self.assertEqual(self.router.posts(), [])

    def test_a_stop_that_does_not_go_in_is_an_alarm(self):
        qty = self.morning()["ideas"][0]["qty"]
        self.account(summary=FULL, filled=qty, stop_reply=test_t212_place.http_error(400, "nope"))
        res = self.buy()
        self.assertEqual(len(res["bought"]), 1)
        self.assertFalse(res["bought"][0]["stop_ok"])
        self.assertTrue(any("NEWS TSLA" in n[1] or "NEWS TSLA" in n[0] for n in self.notes) or self.pushed)
        self.assertIn("safety net isn't in yet", self.pushed_db[0])


class BrokenReason(NewsBase):
    def page(self, status="open"):
        doc = H.load(self.dir)
        doc["pages"].append({"id": "p1", "symbol": "TSLA", "strategy": "news", "status": status, "broker": "trading212",
                             "account": "demo", "broker_ref": {"ref": TICKER}, "reason": "robotaxi launch",
                             "event": "launch", "event_date": "2026-11-19", "wrong_if": "the launch is delayed",
                             "sell_rule": {"date": "2026-11-27"}, "qty": 3, "buy_date": "2026-11-03"})
        H.save(self.dir, doc)

    def review(self, **over):
        return dict({"symbol": "TSLA", "broken": True, "why": "Tesla delayed the launch to 2027.",
                     "sources": [{"title": "Launch delayed", "url": "https://example.com/d", "date": "2026-11-04"}]}, **over)

    def status(self):
        return H.load(self.dir)["pages"][0]["status"]

    def test_a_broken_reason_asks_and_never_sells(self):
        self.page()
        self.reads(positions=[held(3)], pending=[{"id": 1, "ticker": TICKER, "type": "STOP", "quantity": -3,
                                                  "stopPrice": 340.0, "status": "NEW"}])
        doc = self.run_news(answer(reviews=[self.review()]))
        self.assertEqual(len(doc["asks"]), 1)
        self.assertEqual(self.status(), "sell_asked")
        self.assertEqual(self.writes(), [])                             # no sale, no cancel
        self.assertIn("REVIEW", self.asked[0])
        self.assertIn("robotaxi launch", self.asked[0])
        ask = news.asks_to_send(self.dir)[0]
        self.assertEqual((ask["symbol"], ask["status"]), ("TSLA", "pending"))
        self.assertEqual(t212.approved_sales(self.dir, "demo"), set())  # asked is not approved

    def test_a_review_needs_a_dated_source_and_a_held_news_page(self):
        self.page()
        for bad in (self.review(sources=[]), self.review(broken="yes"), self.review(broken=False),
                    self.review(symbol="AAPL"), self.review(why="")):
            self.assertEqual(news.make_asks(self.dir, [bad], DAY), [], bad)
        self.assertEqual(self.status(), "open")
        doc = H.load(self.dir)
        doc["pages"][0]["strategy"] = "drift"                           # only news holdings are reviewed this way
        H.save(self.dir, doc)
        self.assertEqual(news.make_asks(self.dir, [self.review()], DAY), [])

    def test_approve_marks_it_for_the_close_and_a_second_tap_does_nothing(self):
        self.page()
        ask = news.make_asks(self.dir, [self.review()], DAY)[0]
        self.assertEqual(news.answer(self.dir, ask["id"], "0" * 16, True)[0], "unknown")
        self.assertEqual(news.answer(self.dir, "deadbeef", ask["nonce"], True)[0], "unknown")
        self.assertEqual(self.status(), "sell_asked")
        self.assertEqual(news.answer(self.dir, ask["id"], ask["nonce"], True)[0], "approved")
        self.assertEqual(self.status(), "selling")
        self.assertEqual(t212.approved_sales(self.dir, "demo"), {TICKER})
        self.assertEqual(news.answer(self.dir, ask["id"], ask["nonce"], True)[0], "already")
        self.assertEqual(news.answer(self.dir, ask["id"], ask["nonce"], False)[0], "already")
        self.assertEqual(self.status(), "selling")
        self.assertEqual(self.router.calls, [])                         # the tap itself sends nothing to Trading 212

    def test_reject_keeps_it_and_a_new_review_can_ask_again(self):
        self.page()
        ask = news.make_asks(self.dir, [self.review()], DAY)[0]
        self.assertEqual(news.answer(self.dir, ask["id"], ask["nonce"], False)[0], "rejected")
        self.assertEqual(self.status(), "open")
        self.assertEqual(t212.approved_sales(self.dir, "demo"), set())
        again = news.make_asks(self.dir, [self.review()], D(2026, 11, 6))
        self.assertEqual(len(again), 1)
        self.assertNotEqual(again[0]["id"], ask["id"])

    def test_no_answer_keeps_it(self):
        self.page()
        news.make_asks(self.dir, [self.review()], DAY)
        self.assertEqual(t212.approved_sales(self.dir, "demo"), set())
        self.assertEqual(self.status(), "sell_asked")


class OnThePhone(test_autopilot.PilotBase):
    """The autopilot sends the request with buttons and acts on your tap."""

    def setUp(self):
        super().setUp()
        ap = test_autopilot.ap
        self._mod = ap.mod
        mods = {("trader-broker", "news_ideas"): news, ("trader-broker", "holdings"): H, ("trader-broker", "holdings_t212"): HT,
                ("trader-broker", "drift"): drift}
        ap.mod = lambda folder, name: mods.get((folder, name)) or self._mod(folder, name)
        doc = H.load(self.dir)
        doc["pages"].append({"id": "p1", "symbol": "TSLA", "strategy": "news", "status": "open", "broker": "trading212",
                             "account": "demo", "broker_ref": {"ref": TICKER}, "reason": "launch"})
        H.save(self.dir, doc)
        self.ask = news.make_asks(self.dir, [{"symbol": "TSLA", "broken": True, "why": "The launch was delayed.",
                                              "sources": [{"title": "Delayed", "url": "https://example.com/d",
                                                           "date": "2026-11-04"}]}], D(2026, 11, 5))[0]

    def tearDown(self):
        test_autopilot.ap.mod = self._mod
        news.t212, news.drift, news.allocation, news.holdings, news.holdings_t212 = t212, drift, alloc, H, HT
        super().tearDown()

    def test_the_request_goes_out_once_with_approve_and_reject(self):
        p = self.pilot()
        p.news_asks()
        p.news_asks()
        self.assertEqual(len(self.phone.sent), 1)
        text, buttons = self.phone.sent[0]
        self.assertIn("Sell", text)
        self.assertIn("TSLA", text)
        self.assertIn("The launch was delayed.", text)
        self.assertEqual([b[0] for b in buttons], ["Approve", "Reject"])
        self.assertEqual(buttons[0][1], "sa:%s:%s" % (self.ask["id"], self.ask["nonce"]))

    def test_an_approve_tap_marks_the_sale_and_sends_no_order(self):
        p = self.pilot()
        p.news_asks()
        tap = {"kind": "tap", "id": "cb", "data": "sa:%s:%s" % (self.ask["id"], self.ask["nonce"]), "message_id": 101}
        p.tap(tap)
        p.tap(tap)
        self.assertEqual(self.phone.answers, [M.SELL_TAP["approved"], M.SELL_TAP["already"]])
        self.assertEqual(H.load(self.dir)["pages"][0]["status"], "selling")
        self.assertEqual(len(self.phone.edits), 1)
        self.assertIn("Approved by you", self.phone.edits[0][1])
        self.assertEqual(self.router.calls, [])                         # the autopilot never sells: the guard's close does

    def test_a_reject_tap_and_a_forged_tap(self):
        p = self.pilot()
        p.tap({"kind": "tap", "id": "cb", "data": "sa:%s:%s" % (self.ask["id"], "f" * 16), "message_id": 101})
        self.assertEqual(self.phone.answers, [M.SELL_TAP["unknown"]])
        self.assertEqual(H.load(self.dir)["pages"][0]["status"], "sell_asked")
        p.tap({"kind": "tap", "id": "cb", "data": "sr:%s:%s" % (self.ask["id"], self.ask["nonce"]), "message_id": 101})
        self.assertEqual(H.load(self.dir)["pages"][0]["status"], "open")
        self.assertEqual(self.router.calls, [])


if __name__ == "__main__":
    unittest.main()
