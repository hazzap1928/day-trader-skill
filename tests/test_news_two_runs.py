"""The news strategy's two runs a day (trader-broker/news_ideas.py): the 06:00 UK main review, whose ideas go
into the 07:30 briefing, and the 08:30 New York update, which keeps or drops each idea. Also: confidence,
the hold-length check, the price recheck at buy time, what a run used, and the plan's usage limit.
A fake Claude answer, fake bars, a fake Trading 212 and phone: no network, no real Claude run, no order."""

import calendar
import datetime
import json
import os
import subprocess
import unittest

from _load import load
import test_autopilot
import test_news_ideas as TN
from test_news_ideas import NewsBase, idea, answer, keep, news, M, t212, H, HT, drift, alloc, DAY, RATE, FULL
from test_drift_buy import bars, held

D = datetime.date


def utc(h, m, day=5, month=11, year=2026):
    return datetime.datetime(year, month, day, h, m)


def clock(h, m):
    return float(calendar.timegm((2026, 11, 5, h, m, 0)))


S = news.settings({})


class Timing(unittest.TestCase):
    """New York time decides. In November New York is UTC-5."""

    def test_the_main_review_must_be_able_to_finish_before_the_update(self):
        self.assertEqual(news.timing("main", S, utc(6, 0))[0], "ok")          # 06:00 UK = 01:00 New York
        self.assertEqual(news.timing("main", S, utc(13, 5))[0], "ok")         # 08:05 + 25 min = 08:30
        when, why = news.timing("main", S, utc(13, 6))
        self.assertEqual(when, "late")
        self.assertIn("could not finish before the 08:30 update", why)

    def test_the_update_waits_for_0830_new_york_and_must_finish_before_the_buy_step(self):
        self.assertEqual(news.timing("update", S, utc(12, 30))[0], "early")   # 07:30 New York
        self.assertEqual(news.timing("update", S, utc(13, 30))[0], "ok")
        self.assertEqual(news.timing("update", S, utc(14, 20))[0], "ok")      # 09:20 + 15 min = 09:35
        when, why = news.timing("update", S, utc(14, 21))
        self.assertEqual(when, "late")
        self.assertIn("could not finish before the 09:35 buy step", why)

    def test_when_the_clocks_differ_0830_new_york_is_1230_uk(self):
        # mid-March: the US is on summer time, the UK is not yet. launchd fires at 12:30 and 13:30 UK;
        # the 12:30 one is the right one then, and the 13:30 one finds the run already done
        self.assertEqual(news.timing("update", S, utc(12, 30, day=15, month=3, year=2027))[0], "ok")
        self.assertEqual(news.timing("update", S, utc(12, 29, day=15, month=3, year=2027))[0], "early")

    def test_settings(self):
        self.assertEqual((S["main_at"], S["update_at"], S["update_timeout_min"], S["max_move_pct"], S["buy_confidence"],
                          S["max_days_after_event"]), ("06:00", "08:30", 15, 5.0, "high", 10))
        for bad in ({"update_at": "soon"}, {"max_move_pct": 0}, {"buy_confidence": "sure"}, {"update_timeout_min": 0},
                    {"max_days_after_event": 0}):
            with self.assertRaises(news.NewsError, msg=bad):
                news.settings({"news": bad})


class TheMainReview(NewsBase):
    def test_its_ideas_go_into_the_briefing_not_a_message_of_their_own(self):
        self.reads()
        doc = self.run_news(answer(idea(), idea(symbol="NVDA")))
        self.assertEqual(self.pushed_news, [])                           # no separate message
        self.assertEqual((doc["runs"]["main"]["status"], doc["ideas"][0]["state"], doc["ideas"][0]["from"]),
                         ("ok", "proposed", "main"))
        d = news.briefing_data(self.dir, "2026-11-05")
        self.assertEqual((d["state"], [r["symbol"] for r in d["ideas"]], d["rejected"], d["buying"]), ("ok", ["TSLA"], 1, False))
        lines = M.news_briefing(d)
        text = M.briefing({"session": DAY, "prev": D(2026, 11, 4), "account": {}}, "trader/briefings/x.md", watch=lines)
        self.assertIn("News ideas", text)
        self.assertIn("Tesla (TSLA)", text)
        self.assertIn("high confidence", text)
        self.assertIn("practice only", text)
        self.assertIn("check them again before the US market opens", text)
        for jargon in ("SHADOW", "_US_EQ", "ATR", "trader-risk"):
            self.assertNotIn(jargon, text)

    def test_the_briefing_module_reads_it(self):
        briefing = load("trader-broker", "briefing")
        self.reads()
        self.run_news(answer(idea()))
        d = briefing.news_ideas_data(self.dir, DAY)
        self.assertEqual([r["symbol"] for r in d["ideas"]], ["TSLA"])
        self.assertIsNone(briefing.news_ideas_data(self.dir + "/nowhere", DAY))

    def test_a_late_main_review_gets_its_own_message_because_the_briefing_has_gone(self):
        self.reads()
        self.briefing_gone()
        self.run_news(answer(idea()))
        self.assertEqual([k for _, k in self.pushed_news], ["news-ideas-2026-11-05"])

    def test_a_failure_before_the_briefing_is_said_in_the_briefing(self):
        doc = self.run_news(news.NewsError("the Claude run ended with code 1: not logged in"))
        self.assertEqual(self.pushed_news, [])
        self.assertEqual((doc["status"], doc["runs"]["main"]["status"]), ("failed", "failed"))
        lines = M.news_briefing(news.briefing_data(self.dir, "2026-11-05"))
        self.assertIn("failed", lines[1])
        self.assertNotIn("usage limit", lines[1])

    def test_the_briefing_says_not_finished_skipped_or_nothing(self):
        self.assertIn("hasn't finished yet", M.news_briefing(news.briefing_data(self.dir, "2026-11-05"))[1])
        news.mark_skipped(self.dir, "main", DAY, "too late")
        self.assertIn("skipped", M.news_briefing(news.briefing_data(self.dir, "2026-11-05"))[1])
        self.assertEqual(M.news_briefing(None), [])
        self.write("config.json", dict(self.cfg, news={"enabled": False}))
        self.assertIsNone(news.briefing_data(self.dir, "2026-11-05"))

    def test_too_late_to_finish_before_the_update_it_is_skipped_not_run(self):
        self.clock[0] = clock(13, 10)                                    # 08:10 New York
        doc = self.run_news(answer(idea()))
        self.assertEqual(self.asked, [])                                 # Claude was never asked
        self.assertEqual(doc["runs"]["main"]["status"], "skipped")
        self.assertEqual(self.pushed_news, [])

    def test_a_run_that_became_too_late_while_it_waited_is_skipped_not_left_running(self):
        news.begin(self.dir, "main", DAY, S)                             # marked, then the internet took too long
        self.clock[0] = clock(13, 10)
        doc = self.run_news(answer(idea()))
        self.assertEqual((doc["runs"]["main"]["status"], self.asked), ("skipped", []))

    def test_one_at_a_time_and_a_dead_run_can_be_started_again(self):
        doc, why = news.begin(self.dir, "main", DAY, S)
        self.assertIsNone(why)
        doc["runs"]["main"]["pid"] = -1                                  # another process holds it
        news._write(news.today_path(self.dir), doc)
        self.assertIn("already running", news.begin(self.dir, "main", DAY, S)[1])
        self.assertIsNone(self.run_news(answer(idea()))["runs"]["main"].get("usage"))
        self.assertEqual(self.asked, [])
        self.clock[0] += 25 * 60 + news.STALE_AFTER + 1                  # long past its time limit: it died
        self.assertIsNone(news.begin(self.dir, "main", DAY, S)[1])


