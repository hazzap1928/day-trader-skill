"""The news runs' proof of web searches, the buy-confidence setting, a verdict on every holding (keep, watch,
sell), the approval step (Approve sells soon after the next open), and "what the market is talking about".
A fake Claude answer with a fake record of its searches, fake bars, a fake Trading 212 and phone: no
network, no real Claude run, no real order."""

import calendar
import datetime
import io
import json
import os
import subprocess
import unittest
from contextlib import redirect_stdout

from _load import load
import test_eod_close
import test_holdings as TH
import test_news_ideas as TN
import test_t212_place
from test_eod_close import STOP, ny
from test_news_ideas import NewsBase, idea, answer, keep, news, M, t212, H, DAY, RATE, TICKER, searched
from test_drift_buy import bars
from test_t212_place import FakeResp, CREDS, http_error

D = datetime.date
review = load("trader-broker", "review")
review.t212, review.drift, review.holdings, review.holdings_t212 = t212, TN.drift, H, TN.HT
S = news.settings({})
SRC = {"title": "Launch delayed", "url": "https://example.com/d", "date": "2026-11-04"}


def verdict(sym="TSLA", what="keep", **over):
    return dict({"symbol": sym, "verdict": what, "why": "The launch date is confirmed again.", "confidence": "medium",
                 "sources": [dict(SRC)]}, **over)


def stream(*events, result="{\"ideas\": []}"):
    """A fake `claude -p --output-format stream-json` output."""
    lines = [{"type": "system", "subtype": "init"}] + list(events)
    lines.append({"type": "result", "subtype": "success", "is_error": False, "result": result, "num_turns": 3,
                  "duration_ms": 9000, "usage": {"input_tokens": 10, "output_tokens": 5,
                                                 "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0}}})
    return "\n".join(json.dumps(x) for x in lines)


def use(uid, name, **inp):
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": uid, "name": name, "input": inp}]}}


def got(uid, content, error=None, **extra):
    return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": uid, "content": content,
                                                      "is_error": error}]}, "tool_use_result": extra}


class TheRecordOfSearches(unittest.TestCase):
    def test_searches_and_opened_pages_are_read_from_the_tool_calls(self):
        text = stream(use("a", "WebSearch", query="tesla robotaxi date"),
                      got("a", "Links: [{\"title\":\"x\",\"url\":\"https://news.example.com/seen?id=1\"}]"),
                      use("b", "WebFetch", url="https://www.example.com/tsla/", prompt="p"),
                      got("b", "the page", code=200, url="https://www.example.com/tsla/"),
                      use("c", "WebFetch", url="https://example.com/gone"),
                      got("c", "Not found", code=404, url="https://example.com/gone"),
                      use("d", "WebFetch", url="https://example.com/blocked"),
                      got("d", "refused", error=True))
        doc, web = news.read_stream(text)
        self.assertEqual(doc["result"], "{\"ideas\": []}")
        self.assertEqual((web["searches"], web["fetches"], web["queries"]), (1, 3, ["tesla robotaxi date"]))
        self.assertEqual(web["opened"], ["https://www.example.com/tsla/"])
        self.assertEqual(sorted(web["failed"]), ["https://example.com/blocked", "https://example.com/gone"])
        self.assertEqual(web["seen"], ["https://news.example.com/seen?id=1"])
        self.assertTrue(news.searched(web))

    def test_a_plain_json_answer_has_no_record_at_all(self):
        doc, web = news.read_stream(json.dumps({"is_error": False, "result": "x"}))
        self.assertEqual((doc["result"], web), ("x", None))
        self.assertFalse(news.searched(web))
        self.assertFalse(news.searched({"searches": 0, "fetches": 0}))
        self.assertEqual(news.read_stream("not json")[0], None)

    def test_the_same_page_is_recognised_whatever_its_www_slash_or_tracking_tag(self):
        web = {"opened": ["https://www.Example.com/tsla/?utm=x"], "seen": ["http://seen.example.com/a#top"]}
        src = [{"url": "https://example.com/tsla", "title": "t", "date": "2026-11-03"},
               {"url": "https://seen.example.com/a", "title": "t", "date": "2026-11-03"},
               {"url": "https://example.com/never", "title": "t", "date": "2026-11-03"}]
        out = news.check_sources(src, web)
        self.assertEqual([(x["url"], x["opened"]) for x in out], [("https://example.com/tsla", True),
                                                                   ("https://seen.example.com/a", False)])
        self.assertEqual(news.check_sources(src, None), src)             # no record: left unmarked

    def test_the_real_command_asks_for_the_stream_and_counts_from_it(self):
        seen = {}

        def fake_run(cmd, **kw):
            seen.update(cmd=cmd)
            return subprocess.CompletedProcess(cmd, 0, stderr="", stdout=stream(
                use("a", "WebSearch", query="q"), got("a", "Links: []"),
                use("b", "WebFetch", url="https://example.com/tsla"), got("b", "page", code=200)))
        real, real_exists, old = subprocess.run, os.path.exists, os.environ.get("CLAUDE_BIN")
        os.environ["CLAUDE_BIN"] = "/fake/claude"
        subprocess.run = fake_run
        os.path.exists = lambda p: True if p == "/fake/claude" else real_exists(p)
        try:
            out = news.run_claude("question", dict(S))
        finally:
            subprocess.run, os.path.exists = real, real_exists
            os.environ.pop("CLAUDE_BIN") if old is None else os.environ.__setitem__("CLAUDE_BIN", old)
        cmd = seen["cmd"]
        self.assertEqual(cmd[cmd.index("--output-format") + 1], "stream-json")
        self.assertIn("--verbose", cmd)
        self.assertEqual(cmd[cmd.index("--tools") + 1], "WebSearch,WebFetch")
        self.assertEqual(out, "{\"ideas\": []}")
        self.assertEqual((out.web["searches"], out.web["opened"]), (1, ["https://example.com/tsla"]))
        self.assertEqual((out.meta["web_searches"], out.meta["web_fetches"], out.meta["pages_opened"]), (1, 1, 1))


