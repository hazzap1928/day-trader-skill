"""The daily review of what is held (trader-broker/review.py, report only) and "pause new buys on a
mismatch" (the holdings record against Trading 212). A fake Trading 212 and phone, no network, no order."""

import datetime
import json
import os
import tempfile
import unittest

from _load import load
import test_autopilot
import test_drift_buy
import test_eod_close
import test_holdings as TH
from test_eod_close import STOP, held, record, ny
from test_holdings import view, pos, stop, buy, RATE, TODAY
from test_t212_place import FakeResp, SUMMARY

t212 = test_eod_close.t212
H, HT, report = TH.H, TH.HT, TH.report
drift = test_drift_buy.drift
review = load("trader-broker", "review")
review.t212, review.drift, review.holdings, review.holdings_t212 = t212, drift, H, HT
M = t212.messages()

D = datetime.date
DAY = D(2026, 11, 5)
CFG = {"paper": True, "cash_buffer_pct": 10, "allowed_tickers": ["TSLA", "AAPL", "MSFT"], "blocked_tickers": [],
       "risk_caps_pct": {"drift": 2, "news": 2, "intraday": 1}, "drift": {"buy_enabled": True, "max_share_pct": 50}}


def mismatch(kind, sym="MSFT"):
    return {"kind": kind, "symbol": sym, "ref": sym + "_US_EQ", "record": "r", "broker": "b", "what": H.KINDS[kind],
            "key": "%s:%s" % (kind, sym)}


class Pause(unittest.TestCase):
    """holdings.pause_update: a flag the buy steps read. It never sells, cancels or fixes."""

    def test_a_real_disagreement_starts_it_and_its_end_lifts_it(self):
        doc = TH.doc()
        self.assertEqual(H.pause_update(doc, []), (None, []))
        change, bad = H.pause_update(doc, [mismatch("no_record")])
        self.assertEqual((change, [m["kind"] for m in bad]), ("started", ["no_record"]))
        self.assertEqual(doc["pause"]["symbols"], ["MSFT"])
        since = doc["pause"]["since"]
        self.assertEqual(H.pause_update(doc, [mismatch("no_record"), mismatch("qty", "TSLA")])[0], "still")
        self.assertEqual((doc["pause"]["since"], doc["pause"]["symbols"]), (since, ["MSFT", "TSLA"]))
        self.assertEqual(H.pause_update(doc, [])[0], "ended")
        self.assertIsNone(doc["pause"])

    def test_every_spec_mismatch_pauses_a_software_stop_alone_does_not(self):
        for kind in ("no_record", "not_held", "qty", "no_stop", "part_stop", "stray_stop", "unknown_label", "no_sell_rule"):
            self.assertEqual(H.pause_update(TH.doc(), [mismatch(kind)])[0], "started", kind)
        self.assertEqual(H.pause_update(TH.doc(), [mismatch("soft_stop")]), (None, []))

    def test_switched_off_it_never_pauses_and_lifts_one_that_stands(self):
        doc = TH.doc()
        self.assertEqual(H.pause_update(doc, [mismatch("qty")], on=False), (None, []))
        H.pause_update(doc, [mismatch("qty")])
        self.assertEqual(H.pause_update(doc, [mismatch("qty")], on=False)[0], "ended")