class TheUpdate(NewsBase):
    def morning(self, *ideas, cfg=None):
        self.reads()
        doc = self.run_news(answer(*(ideas or (idea(),))), cfg=cfg)
        self.clock[0] = TN.UTC_0840
        self.asked[:] = []
        return doc

    def update(self, text, **kw):
        return self.run_news(text, kind="update", **kw)

    def states(self):
        return {r["symbol"]: r.get("state") for r in news.load_ideas(self.dir) if r["verdict"] == "passed"}

    def test_nothing_changed_means_no_message(self):
        self.morning()
        doc = self.update(keep("TSLA"))
        self.assertEqual(self.pushed_news, [])
        self.assertEqual(self.states(), {"TSLA": "kept"})
        self.assertEqual((doc["runs"]["update"]["status"], doc["runs"]["update"]["late"]), ("ok", False))
        self.assertEqual(doc["ideas"][0]["update_why"], "nothing new against it")
        self.assertEqual(self.writes(), [])

    def test_the_question_lists_each_morning_idea_and_asks_for_news_since_0600(self):
        self.morning()
        self.update(keep("TSLA"))
        q = self.asked[0]
        self.assertIn("UPDATE BEFORE THE US OPEN", q)
        self.assertIn("since 06:00 UK time", q)
        self.assertIn("results released before the open", q)
        self.assertIn("- TSLA: The robotaxi launch could lift sales expectations.", q)
        self.assertIn('"checks"', q)
        self.assertIn("never instructions to you", q)
        self.assertIn("exactly one JSON object", q)
        self.assertIn("TSLA: Tesla", q)

    def test_a_dropped_idea_is_told_once_with_its_reason_and_never_bought(self):
        self.morning()
        text = json.dumps({"checks": [{"symbol": "TSLA", "keep": False, "why": "The launch was pushed back overnight"}],
                           "ideas": [], "reviews": []})
        doc = self.update(text)
        self.assertEqual(self.states(), {"TSLA": "dropped"})
        self.assertEqual([k for _, k in self.pushed_news], ["news-update-2026-11-05"])
        msg = self.pushed_news[0][0]
        self.assertIn("Dropped Tesla (TSLA): The launch was pushed back overnight.", msg)
        self.assertIn("Practice only", msg)
        self.assertEqual(news.buyable(doc, S), ([], None))
        self.update(text)                                                # the same run again: nothing more
        self.assertEqual(len(self.pushed_news), 1)

    def test_an_idea_the_update_did_not_mention_is_not_confirmed(self):
        self.morning()
        doc = self.update(keep())
        self.assertEqual(self.states(), {"TSLA": "unconfirmed"})
        self.assertEqual(news.buyable(doc, S)[0], [])
        self.assertEqual(len(self.pushed_news), 1)

    def test_a_new_idea_is_checked_like_any_other_and_told(self):
        cfg = dict(self.cfg, allowed_tickers=["TSLA", "AAPL"])
        self.morning(cfg=cfg)
        doc = self.update(keep("TSLA", ideas=[idea(symbol="AAPL", company="Apple"), idea(symbol="NVDA")]), cfg=cfg)
        self.assertEqual(self.states(), {"TSLA": "kept", "AAPL": "new"})
        new = [r for r in doc["ideas"] if r["symbol"] == "AAPL"][0]
        self.assertEqual((new["from"], new["verdict"], new["risk"]["results"][0]["verdict"]), ("update", "passed", "PASS"))
        self.assertEqual([r["symbol"] for r in doc["rejected"]], ["NVDA"])
        msg = self.pushed_news[0][0]
        self.assertIn("New: <b>Apple (AAPL)</b>", msg)
        self.assertIn("Still standing from this morning: Tesla (TSLA).", msg)
        self.assertEqual(sorted(r["symbol"] for r in news.buyable(doc, S)[0]), ["AAPL", "TSLA"])

    def test_the_update_cannot_repeat_a_morning_idea_as_new(self):
        self.morning()
        doc = self.update(keep("TSLA", ideas=[idea()]))
        self.assertEqual(len([r for r in doc["ideas"] if r["symbol"] == "TSLA"]), 1)
        self.assertIn("a second idea for the same company today", doc["rejected"][0]["why_not"])
        ids = [r["id"] for r in news.load_ideas(self.dir)]
        self.assertEqual(len(ids), len(set(ids)))

    def test_before_0830_new_york_it_does_nothing_at_all(self):
        self.morning()
        self.clock[0] = clock(12, 30)                                    # the 12:30 UK start in winter: 07:30 New York
        self.assertIsNone(self.update(keep("TSLA")))
        self.assertEqual(self.asked, [])
        self.assertNotIn("update", self.read("state/news/today.json")["runs"])
        self.clock[0] = clock(13, 30)
        self.assertEqual(self.update(keep("TSLA"))["runs"]["update"]["status"], "ok")

    def test_too_late_to_finish_before_the_buy_step_it_is_skipped_and_nothing_is_bought(self):
        on = dict(self.cfg, news={"buy_enabled": True})
        self.morning(cfg=on)
        self.clock[0] = clock(14, 25)                                    # 09:25 New York: 10 minutes to the buy step
        doc = self.update(keep("TSLA"), cfg=on)
        self.assertEqual(self.asked, [])
        self.assertEqual(doc["runs"]["update"]["status"], "skipped")
        self.assertEqual([k for _, k in self.pushed_news], ["news-update-missed-2026-11-05"])
        self.assertIn("nothing is bought on news today", self.pushed_news[0][0])
        self.router.calls.clear()
        res = news.buy_today(self.client, self.dir, on, TN.NY_0935, RATE, lambda s: 366.0, False, self.said.append,
                             self.pushed_db.append)
        self.assertEqual(res["bought"], [])
        self.assertIn("did not finish (skipped)", res["status"])
        self.assertEqual(self.router.calls, [])

    def test_a_failed_update_is_told_once_and_nothing_is_bought(self):
        on = dict(self.cfg, news={"buy_enabled": True})
        self.morning(cfg=on)
        e = news.NewsError("the Claude run reported an error: Claude usage limit reached")
        e.limit = True
        doc = self.update(e, cfg=on)
        self.assertEqual((doc["status"], doc["runs"]["update"]["status"], doc["runs"]["update"]["limit"]), ("ok", "failed", True))
        self.assertEqual([k for _, k in self.pushed_news], ["news-update-failed-2026-11-05"])
        self.assertIn("usage limit", self.pushed_news[0][0])
        self.assertIn("nothing is bought on news today", self.pushed_news[0][0])
        self.assertEqual(self.states(), {"TSLA": "proposed"})            # the morning idea stays on the log
        self.router.calls.clear()
        res = news.buy_today(self.client, self.dir, on, TN.NY_0935, RATE, lambda s: 366.0, False, self.said.append,
                             self.pushed_db.append)
        self.assertEqual(res["bought"], [])
        self.assertIn("did not finish (failed)", res["status"])
        self.assertEqual(self.router.calls, [])

    def test_with_no_main_review_the_update_works_alone(self):
        self.clock[0] = TN.UTC_0840
        self.reads()
        doc = self.update(keep(ideas=[idea()]))
        self.assertIn("There are no morning ideas to check today", self.asked[0])
        self.assertEqual(self.states(), {"TSLA": "new"})
        self.assertEqual([r["symbol"] for r in news.buyable(doc, S)[0]], ["TSLA"])
        self.assertEqual(self.run_news(answer(idea()))["runs"]["main"]["status"], "skipped")   # never run after it
        self.clock[0] = TN.UTC_0700
        self.assertNotIn("usage", self.run_news(answer(idea()))["runs"]["main"])
        self.assertEqual(len(self.asked), 1)

    def test_an_update_that_finished_after_the_buy_time_is_never_bought_on(self):
        self.morning()
        self.clock[0] = clock(14, 50)                                    # forced by hand at 09:50 New York
        doc = self.update(keep("TSLA"), force=True)
        self.assertTrue(doc["runs"]["update"]["late"])
        self.assertIn("after the buy time", news.buyable(doc, S)[1])

    def test_a_broken_reason_found_by_the_update_asks_and_never_sells(self):
        page = {"id": "p1", "symbol": "TSLA", "strategy": "news", "status": "open", "broker": "trading212",
                "account": "demo", "broker_ref": {"ref": "TSLA_US_EQ"}, "reason": "launch", "event": "launch",
                "event_date": "2026-11-19", "wrong_if": "delay"}
        doc = H.load(self.dir)
        doc["pages"].append(page)
        H.save(self.dir, doc)
        self.clock[0] = TN.UTC_0840
        self.reads()
        out = self.update(keep(reviews=[{"symbol": "TSLA", "broken": True, "why": "The launch was delayed to next year.",
                                         "sources": [{"title": "Delayed", "url": "https://example.com/d", "date": "2026-11-05"}]}]))
        self.assertIn("HOLDINGS", self.asked[0])
        self.assertEqual(len(out["asks"]), 1)
        self.assertEqual(H.load(self.dir)["pages"][0]["status"], "sell_asked")
        self.assertEqual(self.writes(), [])