class ReadingTheAnswer(NewsBase):
    def test_json_followed_by_more_text_or_a_second_object_is_still_read(self):
        a = answer(idea())
        for text in (a + "\n\nSources: {see above}", a + "\n" + json.dumps({"note": 1}),
                     "Here it is {not json} then " + a, "```json\n%s\n```\n{x}" % a):
            self.assertEqual(news.parse(text)["ideas"][0]["symbol"], "TSLA", text[-40:])

    def test_the_raw_answer_is_kept(self):
        self.reads()
        self.run_news(answer(idea()) + "\nThanks {}")
        with open(os.path.join(self.dir, "state", "news", "answer-2026-11-05-main.txt")) as fh:
            self.assertIn("Thanks {}", fh.read())


class ARunMustSearch(NewsBase):
    def test_a_run_with_no_record_of_searching_fails_and_its_ideas_are_never_used(self):
        self.reads()
        for web in (None, {"searches": 0, "fetches": 0, "opened": [], "seen": []}):
            doc = self.run_news(answer(idea()), web=web, force=True)
            self.assertEqual((doc["status"], doc["runs"]["main"]["status"], doc["ideas"]), ("failed", "failed", []))
            self.assertIn("no web search", doc["runs"]["main"]["error"])
        self.assertEqual(news.load_ideas(self.dir), [])
        self.assertIn("failed", M.news_briefing(news.briefing_data(self.dir, "2026-11-05"))[1])

    def test_an_update_that_did_not_search_means_nothing_is_bought_that_day(self):
        self.reads()
        self.run_news(answer(idea()))
        self.clock[0] = TN.UTC_0840
        doc = self.run_news(keep("TSLA"), kind="update", web=None)
        self.assertEqual(doc["runs"]["update"]["status"], "failed")
        ok, why = news.buyable(doc, S)
        self.assertEqual(ok, [])
        self.assertIn("did not finish", why)
        self.assertEqual([k for _, k in self.pushed_news], ["news-update-failed-2026-11-05"])

    def test_an_idea_needs_a_source_the_run_really_opened(self):
        self.reads()
        web = {"searches": 2, "fetches": 1, "opened": ["https://example.com/aapl"], "failed": [],
               "seen": ["https://example.com/tsla"]}
        doc = self.run_news(answer(
            idea(),                                                          # its page was only seen in search results
            idea(symbol="AAPL", sources=[{"title": "Opened", "url": "https://www.example.com/aapl/", "date": "2026-11-03"},
                                         {"title": "Invented", "url": "https://example.com/never", "date": "2026-11-03"}])),
            web=web)
        self.assertEqual([r["symbol"] for r in doc["ideas"]], ["AAPL"])
        self.assertEqual([(x["title"], x["opened"]) for x in doc["ideas"][0]["sources"]], [("Opened", True)])
        self.assertIn("none of its 2 sources was opened in this run", doc["rejected"][0]["why_not"])
        run = doc["runs"]["main"]["web"]
        self.assertEqual((run["searches"], run["opened"]), (2, ["https://example.com/aapl"]))
        self.assertIn("2 web searches, 1 page opened", " ".join(self.said_news))
        self.assertIn("searched the web 2 time(s)", news.show(self.dir))

    def test_the_question_says_the_searches_are_checked(self):
        p = news.prompt(DAY, {"AAPL": ""}, S)
        self.assertIn("OPENED that page with web fetch in this run", p)
        self.assertIn("without any web search is thrown away", p)
        self.assertIn("you MUST search", news.update_prompt(DAY, {"AAPL": ""}, S))