class PauseFromTheMatch(TH.DailyMatch):
    def test_a_mismatch_pauses_buys_and_changes_nothing_at_the_broker(self):
        self.write("state/broker.json", {"orders": [record(strategy="news", sell_date="2026-12-01")]})
        self.account_is([held(7), held(2, "MSFT_US_EQ")], [STOP])        # MSFT: held, no record, no stop
        res = self.run_match()
        self.assertEqual(res["pause"], "started")
        self.assertEqual(sorted({m["symbol"] for m in res["pausing"]}), ["MSFT"])
        self.assertEqual(H.paused(self.dir)["symbols"], ["MSFT"])
        self.assertIn("MSFT", H.pause_words(H.paused(self.dir)))
        self.assertEqual([c[0] for c in self.router.calls if c[0] != "GET"], [])     # nothing sold, nothing cancelled
        pages = self.read("state/holdings.json")["pages"]
        self.assertEqual([p["symbol"] for p in pages], ["TSLA"])                     # no page was made up for MSFT
        self.assertEqual(self.run_match()["pause"], "still")
        self.assertIn("NEW BUYS ARE PAUSED", HT.text(res))
        # you sell MSFT in the app: the next match lifts the pause by itself
        self.router.routes.clear()
        self.account_is([held(7)], [STOP])
        res = self.run_match()
        self.assertEqual((res["pause"], res["pausing"]), ("ended", []))
        self.assertIsNone(H.paused(self.dir))
        with open(os.path.join(self.dir, "state", "holdings-log.jsonl")) as fh:
            events = [json.loads(x)["event"] for x in fh]
        self.assertEqual([e for e in events if e.startswith("buys_")], ["buys_paused", "buys_resumed"])

    def test_with_the_setting_off_it_reports_and_does_not_pause(self):
        self.write("state/broker.json", {"orders": []})
        self.account_is([held(2, "MSFT_US_EQ")], [])
        res = HT.daily_match(self.client, self.dir, RATE, TODAY, report, pause=False)
        self.assertTrue(res["mismatches"])
        self.assertIsNone(res["pause"])
        self.assertIsNone(H.paused(self.dir))


class DriftWaits(test_drift_buy.DriftBase):
    def test_a_drift_buy_is_skipped_with_the_reason_while_paused(self):
        why = H.pause_words({"symbols": ["MSFT"]})
        res = test_drift_buy.db.buy_today(self.client, self.dir, self.cfg, test_drift_buy.NY_0933, RATE, lambda s: 366.0,
                                          why, self.said.append, self.pushed_db.append)
        self.assertEqual((res["status"], res["bought"]), ("paused", []))
        self.assertIn("do not match (MSFT)", res["skipped"][0][1])
        self.assertEqual(self.router.calls, [])


class AutopilotPauses(test_autopilot.PilotBase):
    def setUp(self):
        super().setUp()
        self.write("state/broker.json", {"orders": []})

    def account_is(self, positions, orders):
        r = self.router
        r.routes.clear()
        r.add("GET", "/equity/positions", FakeResp(positions))
        r.add("GET", "/equity/orders", FakeResp(orders))
        r.add("GET", "/equity/account/summary", FakeResp(SUMMARY))
        r.add("GET", "/equity/history/orders", FakeResp({"items": []}))

    def test_one_message_no_scan_no_order_and_buying_comes_back_by_itself(self):
        p = self.pilot()
        self.account_is([held(2, "MSFT_US_EQ")], [])
        p.holdings_match()
        self.assertEqual(len(self.phone.sent), 1)                        # ONE message: what, and that buys are paused
        text = self.phone.sent[0][0]
        self.assertIn("don't match", text)
        self.assertIn("paused new buys", text)
        self.assertIn("haven't sold or changed anything", text)
        p._pause_checked = self.clock[0]
        line = p.cycle()
        self.assertIn("new buys are paused", line)
        self.assertIn("no scan", line)
        self.assertEqual(self.pipe.calls, [])                            # the scanner was not even started
        p.holdings_match()
        self.assertEqual(len(self.phone.sent), 1)                        # not told again while it stands
        self.assertEqual([c[0] for c in self.router.calls if c[0] != "GET"], [])
        # fixed in the app; ten minutes later the autopilot looks again before the next scan
        self.account_is([], [])
        self.clock[0] += 601
        self.assertIsNone(p.hold_pause())
        self.assertEqual(len(self.phone.sent), 2)
        self.assertIn("match again", self.phone.sent[1][0])

    def test_the_drift_and_news_steps_get_the_reason(self):
        p = self.pilot()
        self.account_is([held(2, "MSFT_US_EQ")], [])
        p.holdings_match()
        p._pause_checked = self.clock[0]
        self.assertIn("MSFT", p.hold_pause())

    def test_switched_off_in_the_config_nothing_pauses(self):
        self.write("config.json", dict(self.cfg, holdings={"pause_on_mismatch": False}))
        p = self.pilot()
        self.account_is([held(2, "MSFT_US_EQ")], [])
        p.holdings_match()
        self.assertIsNone(p.hold_pause())
        self.assertNotIn("paused new buys", self.phone.sent[0][0])