class CatchUp(NewsBase):
    """What the always-on autopilot does about a run the Mac slept through."""

    def at(self, h, m):
        return news.catch_up(self.dir, self.cfg, utc(h, m), utc(h, m))    # UK is on UTC in November

    def test_nothing_before_its_time_plus_ten_minutes(self):
        self.assertEqual(self.at(5, 30), [])
        self.assertEqual(self.at(6, 9), [])

    def test_a_missed_main_review_runs_when_the_mac_wakes(self):
        self.assertEqual([(k, w) for k, w, _ in self.at(6, 10)], [("main", "run")])
        self.assertEqual([(k, w) for k, w, _ in self.at(9, 0)], [("main", "run")])

    def test_but_not_once_it_can_no_longer_finish_before_the_update(self):
        self.assertEqual([(k, w) for k, w, _ in self.at(13, 20)], [("main", "skip")])        # 08:20 New York
        got = self.at(13, 45)                                             # 08:45: the update is due as well
        self.assertEqual([(k, w) for k, w, _ in got], [("main", "skip"), ("update", "run")])

    def test_a_missed_update_runs_until_0920_new_york_then_is_skipped(self):
        self.reads()
        self.run_news(answer(idea()))
        self.assertEqual(self.at(13, 35), [])                             # launchd's own start gets ten minutes
        self.assertEqual([(k, w) for k, w, _ in self.at(13, 40)], [("update", "run")])
        self.assertEqual([(k, w) for k, w, _ in self.at(14, 20)], [("update", "run")])
        self.assertEqual([(k, w) for k, w, _ in self.at(14, 30)], [("update", "skip")])
        self.clock[0] = TN.UTC_0840
        self.run_news(keep("TSLA"), kind="update")
        self.assertEqual(self.at(14, 0), [])                              # done: nothing to catch up

    def test_never_on_a_holiday_a_weekend_live_or_switched_off(self):
        sat = datetime.datetime(2026, 11, 7, 9, 0)
        self.assertEqual(news.catch_up(self.dir, self.cfg, sat, sat), [])
        self.assertEqual(news.catch_up(self.dir, dict(self.cfg, paper=False), utc(9, 0), utc(9, 0)), [])
        self.assertEqual(news.catch_up(self.dir, dict(self.cfg, news={"enabled": False}), utc(9, 0), utc(9, 0)), [])