class WhichConfidenceIsBought(NewsBase):
    morning, buy, fake_account = TN.Buying.morning, TN.Buying.buy, TN.Buying.fake_account

    def setUp(self):
        super().setUp()
        self.on = dict(self.cfg, news={"buy_enabled": True, "buy_confidence": ["medium", "high"]})
        self.write("config.json", self.on)

    def test_the_setting_takes_one_level_or_a_list(self):
        for raw, levels, words in (("high", ("high",), "high"), (["medium", "high"], ("high", "medium"), "medium and high"),
                                   ("medium, high", ("high", "medium"), "medium and high"), (["HIGH"], ("high",), "high"),
                                   (["low", "medium", "high"], ("high", "medium", "low"), "low and medium and high")):
            s = news.settings({"news": {"buy_confidence": raw}})
            self.assertEqual((s["buy_levels"], s["buy_confidence"]), (levels, words), raw)
        self.assertEqual(S["buy_levels"], ("high",))                     # the code's default: high only
        for bad in ([], ["sure"], 3, "very high", ["high", 2]):
            with self.assertRaises(news.NewsError, msg=bad):
                news.settings({"news": {"buy_confidence": bad}})

    def test_with_medium_and_high_a_medium_idea_is_bought_and_a_low_one_is_not(self):
        doc = self.morning(idea(confidence="medium"), idea(symbol="AAPL", confidence="low"))
        self.assertEqual(doc["buy_levels"], ["high", "medium"])
        qty = [r for r in doc["ideas"] if r["symbol"] == "TSLA"][0]["qty"]
        self.fake_account(qty)
        res = self.buy()
        self.assertEqual([b["symbol"] for b in res["bought"]], ["TSLA"], res)
        self.assertEqual([s for s, _ in res["skipped"]], ["AAPL"])
        self.assertIn("its confidence is low: only medium and high confidence ideas are bought", res["skipped"][0][1])
        with open(os.path.join(self.dir, "state", "news", "buys.jsonl")) as fh:
            buys = [json.loads(x) for x in fh]
        self.assertEqual({b["symbol"]: b.get("confidence") for b in buys if b["status"] == "skipped"}, {"AAPL": "low"})
        self.assertEqual([r["confidence"] for r in news.load_ideas(self.dir)], ["medium", "low"])   # still logged

    def test_the_messages_say_which_levels_are_bought(self):
        self.reads()
        doc = self.run_news(answer(idea(confidence="medium"), idea(symbol="AAPL", confidence="low")), cfg=self.on)
        text = M.news_ideas(doc)
        self.assertIn("medium and high-confidence", text)
        d = news.briefing_data(self.dir, "2026-11-05")
        self.assertEqual(d["buy_confidence"], ["high", "medium"])
        lines = "\n".join(M.news_briefing(d))
        self.assertIn("I'd buy", lines)                                  # the medium one is mine now
        self.assertIn("low confidence, so it is only watched", lines)


class Holdings(NewsBase):
    """A news holding (TSLA) and a drift holding (AAPL) in the holdings record."""

    def setUp(self):
        super().setUp()
        doc = H.load(self.dir)
        base = {"status": "open", "broker": "trading212", "account": "demo", "qty": 3, "buy_date": "2026-11-03",
                "buy_price": 360.0, "sell_rule": {"date": "2026-11-27"}}
        doc["pages"] += [dict(base, id="p1", symbol="TSLA", strategy="news", broker_ref={"ref": TICKER},
                              reason="robotaxi launch", event="launch", event_date="2026-11-19",
                              wrong_if="the launch is delayed"),
                         dict(base, id="p2", symbol="AAPL", strategy="drift", broker_ref={"ref": "AAPL_US_EQ"},
                              reason="Beat its earnings forecast (the post-earnings drift rule)")]
        H.save(self.dir, doc)
        self.reads()

    def status(self, pid="p1"):
        return H.find(H.load(self.dir), pid)["status"]

    def rows(self):
        return news.load_verdicts(self.dir)

    def update(self, text, **kw):
        self.clock[0] = TN.UTC_0840
        return self.run_news(text, kind="update", **kw)