class Review(unittest.TestCase):
    """review.build from a daily-match result. Pure: it reads, and says what it would do."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        os.makedirs(os.path.join(self.dir, "state"))
        M.use(None)

    def tearDown(self):
        self.tmp.cleanup()

    def res(self, v, mismatches=(), pausing=()):
        doc = H.load(self.dir)
        H.sync(doc, v, TODAY)
        H.save(self.dir, doc)
        return {"view": v, "mismatches": list(mismatches), "pausing": list(pausing), "risk": H.open_risk(v), "new": [],
                "events": [], "pause": None}

    def build(self, v, cfg=None, **kw):
        res = kw.pop("res", None) or self.res(v, kw.pop("mismatches", ()), kw.pop("pausing", ()))
        return review.build(self.dir, cfg or CFG, res, kw.pop("today", DAY), **kw)

    def healthy(self, **over):
        return dict(dict(view([pos()], [stop()], [buy(reason="Robotaxi launch")]), cash_gbp=2500.0), **over)

    def fresh(self):
        os.remove(H.path(self.dir))

    def test_a_healthy_holding(self):
        r = self.build(self.healthy())
        self.assertTrue(r["ok"])
        self.assertEqual(r["problems"], [])
        row = r["rows"][0]
        self.assertEqual((row["symbol"], row["strategy"], row["stop"], row["sell_date"]), ("TSLA", "news", 358.32, "2026-12-01"))
        want = round(7 * (366.0 * (1 - 0.0015) - 364.0 * (1 + 0.0015)) / RATE, 2)
        self.assertEqual(row["gain_gbp"], want)                          # pounds, after the fee on the buy and the sale
        self.assertTrue(all(x["ok"] for x in r["limits"]))
        self.assertEqual(sorted(x["name"] for x in r["limits"]), ["cash", "drift_share", "risk_drift", "risk_intraday", "risk_news"])
        self.assertTrue(r["report_only"])
        self.assertTrue(review.worth_sending(r, self.dir))
        text = M.daily_review(r)
        self.assertIn("Tesla", text) if "Tesla" in text else self.assertIn("TSLA", text)
        self.assertIn("My records match Trading 212", text)
        self.assertIn("limits are all met", text)
        self.assertIn("sells nothing", text)
        for jargon in ("SHADOW", "_US_EQ", "risk_caps_pct", " R ", "at market", "stop order"):
            self.assertNotIn(jargon, text)
        self.assertIn("report only", review.text(r))

    def test_a_missing_stop_is_the_first_line(self):
        v = dict(view([pos(), pos("MSFT", 2, 400.0, 401.0)], [stop()], [buy()]), cash_gbp=2500.0)
        r = self.build(v, mismatches=[mismatch("no_stop"), mismatch("no_record")], pausing=[mismatch("no_record")])
        self.assertEqual(r["no_stop"], ["MSFT"])
        self.assertEqual(r["problems"][0], "no_stop")
        lines = M.daily_review(r).split("\n")
        self.assertIn("No full safety net", lines[1])                    # straight after the heading
        self.assertIn("MSFT", lines[1])
        self.assertIn("NO STOP", review.text(r).split("\n")[1])
        self.assertIn("New buys are paused", M.daily_review(r))
        self.assertTrue(r["paused"])
        self.assertEqual([x["strategy"] for x in r["rows"]], ["news", "no record"])

    def test_the_money_limits(self):
        r = self.build(self.healthy(cash_gbp=120.0))                     # under the 10% (GBP 500) always kept
        cash = [x for x in r["limits"] if x["name"] == "cash"][0]
        self.assertEqual((cash["ok"], cash["limit_gbp"]), (False, 500.0))
        self.assertIn("limits", r["problems"])
        self.assertIn("Nothing is sold to fix it", M.daily_review(r))
        tight = dict(CFG, risk_caps_pct={"drift": 2, "news": 0.5, "intraday": 1})        # GBP 25 for news
        r = self.build(self.healthy(), cfg=tight)
        news_cap = [x for x in r["limits"] if x["name"] == "risk_news"][0]
        self.assertFalse(news_cap["ok"])
        self.assertIn("News ideas could lose", M.daily_review(r))
        self.fresh()
        v = dict(view([pos(qty=20)], [stop(qty=20)], [buy(strategy="drift", qty=20)]), cash_gbp=2500.0)
        r = self.build(v)                                                # drift holds GBP 5,832 of a GBP 2,250 share
        self.assertFalse([x for x in r["limits"] if x["name"] == "drift_share"][0]["ok"])

    def test_one_cap_for_everything_when_there_is_no_split(self):
        cfg = {k: v for k, v in CFG.items() if k != "risk_caps_pct"}
        r = self.build(self.healthy(), cfg=dict(cfg, max_total_risk_pct=4))
        self.assertIn("risk_total", [x["name"] for x in r["limits"]])

    def test_a_rule_sale_due_overdue_or_approved_is_said_and_never_made(self):
        r = self.build(dict(view([pos()], [stop()], [buy(sell_date="2026-11-06")]), cash_gbp=2500.0))
        self.assertEqual((r["due"], r["overdue"]), (["TSLA"], []))       # tomorrow is its sell date
        self.assertIn("Sell date reached", M.daily_review(r))
        self.fresh()
        r = self.build(dict(view([pos()], [stop()], [buy(sell_date="2026-11-05")]), cash_gbp=2500.0))
        self.assertEqual((r["due"], r["overdue"]), ([], ["TSLA"]))       # its date has come and it is still held
        self.assertIn("overdue", r["problems"])
        self.assertIn("PAST ITS SELL DATE", review.text(r))
        doc = H.load(self.dir)
        doc["pages"][0]["status"] = "selling"
        H.save(self.dir, doc)
        v = dict(view([pos()], [stop()], [buy(sell_date="2026-11-05")]), cash_gbp=2500.0)
        res = {"view": v, "mismatches": [], "pausing": [], "risk": H.open_risk(v)}
        r = review.build(self.dir, CFG, res, DAY)
        self.assertEqual((r["approved"], r["overdue"]), (["TSLA"], []))

    def test_a_reason_that_looks_broken_is_something_it_would_ask_about(self):
        gone = dict(CFG, allowed_tickers=["AAPL"])
        r = self.build(self.healthy(), cfg=gone)
        self.assertEqual([(w["symbol"], w["asked"]) for w in r["would_ask"]], [("TSLA", False)])
        self.assertIn("no longer in the list", r["would_ask"][0]["why"])
        self.assertIn("I'd ask you about selling it", M.daily_review(r))
        self.assertIn("WOULD ASK", review.text(r))
        r = self.build(self.healthy(), cfg=dict(CFG, blocked_tickers=["TSLA"]))
        self.assertIn("blocked list", r["would_ask"][0]["why"])
        page = H.load(self.dir)["pages"][0]["id"]
        asks = [{"page": page, "status": "pending", "why": "The launch was delayed."}]
        r = self.build(self.healthy(), asks=asks)
        self.assertEqual([(w["why"], w["asked"]) for w in r["would_ask"]], [("The launch was delayed.", True)])
        self.assertFalse(os.path.exists(os.path.join(self.dir, "state", "news", "asks.json")))    # it made no request
        self.assertEqual(self.build(self.healthy())["would_ask"], [])

    def test_a_day_trade_left_over_is_shown_but_never_asked_about(self):
        r = self.build(dict(view([pos()], [stop()], [buy(strategy="intraday", sell_date=None)]), cash_gbp=2500.0),
                       cfg=dict(CFG, allowed_tickers=["AAPL"]))
        self.assertEqual((r["rows"][0]["strategy"], r["would_ask"], r["due"]), ("intraday", [], []))

    def test_the_market_is_beside_it_for_information(self):
        spy = {D(2026, 11, 5): (500.0, 0, 0, 510.0, 1.0)}
        r = self.build(self.healthy(), bars_fn=lambda sym, a, b: spy)
        self.assertEqual(r["rows"][0]["market_pct"], 2.0)
        self.assertIn("S&amp;P 500", M.daily_review(r))

        def broken(sym, a, b):
            raise OSError("Yahoo did not answer")
        self.assertIsNone(self.build(self.healthy(), bars_fn=broken)["rows"][0]["market_pct"])

    def test_could_not_check_today(self):
        r = review.build(self.dir, CFG, None, DAY)
        self.assertFalse(r["ok"])
        self.assertIn("could not check today", review.text(r))
        self.assertFalse(review.worth_sending(r, self.dir))              # nothing on record: no message for a blip
        self.build(self.healthy())
        self.assertTrue(review.worth_sending(r, self.dir))               # something should be held: you are told
        self.assertIn("couldn't check today", M.daily_review(r))

    def test_nothing_held_and_nothing_wrong_sends_nothing(self):
        r = self.build(dict(view(), cash_gbp=5000.0))
        self.assertTrue(r["ok"])
        self.assertFalse(review.worth_sending(r, self.dir))
        self.assertIn("nothing is held", review.text(r))

    def test_run_saves_logs_and_sends_once(self):
        said, sent = [], []
        res = self.res(self.healthy())
        r = review.run(self.dir, CFG, res, DAY, say=said.append, push=sent.append)
        self.assertEqual((len(said), len(sent)), (1, 1))
        with open(review.path(self.dir, "2026-11-05")) as fh:
            self.assertEqual(json.load(fh)["rows"][0]["symbol"], "TSLA")
        self.assertTrue(r["ok"])
        sent[:] = []
        review.run(self.dir, CFG, self.res(dict(view(), cash_gbp=5000.0)), DAY, say=said.append, push=sent.append)
        self.assertEqual(sent, [])


class ReviewIsReportOnly(TH.DailyMatch):
    def test_it_sends_no_order_and_changes_no_page(self):
        self.write("state/broker.json", {"orders": [record(strategy="news", sell_date="2026-11-05")]})
        self.account_is([held(7)], [STOP])
        res = self.run_match()
        before = self.read("state/holdings.json")
        sent = []
        cfg = dict(CFG, blocked_tickers=["TSLA"])                        # overdue AND a reason that looks broken
        r = review.run(self.dir, cfg, res, DAY, say=lambda t: None, push=sent.append)
        self.assertEqual((r["overdue"], [w["symbol"] for w in r["would_ask"]]), (["TSLA"], ["TSLA"]))
        self.assertEqual([c[0] for c in self.router.calls if c[0] != "GET"], [])     # not one order, cancel or change
        self.assertEqual(self.read("state/holdings.json"), before)                   # no page touched
        self.assertFalse(os.path.exists(os.path.join(self.dir, "state", "news", "asks.json")))
        self.assertEqual(len(sent), 1)

    def test_the_source_has_no_way_to_write_to_the_broker(self):
        with open(os.path.join(os.path.dirname(__file__), "..", "trader-broker", "review.py")) as fh:
            src = fh.read()
        for word in ("cmd_place", ".post(", ".delete(", "guard_market", "eod_close(", "make_asks", "H.save(", "cancel"):
            self.assertNotIn(word, src.replace("never places, cancels or changes", ""), word)


class AutopilotReview(test_autopilot.PilotBase):
    def test_after_the_close_the_review_goes_to_the_phone(self):
        self.write("state/broker.json", {"orders": [record(strategy="news", sell_date="2026-12-01", date=test_autopilot.TODAY)]})
        r = self.router
        r.add("GET", "/equity/positions", FakeResp([dict(held(7), averagePricePaid=364.0)]))
        r.add("GET", "/equity/orders", FakeResp([STOP]))
        r.add("GET", "/equity/account/summary", FakeResp(SUMMARY))
        p = self.pilot()
        test_autopilot.ap.mod("trader-broker", "drift").daily_bars = lambda sym, a, b: {}
        p.daily_review(p.holdings_match())
        texts = [t for t, _ in self.phone.sent]
        self.assertEqual(len([t for t in texts if "Daily review" in t]), 1)
        self.assertIn("sells nothing", texts[-1])
        self.assertEqual([c[0] for c in r.calls if c[0] != "GET"], [])
        p.daily_review(None)                                             # the account could not be read
        self.assertIn("couldn't check today", self.phone.sent[-1][0])


if __name__ == "__main__":
    unittest.main()