class AutopilotCatchUp(test_autopilot.PilotBase):
    def setUp(self):
        super().setUp()
        ap = test_autopilot.ap
        self._mod, self._local = ap.mod, ap._local
        mods = {("trader-broker", "news_ideas"): news, ("trader-broker", "holdings"): H, ("trader-broker", "holdings_t212"): HT,
                ("trader-broker", "drift"): drift}
        ap.mod = lambda folder, name: mods.get((folder, name)) or self._mod(folder, name)
        ap._local = lambda ts: datetime.datetime.utcfromtimestamp(ts)
        self._news_now = news._now
        news._now = lambda: self.clock[0]

    def tearDown(self):
        ap = test_autopilot.ap
        ap.mod, ap._local = self._mod, self._local
        news._now = self._news_now
        news.t212, news.drift, news.allocation, news.holdings, news.holdings_t212 = t212, drift, alloc, H, HT
        super().tearDown()

    def test_a_missed_run_is_started_once_as_its_own_process(self):
        day = test_autopilot.TODAY.split("-")
        self.clock[0] = float(calendar.timegm((int(day[0]), int(day[1]), int(day[2]), 9, 0, 0)))      # 09:00 UK
        p = self.pilot()
        p.catch_up_news()
        p.catch_up_news()
        runs = [a for a in self.spawned if a[1].endswith("news_ideas.py")]
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0][2:5], ["run", "--kind", "main"])
        self.assertEqual(self.router.calls, [])                          # starting it touches nothing at Trading 212