class AVerdictOnEveryHolding(Holdings):
    def test_both_runs_are_asked_about_news_and_drift_holdings(self):
        self.run_news(answer())
        self.update(keep())
        for q in self.asked:
            self.assertIn("HOLDINGS", q)
            self.assertIn("- TSLA (bought on a news idea): reason: robotaxi launch", q)
            self.assertIn("- AAPL (bought by a fixed earnings rule", q)
            self.assertIn("\"verdicts\"", q)
            self.assertIn("keep | watch | sell", q)

    def test_a_verdicts_confidence_uses_the_same_scale_as_an_ideas_edge(self):
        q = news.prompt(news.datetime.date(2026, 11, 5), {"TSLA": "Tesla"}, news.settings({}),
                        held=[{"symbol": "TSLA", "strategy": "news"}])
        self.assertIn("A verdict's \"confidence\" uses the same scale as an idea's edge", q)
        self.assertIn("Most verdicts should be low; medium should be uncommon, high rare.", q)
        web = {"searches": 2, "fetches": 1, "opened": ["https://example.com/e"], "failed": [], "seen": [SRC["url"]]}
        self.run_news(answer(verdicts=[verdict(confidence="medium"),                       # its page was only seen
                                       verdict("AAPL", "watch", why="Guidance was cut on Tuesday.", confidence="high",
                                               sources=[dict(SRC, url="https://example.com/e")])]), web=web)
        got_ = {r["symbol"]: r["confidence"] for r in self.rows()}
        self.assertEqual(got_, {"TSLA": "low", "AAPL": "medium"})      # no opened page: low; one opened page: medium

    def test_each_verdict_is_written_down_with_its_reason_confidence_and_sources(self):
        doc = self.run_news(answer(verdicts=[verdict(), verdict("AAPL", "watch", why="Guidance was cut on Tuesday.",
                                                                confidence="high",
                                                                sources=[dict(SRC), dict(SRC, url="https://example.com/e")])]))
        got_ = {r["symbol"]: r for r in self.rows()}
        self.assertEqual({k: (r["verdict"], r["strategy"], r["confidence"], r["kind"], r["page"]) for k, r in got_.items()},
                         {"TSLA": ("keep", "news", "medium", "main", "p1"), "AAPL": ("watch", "drift", "high", "main", "p2")})
        self.assertEqual(got_["TSLA"]["sources"], [dict(SRC, opened=True)])
        self.assertEqual(sorted(doc["verdicts"]), ["AAPL", "TSLA"])
        self.assertEqual(doc["asks"], [])
        self.assertEqual(self.writes(), [])
        self.assertIn("holding AAPL (drift): watch, confidence high: Guidance was cut on Tuesday.", news.show(self.dir))

    def test_a_holding_the_run_did_not_mention_is_written_down_as_none(self):
        self.run_news(answer(verdicts=[verdict(), verdict("MSFT", "sell"), verdict("AAPL", "buy more")]))
        self.assertEqual({r["symbol"]: r["verdict"] for r in self.rows()}, {"TSLA": "keep", "AAPL": "unclear"})
        doc = self.update(keep(verdicts=[verdict("AAPL", "watch")]))
        self.assertEqual({r["symbol"]: r["verdict"] for r in self.rows() if r["kind"] == "update"},
                         {"TSLA": "none", "AAPL": "watch"})
        self.assertEqual({k: v["verdict"] for k, v in doc["verdicts"].items()}, {"TSLA": "keep", "AAPL": "watch"})

    def test_a_sell_on_a_news_holding_asks_you_and_never_sells(self):
        doc = self.run_news(answer(verdicts=[verdict(what="sell", why="Tesla delayed the launch to 2027.")]))
        self.assertEqual(len(doc["asks"]), 1)
        self.assertEqual(self.status(), "sell_asked")
        self.assertTrue(doc["verdicts"]["TSLA"]["asked"])
        ask = news.asks_to_send(self.dir)[0]
        self.assertEqual((ask["symbol"], ask["why"]), ("TSLA", "Tesla delayed the launch to 2027."))
        self.assertEqual(t212.approved_sales(self.dir, "demo"), set())   # asked is not approved
        self.assertEqual(self.writes(), [])
        text = M.news_sell_ask(ask)
        self.assertIn("soon after the US market next opens", text)
        self.assertIn("ask again tomorrow", text)
        self.assertIn("safety net stays in place until it is sold", text)

    def test_a_sell_on_a_drift_holding_is_only_written_down(self):
        doc = self.run_news(answer(verdicts=[verdict("AAPL", "sell", why="Apple cut its guidance on Tuesday.")]))
        self.assertEqual([r["verdict"] for r in self.rows() if r["symbol"] == "AAPL"], ["sell"])
        self.assertEqual((doc["asks"], news.load_asks(self.dir)["asks"]), ([], []))
        self.assertEqual(self.status("p2"), "open")
        self.assertEqual(t212.approved_sales(self.dir, "demo"), set())
        self.assertEqual(self.writes(), [])
        self.assertIn("Noted only", M.verdict_line(doc["verdicts"]["AAPL"]))

    def test_a_sell_without_a_source_the_run_opened_counts_as_watch(self):
        web = {"searches": 2, "fetches": 0, "opened": [], "failed": [], "seen": ["https://example.com/d"]}
        self.run_news(answer(verdicts=[verdict(what="sell")]), web=web)
        r = self.rows()[0]
        self.assertEqual((r["verdict"], r["said"]), ("watch", "sell"))
        self.assertIn("counts as watch", r["note"])
        self.assertEqual((news.load_asks(self.dir)["asks"], self.status()), ([], "open"))
        self.run_news(answer(verdicts=[verdict(what="sell", why="")]), force=True)
        self.assertEqual(self.rows()[-2]["verdict"], "watch")

    def test_an_older_answers_reviews_still_work(self):
        doc = self.run_news(answer(reviews=[{"symbol": "TSLA", "broken": True, "why": "Tesla delayed the launch to 2027.",
                                             "sources": [dict(SRC)]}, {"symbol": "AAPL", "broken": False, "why": "fine"}]))
        self.assertEqual({r["symbol"]: r["verdict"] for r in self.rows()}, {"TSLA": "sell", "AAPL": "keep"})
        self.assertEqual(len(doc["asks"]), 1)
        with self.assertRaises(news.NewsError):
            news.parse("{\"ideas\": [], \"verdicts\": 3}")