class Quality(unittest.TestCase):
    ALLOWED = {"TSLA", "AAPL"}

    def check(self, **over):
        return news.validate(idea(**over), DAY, S, self.ALLOWED, set())

    def test_claude_is_asked_for_two_ratings_what_has_moved_and_a_reason_for_a_long_hold(self):
        q = news.prompt(DAY, {"TSLA": "Tesla"}, S)
        for word in ('"date_confidence"', '"date_why"', '"edge_confidence"', '"edge_why"', '"edge_source"',
                     '"moved_pct"', '"moved_note"', '"long_hold_reason"', "within 10 trading days after the event",
                     "Only this rating decides whether the idea is bought",
                     "\"The shares fell recently\" or \"the outcome is uncertain\" alone are not enough",
                     "Most ideas should be low; medium should be uncommon, high rare.",
                     "General market mood (a falling or jumpy market) is not part of either rating"):
            self.assertIn(word, q)
        self.assertNotIn('"confidence_why"', q)
        self.assertNotIn("Be strict", q)

    def test_both_ratings_are_logged_and_the_edge_decides(self):
        got, why = self.check(edge_confidence=" High ", date_confidence="medium", moved_pct="3.5")
        self.assertIsNone(why)
        self.assertEqual((got["confidence"], got["edge_confidence"], got["date_confidence"], got["edge_note"]),
                         ("high", "high", "medium", None))
        self.assertEqual((got["confidence_why"], got["date_why"], got["moved_pct"]),
                         ("Analysts expect a small launch against the published plan.",
                          "The date is confirmed by the company.", 3.5))
        for odd in (None, "", "certain", 9):
            self.assertEqual(self.check(edge_confidence=odd)[0]["confidence"], "unknown", odd)     # logged, never bought
            self.assertEqual(self.check(date_confidence=odd)[0]["date_confidence"], "unknown", odd)
        self.assertIsNone(self.check(moved_pct="a lot")[0]["moved_pct"])

    def test_a_bare_confidence_without_an_edge_rating_is_never_bought(self):
        raw = idea()
        for k in ("edge_confidence", "edge_why", "edge_source"):
            raw.pop(k)
        got, why = news.validate(dict(raw, confidence="high"), DAY, S, self.ALLOWED, set())
        self.assertEqual((why, got["confidence"]), (None, "unknown"))

    def test_medium_needs_a_reason_backed_by_a_page_the_run_opened(self):
        web = TN.searched(json.dumps(idea())).web                       # every address in the idea was opened
        ok = news.validate(idea(confidence="medium"), DAY, S, self.ALLOWED, set(), web=web)[0]
        self.assertEqual((ok["confidence"], ok["edge_note"]), ("medium", None))
        for over in ({"edge_source": "https://example.com/never-opened"}, {"edge_source": ""}, {"edge_why": "low"}):
            got = news.validate(idea(confidence="medium", **over), DAY, S, self.ALLOWED, set(), web=web)[0]
            self.assertEqual(got["confidence"], "low", over)
            self.assertIn("without a reason backed by a page it opened", got["edge_note"])
        self.assertEqual(self.check(confidence="medium", edge_source="not a link")[0]["confidence"], "low")  # no record

    def test_high_needs_more_than_one_opened_source(self):
        one = [TN.SRC[1]]
        web = TN.searched(json.dumps(idea(sources=one))).web
        got = news.validate(idea(sources=one), DAY, S, self.ALLOWED, set(), web=web)[0]
        self.assertEqual((got["confidence"], got["edge_note"]),
                         ("medium", "it said high with only one source it opened, so this counts as medium"))
        self.assertEqual(news.validate(idea(), DAY, S, self.ALLOWED, set(), web=TN.searched(json.dumps(idea())).web)[0]
                         ["confidence"], "high")

    def test_a_low_edge_is_kept_as_it_is(self):
        got = self.check(confidence="low", edge_source="")[0]
        self.assertEqual((got["confidence"], got["edge_note"]), ("low", None))

    def test_the_prompt_favours_big_events_and_rotates_the_kinds_of_event(self):
        q = news.prompt(DAY, {"TSLA": "Tesla"}, S)
        self.assertIn("Prefer events that are large for the company's size", q)
        a, b = news.kinds_for(DAY)
        self.assertIn("Start today with these kinds of event, which change from day to day: %s and %s." % (a, b), q)
        days = [DAY + datetime.timedelta(days=i) for i in range(4)]
        self.assertEqual(len({k for d in days for k in news.kinds_for(d)}), len(news.EVENT_KINDS))   # all in 4 days
        self.assertNotIn("Earlier runs already made the searches below", q)

    def test_the_last_runs_searches_are_shown_so_they_are_not_repeated(self):
        import tempfile
        sd = tempfile.mkdtemp()
        rows = [{"day": "2026-10-28", "kind": "main", "at": 1, "web": {"queries": ["very old"]}},
                {"day": "2026-11-02", "kind": "main", "at": 2, "web": {"queries": ["PDUFA date December 2026", "a"]}},
                {"day": "2026-11-02", "kind": "update", "at": 3, "web": {"queries": ["b"]}},
                {"day": "2026-11-03", "kind": "main", "at": 4, "web": {"queries": ["pdufa DATE december 2026", "c"]}},
                {"day": "2026-11-04", "kind": "main", "at": 5, "web": {"queries": ["d"]}},
                {"day": "2026-11-04", "kind": "main", "at": 6, "status": "failed"},
                {"day": DAY.isoformat(), "kind": "main", "at": 7, "web": {"queries": ["today's own"]}}]
        with open(os.path.join(news._dir(sd), "runs.jsonl"), "w") as fh:
            fh.write("\n".join(json.dumps(r) for r in rows) + "\nnot json\n")
        got = news.recent_queries(sd, DAY)
        self.assertEqual(got, ["d", "pdufa DATE december 2026", "c", "b", "a"])     # 3 run days, newest first, once
        self.assertEqual(news.recent_queries(sd, DAY, limit=2), ["d", "pdufa DATE december 2026"])
        self.assertEqual(news.recent_queries(tempfile.mkdtemp(), DAY), [])
        q = news.prompt(DAY, {"TSLA": "Tesla"}, S, recent=got)
        self.assertIn("Earlier runs already made the searches below. Do not repeat them word for word", q)
        self.assertIn("  d | pdufa DATE december 2026 | c | b | a", q)

    def test_the_main_run_is_given_the_recent_searches_and_the_update_is_not(self):
        upd = news.update_prompt(DAY, {"TSLA": "Tesla"}, S, morning=[idea()])
        self.assertNotIn("Earlier runs already made the searches below", upd)
        self.assertIn('"edge_confidence"', upd)
        json.loads(upd.split("ANSWER with exactly one JSON object and no other text, in this shape:\n")[1].split("\n")[0])

    def test_the_sell_date_is_at_most_ten_trading_days_after_the_event_unless_a_reason_is_given(self):
        got, why = self.check()                                          # the event on the 19th, sold on the 27th
        self.assertEqual((why, got["days_after_event"]), (None, 5))      # Thanksgiving is not a trading day
        got, why = self.check(sell_date="2026-12-04")                    # the 10th trading day after
        self.assertEqual((why, got["days_after_event"]), (None, 10))
        got, why = self.check(sell_date="2026-12-07")
        self.assertIsNone(got)
        self.assertIn("11 trading days after the event, over the 10 allowed", why)
        self.assertIn("no reason", why)
        self.assertIn("over the 10", self.check(sell_date="2026-12-07", long_hold_reason="yes")[1])    # not a reason
        got, why = self.check(sell_date="2026-12-07", long_hold_reason="Sales figures for the launch come out a month later.")
        self.assertEqual((why, got["days_after_event"]), (None, 11))
        self.assertEqual(got["long_hold_reason"], "Sales figures for the launch come out a month later.")
        self.assertEqual(self.check(event_date="2026-11-27", sell_date="2026-11-20")[0]["days_after_event"], 0)

    def test_the_limit_is_a_setting(self):
        s = news.settings({"news": {"max_days_after_event": 3}})
        self.assertIn("over the 3 allowed", news.validate(idea(), DAY, s, self.ALLOWED, set())[1])


class OnlyHighIsBought(TN.Buying):
    def test_a_medium_idea_is_logged_and_never_bought(self):
        self.morning(idea(confidence="medium"))
        res = self.buy()
        self.assertEqual(res["bought"], [])
        self.assertEqual(res["status"], "nothing to buy today")
        self.assertIn("its confidence is medium: only high confidence ideas are bought", res["skipped"][0][1])
        self.assertEqual(self.router.calls, [])                          # Trading 212 was not even asked
        with open(os.path.join(self.dir, "state", "news", "buys.jsonl")) as fh:
            row = json.loads(fh.readline())
        self.assertEqual((row["status"], row["confidence"], row["symbol"]), ("skipped", "medium", "TSLA"))

    def test_high_is_bought_and_the_rest_of_the_day_is_only_watched(self):
        on = dict(self.on, allowed_tickers=["TSLA", "AAPL"])
        self.write("config.json", on)
        self.reads()
        doc = self.run_news(answer(idea(symbol="AAPL", confidence="low"), idea()), cfg=on)
        self.clock[0] = TN.UTC_0840
        doc = self.run_news(keep("TSLA", "AAPL"), cfg=on, kind="update")
        self.router.routes.clear()
        self.router.calls.clear()
        self.clock[0] = TN.UTC_0935
        qty = [r for r in doc["ideas"] if r["symbol"] == "TSLA"][0]["qty"]
        self.fake_account(qty)
        res = self.buy(on)
        self.assertEqual([b["symbol"] for b in res["bought"]], ["TSLA"], res)
        self.assertEqual([s for s, _ in res["skipped"]], ["AAPL"])

    def test_the_message_says_which_ideas_are_only_watched(self):
        line = M._idea_line(dict(idea(confidence="medium"), cost_gbp=900.0, max_loss_gbp=30.0), True)
        self.assertIn("medium confidence, so it is only watched", line)
        self.assertIn("Would buy", line)
        self.assertIn("I'd buy", M._idea_line(dict(idea(), cost_gbp=900.0, max_loss_gbp=30.0), True))
        self.assertIn("Already moved about", M._idea_line(dict(idea(moved_pct=4.0)), False))

    def test_a_low_date_rating_holds_back_a_high_edge_and_is_logged(self):
        self.morning(idea(date_confidence="low"))
        res = self.buy()
        self.assertEqual(res["bought"], [])
        self.assertEqual(self.router.calls, [])                          # Trading 212 was not even asked
        self.assertIn("held back by its date rating: the date is rated low", res["skipped"][0][1])
        with open(os.path.join(self.dir, "state", "news", "buys.jsonl")) as fh:
            row = json.loads(fh.readline())
        self.assertEqual((row["status"], row["held_back"], row["date_confidence"], row["confidence"]),
                         ("skipped", "date", "low", "high"))

    def test_the_date_gate_also_decides_the_pretend_book(self):
        s = news.settings({"news": {"buy_confidence": ["medium", "high"]}})
        row = {"verdict": "passed", "state": "kept", "confidence": "medium"}
        for date, want in (("high", True), ("medium", True), ("low", False), (None, False)):
            self.assertEqual(news.in_book(dict(row, date_confidence=date), s), want, date)
        self.assertFalse(news.in_book(dict(row, confidence="low", date_confidence="high"), s))
        self.assertTrue(news.held_back_by_date(dict(row, date_confidence="low"), s))
        self.assertFalse(news.held_back_by_date(dict(row, confidence="low", date_confidence="low"), s))

    def test_the_message_says_when_the_date_holds_an_idea_back(self):
        line = M._idea_line(dict(idea(date_confidence="low"), cost_gbp=900.0, max_loss_gbp=30.0), True)
        self.assertIn("high confidence, but its date is not confirmed, so it is only watched", line)
        self.assertIn("Would buy", line)

    def test_a_dropped_idea_is_never_bought(self):
        self.morning(update=json.dumps({"checks": [{"symbol": "TSLA", "keep": False, "why": "results missed"}],
                                        "ideas": [], "reviews": []}))
        res = self.buy()
        self.assertEqual(res["bought"], [])
        self.assertIn("not kept by the update before the open (dropped): results missed", res["skipped"][0][1])
        self.assertEqual(self.router.calls, [])

    def test_without_the_update_nothing_is_bought(self):
        self.morning(update=False)
        res = self.buy()
        self.assertEqual(res["bought"], [])
        self.assertIn("the update before the open did not finish (it did not run)", res["status"])
        self.assertEqual(self.router.calls, [])

    def test_medium_can_be_allowed_by_the_owner_in_the_settings(self):
        on = dict(self.on, news={"buy_enabled": True, "buy_confidence": "medium"})
        self.write("config.json", on)
        self.reads()
        doc = self.run_news(answer(idea(confidence="medium")), cfg=on)
        self.clock[0] = TN.UTC_0840
        doc = self.run_news(keep("TSLA"), cfg=on, kind="update")
        self.assertEqual([r["symbol"] for r in news.buyable(doc, news.settings(on))[0]], ["TSLA"])