class TheApprovalStep(Holdings):
    SELL = dict(what="sell", why="Tesla delayed the launch to 2027.")

    def test_reject_keeps_it_and_it_is_not_asked_again_the_same_day(self):
        self.run_news(answer(verdicts=[verdict(**self.SELL)]))
        ask = news.asks_to_send(self.dir)[0]
        self.assertEqual(news.answer(self.dir, ask["id"], ask["nonce"], False)[0], "rejected")
        self.assertEqual(self.status(), "open")
        self.update(keep(verdicts=[verdict(**self.SELL)]))
        self.assertEqual(len(news.load_asks(self.dir)["asks"]), 1)       # one question a holding a day
        self.assertEqual(self.status(), "open")

    def test_after_a_reject_it_asks_again_the_next_day(self):
        self.run_news(answer(verdicts=[verdict(**self.SELL)]))
        ask = news.asks_to_send(self.dir)[0]
        news.answer(self.dir, ask["id"], ask["nonce"], False)
        self.clock[0] += 86400
        doc = self.run_news(answer(verdicts=[verdict(**self.SELL)]), today=D(2026, 11, 6))
        self.assertEqual(len(doc["asks"]), 1)
        self.assertEqual([a["status"] for a in news.load_asks(self.dir)["asks"]], ["rejected", "pending"])
        self.assertEqual(self.status(), "sell_asked")

    def test_no_answer_keeps_it_and_the_next_day_asks_again(self):
        self.run_news(answer(verdicts=[verdict(**self.SELL)]))
        old = news.asks_to_send(self.dir)[0]
        news.mark_sent(self.dir, old["id"], 77)
        self.assertEqual((self.status(), t212.approved_sales(self.dir, "demo")), ("sell_asked", set()))
        self.clock[0] += 86400
        self.run_news(answer(verdicts=[verdict(**self.SELL)]), today=D(2026, 11, 6))
        asks = news.load_asks(self.dir)["asks"]
        self.assertEqual([a["status"] for a in asks], ["no_answer", "pending"])
        self.assertEqual(len(news.asks_to_send(self.dir)), 1)            # a fresh question goes to the phone
        self.assertEqual(news.answer(self.dir, old["id"], old["nonce"], True)[0], "expired")   # the old buttons do nothing
        self.assertEqual((self.status(), t212.approved_sales(self.dir, "demo")), ("sell_asked", set()))
        self.assertIn("earlier day", M.SELL_TAP["expired"])

    def test_no_answer_and_the_next_day_says_keep_so_the_question_is_closed(self):
        self.run_news(answer(verdicts=[verdict(**self.SELL)]))
        self.clock[0] += 86400
        self.run_news(answer(verdicts=[verdict()]), today=D(2026, 11, 6))
        self.assertEqual([a["status"] for a in news.load_asks(self.dir)["asks"]], ["no_answer"])
        self.assertEqual(self.status(), "open")
        self.assertEqual(self.writes(), [])

    def test_approve_marks_it_for_the_guard_and_sends_nothing_itself(self):
        self.run_news(answer(verdicts=[verdict(**self.SELL)]))
        ask = news.asks_to_send(self.dir)[0]
        self.router.calls.clear()
        self.assertEqual(news.answer(self.dir, ask["id"], ask["nonce"], True)[0], "approved")
        self.assertEqual((self.status(), t212.approved_sales(self.dir, "demo")), ("selling", {TICKER}))
        self.assertEqual(news.answer(self.dir, ask["id"], ask["nonce"], True)[0], "already")
        self.assertEqual(self.router.calls, [])
        self.assertIn("soon after the US market next opens", M.news_sell_end("x", "approved"))
        self.assertIn("ask again tomorrow", M.news_sell_end("x", "rejected"))