class PriceAtBuyTime(TN.Buying):
    """The idea was proposed at $366 (the last close) with a stop at $354."""

    def test_moved_more_than_5_percent_either_way_it_is_skipped_and_logged(self):
        self.morning()
        self.reads()
        res = self.buy(price=385.0)                                      # +5.2%
        self.assertEqual(res["bought"], [])
        self.assertIn("it has moved +5.2% since it was proposed ($366.00 then, $385.00 now), over the 5% allowed",
                      res["skipped"][0][1])
        self.assertEqual(self.router.posts(), [])
        with open(os.path.join(self.dir, "state", "news", "buys.jsonl")) as fh:
            row = [json.loads(x) for x in fh][-1]
        self.assertEqual((row["status"], row["move_pct"], row["price"]), ("skipped", 5.19, 385.0))

    def test_exactly_5_percent_is_still_bought_from(self):
        qty = self.morning()["ideas"][0]["qty"]
        self.fake_account(qty)
        res = self.buy(price=384.3)                                      # +5.0%
        self.assertNotIn("moved", " ".join(w for _, w in res["skipped"]))

    def test_at_or_below_its_stop_it_is_skipped(self):
        self.morning()
        self.reads()
        for px in (354.0, 350.0):
            res = self.buy(price=px)
            self.assertIn("at or under the stop", res["skipped"][0][1], px)
        self.assertEqual(self.router.posts(), [])

    def test_a_fall_of_more_than_5_percent_that_is_still_over_the_stop(self):
        on = dict(self.on, news={"buy_enabled": True, "max_move_pct": 2})
        self.write("config.json", on)
        self.reads()
        self.run_news(answer(idea()), cfg=on)
        self.clock[0] = TN.UTC_0840
        self.run_news(keep("TSLA"), cfg=on, kind="update")
        self.clock[0] = TN.UTC_0935
        res = self.buy(on, price=357.0)                                  # -2.5%, over its $354 stop
        self.assertIn("it has moved -2.5%", res["skipped"][0][1])
        self.assertIn("over the 2% allowed", res["skipped"][0][1])

    def test_paused_by_a_mismatch_says_so(self):
        self.morning()
        res = self.buy(paused="new buys are paused: the holdings record and the broker do not match (MSFT)")
        self.assertEqual(res["status"], "paused")
        self.assertIn("do not match (MSFT)", res["skipped"][0][1])
        self.assertEqual(self.router.calls, [])


class VirtualAndFriday(NewsBase):
    ROW = {"day": "2026-11-05", "sell_date": "2026-11-10", "stop": 90.0, "qty": 10.0, "rate": 1.25, "symbol": "TSLA",
           "price": 100.0}

    def bars(self, *days):
        return {D(2026, 11, d): (o, h, l, c, 1.0) for d, o, h, l, c in days}

    def test_the_pretend_buy_follows_the_same_5_percent_rule(self):
        v = news.virtual(self.ROW, self.bars((5, 106, 108, 105, 107)), D(2026, 11, 6), S)
        self.assertEqual((v["status"], v["pl_gbp"]), ("not bought", 0.0))
        self.assertIn("+6.0% from the price when proposed", v["how"])
        v = news.virtual(self.ROW, self.bars((5, 105, 108, 104, 107)), D(2026, 11, 6), S)
        self.assertEqual(v["status"], "open")
        v = news.virtual(self.ROW, self.bars((5, 94, 96, 93, 95)), D(2026, 11, 6), S)
        self.assertEqual(v["status"], "not bought")

    def rows(self):
        def row(sym, conf, status, pl):
            return {"id": "x-" + sym, "symbol": sym, "day": "2026-11-02", "verdict": "passed", "confidence": conf,
                    "virtual": {"status": status, "pl_gbp": pl}}
        return [row("A", "high", "closed", 30.0), row("B", "high", "open", -10.0), row("C", "medium", "closed", -25.0),
                row("D", "low", "not bought", 0.0), row("E", None, "closed", 5.0),
                {"id": "x-F", "symbol": "F", "verdict": "rejected", "confidence": "high"}]

    def test_results_by_confidence(self):
        news.save_ideas(self.dir, self.rows())
        by = news.by_confidence(self.dir)
        self.assertEqual(by["high"], {"n": 2, "closed": 1, "wins": 1, "pl_gbp": 20.0, "avg_gbp": 10.0})
        self.assertEqual((by["medium"]["n"], by["medium"]["avg_gbp"]), (1, -25.0))
        self.assertNotIn("low", by)                                      # never bought, even on paper: no result
        self.assertEqual(by["unknown"]["n"], 1)

    def test_the_friday_report_and_message_show_them(self):
        news.save_ideas(self.dir, self.rows())
        lines = news.friday_lines(self.dir, self.cfg)
        text = "\n".join(lines)
        self.assertIn("## News ideas (demo, practice only: nothing is bought)", text)
        self.assertIn("- high confidence: 2 ideas (1 finished), 1 made money, total GBP +20.00, average GBP +10.00 each", text)
        self.assertIn("- medium confidence: 1 idea", text)
        phone = "\n".join(news.friday_phone(self.dir, self.cfg))
        self.assertIn("High confidence: 2 ideas, 1 made money", phone)
        self.assertIn("Only high-confidence ideas may be bought", phone)
        self.assertIn("£", phone)
        self.assertIsNone(news.friday_lines(self.dir, dict(self.cfg, news={"enabled": False})))
        self.assertIn("No idea has a result yet", "\n".join(M.friday_news({})))

    def test_the_friday_evidence_report_carries_the_news_section(self):
        evidence = load("trader-broker", "evidence")
        evidence._news = lambda: news
        news.save_ideas(self.dir, self.rows())
        self.assertIn("## News ideas", "\n".join(evidence.news_lines(self.dir, self.cfg)))
        r = {"today": "2026-11-06", "start": "2026-11-09", "week": None, "since_start": None}
        evidence.t212 = lambda: t212
        self.assertIn("High confidence: 2 ideas", evidence.phone_text(self.dir, self.cfg, r))

    def test_only_ideas_that_would_be_bought_use_up_the_pretend_book(self):
        cfg = dict(self.cfg, max_total_risk_pct=1, allowed_tickers=["TSLA", "AAPL"])     # GBP 50: room for one
        self.reads()
        doc = self.run_news(answer(idea(confidence="medium"), idea(symbol="AAPL")), cfg=cfg)
        self.assertEqual([r["symbol"] for r in doc["ideas"]], ["TSLA", "AAPL"])       # the watched one took no room
        doc = self.run_news(answer(idea(), idea(symbol="AAPL")), cfg=cfg, force=True)
        self.assertEqual([r["symbol"] for r in doc["ideas"]], ["TSLA"])               # two that would be bought: one fits


class NewsHasItsOwnCap(NewsBase):
    """"risk_caps_pct": news is held to its own share, and another strategy's risk does not use it up."""

    NAKED = {"instrument": {"ticker": "AAPL_US_EQ"}, "quantity": 5, "currentPrice": 200.0, "averagePricePaid": 200.0}

    def test_another_strategys_risk_does_not_block_a_news_idea(self):
        one_cap = dict(self.cfg, max_position_pct=22)
        self.reads(positions=[self.NAKED])                               # no stop, no record: GBP 800 at risk elsewhere
        self.assertEqual(self.run_news(answer(idea()), cfg=one_cap)["ideas"], [])        # the single 4% cap: blocked
        split = dict(one_cap, risk_caps_pct={"drift": 2, "news": 2, "intraday": 1})
        doc = self.run_news(answer(idea()), cfg=split, force=True)
        self.assertEqual([r["symbol"] for r in doc["ideas"]], ["TSLA"])
        self.assertIn("the news holdings", " ".join(doc["ideas"][0]["risk"]["results"][0]["info"]))

    def test_news_cannot_go_over_its_own_cap(self):
        tight = dict(self.cfg, risk_caps_pct={"drift": 2, "news": 0.5, "intraday": 1})   # GBP 25: under one idea
        self.reads()
        doc = self.run_news(answer(idea()), cfg=tight)
        self.assertEqual(doc["ideas"], [])
        self.assertIn("total risk", doc["rejected"][0]["why_not"])
        self.assertIn("0.50% of the account", doc["rejected"][0]["why_not"])


class WhatARunUses(unittest.TestCase):
    def fake(self, payload, code=0, stderr=""):
        def run(cmd, **kw):
            self.cmd, self.kw = cmd, kw
            return subprocess.CompletedProcess(cmd, code, stdout=payload if isinstance(payload, str) else json.dumps(payload),
                                               stderr=stderr)
        return run

    def call(self, payload, s=None, **kw):
        real, real_exists = subprocess.run, os.path.exists
        old = os.environ.get("CLAUDE_BIN")
        os.environ["CLAUDE_BIN"] = "/fake/claude"
        subprocess.run = self.fake(payload, **kw)
        os.path.exists = lambda p: True if p == "/fake/claude" else real_exists(p)
        try:
            return news.run_claude("question", s or dict(S))
        finally:
            subprocess.run, os.path.exists = real, real_exists
            if old is None:
                os.environ.pop("CLAUDE_BIN", None)
            else:
                os.environ["CLAUDE_BIN"] = old

    def test_the_time_turns_and_tokens_of_a_run_are_kept(self):
        out = self.call({"is_error": False, "result": "{\"ideas\": []}", "duration_ms": 412345, "num_turns": 23,
                         "total_cost_usd": 1.2345,
                         "usage": {"input_tokens": 1200, "cache_creation_input_tokens": 30000, "cache_read_input_tokens": 400000,
                                   "output_tokens": 9000, "server_tool_use": {"web_search_requests": 14, "web_fetch_requests": 6}},
                         "modelUsage": {"claude-x": {}}})
        self.assertEqual(str(out), "{\"ideas\": []}")
        self.assertEqual(out.meta, {"seconds": 412, "turns": 23, "cost_usd_if_api": 1.2345, "tokens_in": 431200,
                                    "tokens_out": 9000, "models": ["claude-x"], "web_searches": 14, "web_fetches": 6})

    def test_the_update_gets_its_own_time_limit_and_model(self):
        self.call({"is_error": False, "result": "{}"}, dict(S, timeout_min=7, model="sonnet"))
        self.assertEqual(self.kw["timeout"], 7 * 60)
        self.assertEqual(self.cmd[self.cmd.index("--model") + 1], "sonnet")

    def test_the_plans_usage_limit_is_recognised(self):
        for payload, kw in (({"is_error": True, "result": "Claude AI usage limit reached|1762350000"}, {}),
                            ({"is_error": True, "result": "You've hit your session limit. It resets at 3pm."}, {}),
                            ("", {"code": 1, "stderr": "Error: usage limit reached"})):
            with self.assertRaises(news.NewsError) as cm:
                self.call(payload, **kw)
            self.assertTrue(cm.exception.limit, payload)
        with self.assertRaises(news.NewsError) as cm:
            self.call({"is_error": True, "result": "Invalid API key"})
        self.assertFalse(cm.exception.limit)

    def test_on_the_phone_the_limit_is_said_plainly(self):
        self.assertIn("your Claude plan had reached its usage limit", M.news_failed(True))
        self.assertNotIn("usage limit", M.news_failed())
        self.assertIn("usage limit", M.news_update_failed(True, 2, True))
        self.assertIn("nothing is bought on news today", M.news_update_failed(False, 2, True))
        self.assertIn("they stay unconfirmed", M.news_update_missed(1, False))


class UsageIsLogged(NewsBase):
    def test_runs_jsonl_keeps_what_each_run_used(self):
        self.reads()
        ans = news.Answer(answer(idea()))
        ans.meta = {"seconds": 300, "turns": 20, "tokens_in": 400000, "tokens_out": 8000, "cost_usd_if_api": 1.1}
        doc = self.run_news(ans)
        self.assertEqual(doc["runs"]["main"]["usage"]["tokens_out"], 8000)
        with open(os.path.join(self.dir, "state", "news", "runs.jsonl")) as fh:
            row = json.loads(fh.readline())
        self.assertEqual((row["kind"], row["status"], row["usage"]["seconds"]), ("main", "ok", 300))
        self.assertIn("300s, 20 turns", " ".join(self.said_news))
        self.assertIn("main run: ok; 300s, 20 turns, 400000 tokens in, 8000 out", news.show(self.dir))


if __name__ == "__main__":
    unittest.main()