class TheGuardSellsAnApprovedHolding(test_t212_place.Base):
    """Exits only: the guard cancels the stop and sells, soon after the open, what you approved."""

    def setUp(self):
        super().setUp()
        self.write("config.json", dict(self.cfg, eod_close={"enabled": True, "at": "15:50"}))
        rec = test_eod_close.record
        self.write("state/broker.json", {"orders": [rec(strategy="news", sell_date="2026-12-01"),
                                                    rec("AAPL_US_EQ", strategy="drift", sell_date="2026-12-01")]})
        self.client = t212.Client("demo", CREDS["T212_API_KEY"], CREDS["T212_API_SECRET"], state_dir=self.dir,
                                  log=lambda m: None)
        self.clock[0] = ny(9, 36)

    def page(self, status="selling", **over):
        page = dict({"id": "p1", "symbol": "TSLA", "strategy": "news", "status": status, "broker": "trading212",
                     "account": "demo", "broker_ref": {"ref": TICKER}, "sell_rule": {"date": "2026-12-01"}}, **over)
        self.write("state/holdings.json", {"schema": 1, "pages": [page], "reported": {}})

    def account(self, sell=None, stop=None):
        r = self.router
        r.add("GET", "/equity/positions", FakeResp([test_eod_close.held(7), test_eod_close.held(2, "AAPL_US_EQ")]))
        r.add("GET", "/equity/orders", FakeResp([STOP]), FakeResp([]))
        r.add("DELETE", "/equity/orders/7001", FakeResp({}))
        r.add("POST", "/equity/orders/market", sell or FakeResp({"id": 9300, "status": "NEW"}))
        if stop:
            r.add("POST", "/equity/orders/stop", stop)

    def go(self):
        with redirect_stdout(io.StringIO()):
            return t212.run_approved(self.client, self.dir)

    def writes(self):
        return [(c[0], c[1]) for c in self.router.calls if c[0] != "GET"]

    def test_it_sells_only_the_approved_one_once_and_tells_you(self):
        self.page()
        self.account()
        res = self.go()
        self.assertEqual(self.writes(), [("DELETE", "/equity/orders/7001"), ("POST", "/equity/orders/market")])
        self.assertEqual(self.router.posts()[0][2], {"ticker": TICKER, "quantity": -7, "extendedHours": False})
        self.assertIn("a sale you approved", res["closed"][0])
        st = self.read("state/t212-guard.json")
        self.assertEqual((st["approved_sale"]["sold"], st["approved_sale"]["tried"]), ([TICKER], [TICKER]))
        self.assertFalse(t212.eod_done_today(self.dir))                  # the 15:50 close still runs as usual
        self.assertEqual(len(self.pushed), 1)
        self.assertIn("as you approved", self.pushed[0])
        self.assertNotIn("_US_EQ", self.pushed[0])
        self.router.calls.clear()
        self.assertIsNone(self.go())                                     # the page still says "selling": not sent twice
        self.assertEqual(self.router.calls, [])

    def test_nothing_approved_means_no_request_at_all(self):
        for status in ("open", "sell_asked", "closed"):
            self.page(status)
            self.assertIsNone(self.go())
        self.page(account="live")
        self.assertIsNone(self.go())
        self.assertEqual(self.router.calls, [])

    def test_a_refused_sale_puts_the_stop_back_alarms_and_is_left_to_the_close(self):
        self.page()
        self.account(sell=http_error(400, "market closed"), stop=FakeResp({"id": 7002}))
        res = self.go()
        self.assertEqual([p[1] for p in self.router.posts()], ["/equity/orders/market", "/equity/orders/stop"])
        self.assertEqual(self.router.posts()[1][2]["stopPrice"], 358.32)
        self.assertIn("REFUSED", res["failed"][0])
        self.assertTrue(any("APPROVED SALE FAILED" in t for _, t in self.notes) or any("approved" in t.lower() for t in self.pushed))
        self.router.calls.clear()
        self.assertIsNone(self.go())                                     # not tried again every five minutes
        self.assertEqual(self.router.calls, [])
        self.assertEqual(t212.approved_sales(self.dir, "demo"), {TICKER})   # the 15:50 close will try again

    def watch(self, until):
        with redirect_stdout(io.StringIO()):
            t212.main(["guard", "--watch", "--every", "5", "--until", until, "--state-dir", self.dir], environ=CREDS)
        return [(c[0], c[1]) for c in self.router.calls if c[0] != "GET"]

    def test_the_guard_loop_leaves_it_alone_before_0935_new_york(self):
        self.page()
        self.clock[0] = ny(9, 31)
        self.router.add("GET", "/equity/positions", FakeResp([test_eod_close.held(7)]))
        self.router.add("GET", "/equity/orders", FakeResp([STOP]))
        self.assertEqual(self.watch("09:34"), [])

    def test_the_guard_loop_sells_it_from_0935_new_york(self):
        self.page()
        self.clock[0] = ny(9, 36)
        r = self.router
        r.add("GET", "/equity/positions", FakeResp([test_eod_close.held(7)]), FakeResp([]))
        r.add("GET", "/equity/orders", FakeResp([STOP]), FakeResp([]))
        r.add("DELETE", "/equity/orders/7001", FakeResp({}))
        r.add("POST", "/equity/orders/market", FakeResp({"id": 9300, "status": "NEW"}))
        self.assertEqual(self.watch("09:39"), [("DELETE", "/equity/orders/7001"), ("POST", "/equity/orders/market")])

    def test_the_holdings_record_says_why_it_was_sold(self):
        page = {"symbol": "TSLA", "buy_date": "2026-11-03", "reason": "launch",
                "result": {"why": "sold because you approved it", "date": "2026-11-05", "pl_gbp": -4.2}}
        text = M.news_sold(page)
        self.assertIn("you approved the sale", text)


class OnThePhone(Holdings):
    def test_the_briefing_shows_the_topics_and_my_read_on_each_holding(self):
        topics = [{"text": "Investors are waiting for Thursday's US inflation figure",
                   "source": {"title": "Inflation preview", "url": "https://example.com/cpi", "date": "2026-11-04"}},
                  {"text": "Chip makers fell after new export limits were announced.",
                   "source": {"title": "Export limits", "url": "https://example.com/chips", "date": "2026-11-05"}},
                  {"text": "No source for this one, so it is left out"},
                  {"text": "short", "source": dict(SRC)}]
        self.run_news(answer(idea(symbol="AAPL"), topics=topics,
                             verdicts=[verdict(), verdict("AAPL", "sell", why="Apple cut its guidance on Tuesday.")]))
        d = news.briefing_data(self.dir, "2026-11-05")
        self.assertEqual(len(d["topics"]), 2)
        self.assertEqual([v["symbol"] for v in d["verdicts"]], ["AAPL", "TSLA"])       # sell first
        lines = M.news_briefing(d)
        text = M.briefing({"session": DAY, "prev": D(2026, 11, 4), "account": {}}, "trader/briefings/x.md", watch=lines)
        self.assertIn("What the market is talking about", text)
        self.assertIn("Investors are waiting for Thursday's US inflation figure. (Inflation preview, ", text)
        self.assertIn("My read on what I'm holding", text)
        self.assertIn("<b>keep</b> (medium confidence). The launch date is confirmed again. Source: Launch delayed", text)
        self.assertIn("<b>sell</b>", text)
        self.assertIn("Noted only: this one is sold by its date rule", text)
        self.assertLess(text.index("What the market is talking about"), text.index("News ideas"))
        for jargon in ("SHADOW", "_US_EQ", "ATR", "trader-risk", "drift", "verdict"):
            self.assertNotIn(jargon, text)
        self.assertEqual(d["topics"][0]["source"]["url"], "https://example.com/cpi")   # the briefing file lists the links

    def test_topics_are_asked_for_in_the_main_run_only(self):
        self.assertIn("HOT TOPICS", news.prompt(DAY, {"AAPL": ""}, S))
        self.assertIn("\"topics\"", news.prompt(DAY, {"AAPL": ""}, S))
        up = news.update_prompt(DAY, {"AAPL": ""}, S)
        self.assertNotIn("HOT TOPICS", up)
        self.assertNotIn("\"topics\"", up)

    def test_at_most_five_topics_each_with_a_dated_source_the_run_saw(self):
        src = lambda n: {"title": "T%d" % n, "url": "https://example.com/t%d" % n, "date": "2026-11-04"}
        raw = [{"text": "Topic number %d is being talked about." % n, "source": src(n)} for n in range(7)]
        self.assertEqual(len(news.clean_topics(raw, DAY)), 5)
        web = {"opened": ["https://example.com/t1"], "seen": ["https://example.com/t2"]}
        self.assertEqual([t["source"]["title"] for t in news.clean_topics(raw, DAY, web)], ["T1", "T2"])
        future = [{"text": "A topic dated tomorrow is not used.", "source": dict(src(1), date="2026-11-06")}]
        self.assertEqual(news.clean_topics(future, DAY), [])
        self.assertEqual(news.clean_topics("nonsense", DAY), [])
        self.assertEqual(M.hot_topics([]), [])

    def test_the_update_speaks_when_its_read_on_a_holding_gets_worse(self):
        self.run_news(answer(verdicts=[verdict(), verdict("AAPL")]))
        self.update(keep(verdicts=[verdict(), verdict("AAPL", "watch", why="Guidance was cut before the open.")]))
        texts = [t for t, k in self.pushed_news if k == "news-update-2026-11-05"]
        self.assertEqual(len(texts), 1)
        self.assertIn("keep, but watch it closely", texts[0])
        self.assertIn("Guidance was cut before the open.", texts[0])
        self.assertNotIn("TSLA", texts[0].split("My read")[1])           # unchanged ones are not repeated

    def test_the_update_stays_silent_when_nothing_changed(self):
        self.run_news(answer(verdicts=[verdict(), verdict("AAPL", "watch")]))
        self.update(keep(verdicts=[verdict(), verdict("AAPL", "watch")]))
        self.assertEqual(self.pushed_news, [])

    def test_the_daily_review_shows_todays_verdict_beside_each_holding(self):
        doc = self.run_news(answer(verdicts=[verdict(), verdict("AAPL", "sell", why="Apple cut its guidance on Tuesday.")]))
        self.assertEqual(news.todays_verdicts(self.dir, DAY), doc["verdicts"])
        self.assertEqual(news.todays_verdicts(self.dir, D(2026, 11, 6)), {})
        os.remove(H.path(self.dir))
        v = dict(TH.view([TH.pos(), TH.pos("AAPL", 2, 200.0, 201.0)], [TH.stop(), TH.stop("AAPL", 2, 190.0)],
                         [TH.buy(), TH.buy("AAPL", strategy="drift")]), cash_gbp=2500.0)
        hd = H.load(self.dir)
        H.sync(hd, v, TH.TODAY)
        H.save(self.dir, hd)
        res = {"view": v, "mismatches": [], "pausing": [], "risk": H.open_risk(v), "new": [], "events": [], "pause": None}
        cfg = {"paper": True, "cash_buffer_pct": 10, "allowed_tickers": ["TSLA", "AAPL"], "blocked_tickers": [],
               "risk_caps_pct": {"drift": 2, "news": 2, "intraday": 1}}
        r = review.build(self.dir, cfg, res, DAY, verdicts=doc["verdicts"])
        self.assertEqual({x["symbol"]: x["verdict"]["verdict"] for x in r["rows"]}, {"TSLA": "keep", "AAPL": "sell"})
        text = M.daily_review(r)
        self.assertIn("Today's read: keep (The launch date is confirmed again.)", text)
        self.assertIn("Today's read: sell (Apple cut its guidance on Tuesday.). Noted only: this one is sold by its date rule", text)
        self.assertIn("today's verdict from the news runs: sell", review.text(r))
        self.assertIn("written down only, the date rule decides", review.text(r))
        self.assertTrue(r["report_only"])
        plain = review.build(self.dir, cfg, res, DAY)
        self.assertEqual([x["verdict"] for x in plain["rows"]], [None, None])
        self.assertNotIn("Today's read", M.daily_review(plain))


class WouldFollowingTheVerdictHaveDoneBetter(Holdings):
    def close_drift(self, pl=-20.0, date="2026-11-10", **over):
        doc = H.load(self.dir)
        page = H.find(doc, "p2")
        page.update(dict(status="closed", result={"date": date, "price": 350.0, "why": "sold at the close", "pl_gbp": pl}, **over))
        H.save(self.dir, doc)

    def week(self, o=362.0):
        out = {}
        for d in (5, 6, 9, 10):
            out[D(2026, 11, d)] = (o, o + 2, o - 2, o, 1000.0)
        return out

    def test_a_drift_sell_is_compared_with_what_the_rule_did_once_the_holding_is_finished(self):
        self.run_news(answer(verdicts=[verdict("AAPL", "sell", why="Apple cut its guidance on Tuesday.")]))
        self.assertEqual(news.resolve_verdicts(self.dir, D(2026, 11, 9), S, lambda s, a, b: self.week()), 0)   # still held
        self.assertEqual(news.verdict_tally(self.dir)["drift"]["open_sells"], 1)
        self.close_drift()
        self.assertEqual(news.resolve_verdicts(self.dir, D(2026, 11, 11), S, lambda s, a, b: self.week()), 1)
        row = [r for r in self.rows() if r["symbol"] == "AAPL"][0]
        k = 0.0015 + 0.0005                                              # sold at the next day's open, after fees
        self.assertEqual(row["would"], {"day": "2026-11-06", "open": 362.0,
                                        "pl_gbp": round(3 * (362.0 * (1 - k) - 360.0 * (1 + k)) / RATE, 2)})
        self.assertEqual(row["rule"]["pl_gbp"], -20.0)
        self.assertEqual(news.resolve_verdicts(self.dir, D(2026, 11, 11), S, lambda s, a, b: self.week()), 0)   # once
        t = news.verdict_tally(self.dir)["drift"]
        self.assertEqual((t["sell"], t["compared"], t["would_gbp"], t["rule_gbp"], t["open_sells"]),
                         (1, 1, row["would"]["pl_gbp"], -20.0, 0))
        self.assertEqual(self.status("p2"), "closed")                    # the verdict itself never changed anything

    def test_sold_before_the_verdict_could_have_acted_is_not_compared(self):
        self.run_news(answer(verdicts=[verdict("AAPL", "sell", why="Apple cut its guidance on Tuesday.")]))
        self.close_drift(date="2026-11-05")                              # its stop went the same day
        self.assertEqual(news.resolve_verdicts(self.dir, D(2026, 11, 11), S, lambda s, a, b: self.week()), 1)
        self.assertTrue(self.rows()[-1]["would"]["same"] if self.rows()[-1]["symbol"] == "AAPL" else
                        [r for r in self.rows() if r["symbol"] == "AAPL"][0]["would"]["same"])
        self.assertEqual(news.verdict_tally(self.dir)["drift"]["compared"], 0)

    def test_the_friday_report_and_message_show_it(self):
        self.run_news(answer(verdicts=[verdict(), verdict("AAPL", "sell", why="Apple cut its guidance on Tuesday.")]))
        self.close_drift()
        news.resolve_verdicts(self.dir, D(2026, 11, 11), S, lambda s, a, b: self.week())
        lines = "\n".join(news.friday_lines(self.dir, self.cfg))
        self.assertIn("## Verdicts on what is held", lines)
        self.assertIn("- drift: 0 keep, 0 watch, 1 sell", lines)
        self.assertIn("1 finished holding with a sell verdict that was not acted on", lines)
        self.assertIn("what happened made GBP -20.00", lines)
        phone = "\n".join(news.friday_phone(self.dir, self.cfg))
        self.assertIn("My daily read on earnings trades so far: 0 keep, 0 watch, 1 sell", phone)
        self.assertIn("selling the next morning would have made", phone)
        self.assertIn("My daily read on news ideas so far: 1 keep", phone)
        self.assertEqual(M.friday_verdicts({}), [])


if __name__ == "__main__":
    unittest.main()
