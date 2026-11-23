"""board.py: the evening strategy board (report only). Built from fake saved state; the phone is a fake;
never the network, never an order."""

import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout

from _load import load, ROOT
import test_autopilot
from test_eod_close import STOP, held, record
from test_t212_place import FakeResp, SUMMARY

board = load("trader-broker", "board")
phone = load("trader-broker", "phone")

DAY = "2026-11-20"                      # a Friday; 14:35 UTC is 14:35 in London in November
CFG = {"paper": True, "cash_buffer_pct": 10, "autopilot": {"intraday_enabled": False}}
TOKEN = "123456:TEST-token-not-real"


def ts(hm, day=DAY):
    import datetime
    return datetime.datetime.fromisoformat("%sT%s:00+00:00" % (day, hm)).timestamp()


def page(pid, sym, strategy, status, buy_date, reason="a reason", result=None, sell_date="2026-12-18", ref=None):
    return {"id": pid, "symbol": sym, "strategy": strategy, "status": status, "buy_date": buy_date, "reason": reason,
            "result": result, "sell_rule": {"date": sell_date, "rule": "r"}, "broker": "trading212", "account": "demo",
            "broker_ref": {"ref": ref or sym + "_US_EQ"}}


class Base(unittest.TestCase):
    def setUp(self):
        self.sd = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.sd)

    def write(self, rel, doc):
        p = os.path.join(self.sd, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            if rel.endswith(".jsonl"):
                fh.write("".join(json.dumps(r) + "\n" for r in doc))
            else:
                json.dump(doc, fh)

    def full_day(self):
        """A day with something in every part of the board."""
        self.write("universe/tradable.json", {"tickers": [{"symbol": "P", "name": "Everpure Inc."},
                                                          {"symbol": "BDX", "name": "Becton Dickinson"},
                                                          {"symbol": "VRTX", "name": "Vertex Pharmaceuticals"},
                                                          {"symbol": "NKE", "name": "Nike"}]})
        self.write("state/holdings.json", {"pages": [
            page("d1", "P", "drift", "open", DAY, reason="Beat its earnings forecast"),
            page("d0", "AAA", "drift", "closed", "2026-10-01", result={"date": "2026-10-29", "pl_gbp": 8.0, "why": "sold at the close"}),
            page("n1", "NKE", "news", "closed", "2026-11-18", result={"date": DAY, "pl_gbp": -12.5, "why": "stop"}),
            page("n0", "BBB", "news", "closed", "2026-11-17", result={"date": "2026-11-19", "pl_gbp": 4.0, "why": "sold at the close"}),
            page("i0", "CCC", "intraday", "closed", "2026-11-16", result={"date": "2026-11-16", "pl_gbp": -9.0, "why": "stop"}),
        ]})
        self.write("state/drift/trades.jsonl", [{"ticker": "AAA_US_EQ", "buy_day": "2026-10-01", "cost_gbp": 200.0, "spy_pct": 2.5}])
        self.write("state/drift/buys.jsonl", [{"day": DAY, "symbol": "P", "status": "bought", "at": ts("14:33")}])
        self.write("state/drift/shortlist.json", {"scan_day": DAY, "generated_utc": "%sT20:35:00Z" % DAY,
                                                  "shortlist": [{"symbol": "INTU"}]})
        self.write("state/news/runs.jsonl", [
            {"day": DAY, "kind": "main", "status": "ok", "ideas": 2, "at": ts("06:02")},
            {"day": DAY, "kind": "update", "status": "ok", "ideas": 0, "dropped": 1, "at": ts("13:30")},
            {"day": "2026-11-19", "kind": "main", "status": "ok", "ideas": 4, "at": ts("06:00", "2026-11-19")}])
        self.write("state/news/ideas.jsonl", [{"day": DAY, "symbol": "BDX", "from": "main"},
                                              {"day": DAY, "symbol": "VRTX", "from": "main"}])
        self.write("state/news/buys.jsonl", [
            {"day": DAY, "symbol": "VRTX", "status": "skipped", "why": "its confidence is low: only medium and high", "at": ts("14:35")},
            {"day": DAY, "symbol": "BDX", "status": "skipped", "why": "its confidence is low: only medium and high", "at": ts("14:35")}])
        self.write("state/fills.json", {"rows": {"n1:sell": {"fill_at": "%sT17:10:00Z" % DAY}}})
        self.write("state/reviews/%s.json" % DAY, {"day": DAY, "ok": True, "account_gbp": 4769.89,
            "limits": [{"name": "cash", "now_gbp": 4626.71, "limit_gbp": 476.99, "ok": True}],
            "rows": [{"symbol": "PSTG", "ref": "P_US_EQ", "strategy": "drift", "buy_date": DAY, "sell_date": "2026-12-18",
                      "gain_gbp": -16.77, "reason": "Beat its earnings forecast", "status": "open"}]})
        self.write("state/account-days.json", {"days": {DAY: {"start": 4786.17, "close": 4769.94}}})
        self.write("state/market-day.json", {"day": DAY, "spy_pct": -0.84})

    def card(self, b, s):
        return [c for c in b["cards"] if c["strategy"] == s][0]


class Build(Base):
    def test_each_card_has_todays_real_events_in_uk_time_in_order(self):
        self.full_day()
        b = board.build(self.sd, CFG, DAY)
        news = [(e["time"], e["text"]) for e in self.card(b, "news")["events"]]
        self.assertEqual([t for t, _ in news], ["06:02", "13:30", "14:35", "17:10", "21:00"])
        self.assertIn("morning news run: 2 ideas (Becton Dickinson (BDX), Vertex Pharmaceuticals (VRTX))", news[0][1])
        self.assertEqual(news[1][1], "pre-open check: 1 dropped")
        self.assertIn("not bought: Vertex Pharmaceuticals (VRTX), Becton Dickinson (BDX) (its confidence is low)", news[2][1])
        self.assertEqual(news[3][1], "sold Nike (NKE) (hit its safety net)")
        self.assertEqual(self.card(b, "news")["events"][3]["pl"], -12.5)
        self.assertEqual(news[4][1], "market closed: nothing held")
        drift = [(e["time"], e["text"]) for e in self.card(b, "drift")["events"]]
        self.assertEqual(drift[0], ("14:33", "bought Everpure (P): Beat its earnings forecast"))
        self.assertIn("earnings check: INTU to buy next trading day", drift[1][1])
        self.assertEqual(drift[-1], ("21:00", "market closed: holding Everpure (P)"))

    def test_what_is_held_now_with_the_gain_after_fees(self):
        self.full_day()
        h = self.card(board.build(self.sd, CFG, DAY), "drift")["holding"]
        self.assertEqual(len(h), 1)
        self.assertEqual((h[0]["symbol"], h[0]["days"], h[0]["sell_date"], h[0]["gain_gbp"]), ("P", 1, "2026-12-18", -16.77))
        self.assertEqual(h[0]["reason"], "Beat its earnings forecast")

    def test_progress_since_the_start_with_the_sp500_where_recorded(self):
        self.full_day()
        b = board.build(self.sd, CFG, DAY)
        d, n = self.card(b, "drift")["progress"], self.card(b, "news")["progress"]
        self.assertEqual((d["trades"], d["wins"], d["losses"], d["total_gbp"], d["spy_gbp"], d["since"]),
                         (1, 1, 0, 8.0, 5.0, "2026-10-01"))
        self.assertEqual((n["trades"], n["wins"], n["losses"], n["total_gbp"], n["spy_gbp"]), (2, 1, 1, -8.5, None))

    def test_the_top_of_the_page(self):
        self.full_day()
        t = board.build(self.sd, CFG, DAY)["top"]
        self.assertEqual((t["account_gbp"], t["cash_gbp"], t["buffer_gbp"], t["buffer_pct"], t["market_pct"], t["day_change_gbp"]),
                         (4769.89, 4626.71, 476.99, 10, -0.84, -16.23))
        page_ = board.render(board.build(self.sd, CFG, DAY))
        self.assertIn("fell 0.8% today", page_)
        self.assertIn("Kept aside (10% rule)", page_)

    def test_a_quiet_day_says_nothing_happened(self):
        b = board.build(self.sd, CFG, DAY)
        for c in b["cards"]:
            self.assertEqual(c["events"], [])
        page_ = board.render(b)
        self.assertEqual(page_.count("Nothing happened today."), 2)
        self.assertIn("market move was not recorded", page_)
        self.assertIn("could not be read this evening", page_)

    def test_day_trading_is_one_off_line_unless_it_is_switched_on(self):
        self.full_day()
        b = board.build(self.sd, CFG, DAY)
        self.assertEqual([c["strategy"] for c in b["cards"]], ["drift", "news"])
        self.assertIn("Switched off", board.render(b))
        on = board.build(self.sd, dict(CFG, autopilot={"intraday_enabled": True}), DAY)
        self.assertEqual([c["strategy"] for c in on["cards"]], ["drift", "news", "intraday"])
        self.assertNotIn("Switched off", board.render(on))

    def test_without_a_review_holdings_come_from_the_record_with_no_gain(self):
        self.full_day()
        os.remove(os.path.join(self.sd, "state", "reviews", "%s.json" % DAY))
        h = self.card(board.build(self.sd, CFG, DAY), "drift")["holding"]
        self.assertEqual([(x["symbol"], x["gain_gbp"]) for x in h], [("P", None)])


class Clock(unittest.TestCase):
    def test_uk_time_in_winter_and_in_summer(self):
        self.assertEqual(board.uk_hm("2026-11-20T14:35:00Z")[0], "14:35")
        self.assertEqual(board.uk_hm("2026-06-15T13:30:00Z")[0], "14:30")
        self.assertEqual(board.uk_hm("2026-03-29T00:59:00Z")[0], "00:59")     # BST starts 01:00 UTC on 29 Mar 2026
        self.assertEqual(board.uk_hm("2026-03-29T01:00:00Z")[0], "02:00")
        import datetime
        self.assertEqual(board.close_uk(datetime.date(2026, 11, 20))[0], "21:00")
        self.assertEqual(board.close_uk(datetime.date(2026, 6, 15))[0], "21:00")
        self.assertEqual(board.close_uk(datetime.date(2026, 11, 27))[0], "18:00")    # 1:00 p.m. early close
        self.assertIsNone(board.close_uk(datetime.date(2026, 11, 26)))                # Thanksgiving


class Page(Base):
    def test_one_self_contained_page_light_and_dark(self):
        self.full_day()
        p = board.write(self.sd, CFG, DAY)
        self.assertEqual(p, os.path.join(self.sd, "state", "boards", "%s.html" % DAY))
        with open(p, encoding="utf-8") as fh:
            s = fh.read()
        for bad in ("<script", "http://", "https://", "src=", "<link", "@import", "url("):
            self.assertNotIn(bad, s.lower(), bad)
        self.assertIn("prefers-color-scheme:dark", s)
        self.assertIn('name="viewport"', s)
        self.assertNotIn("\u2014", s)

    def test_colour_only_on_pound_results(self):
        self.full_day()
        s = board.render(board.build(self.sd, CFG, DAY))
        import re
        for span in re.findall(r'<span class="(?:up|down)">([^<]*)</span>', s):
            self.assertIn("£", span)
        self.assertIn('<span class="down">−£16.77</span>', s)
        self.assertIn('<span class="up">+£8.00</span>', s)

    def test_outside_text_is_escaped(self):
        self.full_day()
        self.write("state/holdings.json", {"pages": [page("d1", "P", "drift", "open", DAY, reason="<b>bold</b> & co")]})
        self.write("state/reviews/%s.json" % DAY, {"day": DAY, "ok": False})
        s = board.render(board.build(self.sd, CFG, DAY))
        self.assertNotIn("<b>bold</b>", s)
        self.assertIn("&lt;b&gt;bold&lt;/b&gt; &amp; co", s)

    def test_by_hand_it_writes_the_file_and_prints_the_path_sending_nothing(self):
        self.full_day()
        sent = []
        real = phone._open
        phone._open = lambda req, timeout: sent.append(req)
        try:
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(board.main([DAY, "--state-dir", self.sd], environ={}), 0)
        finally:
            phone._open = real
        self.assertEqual(out.getvalue().strip(), board.path(self.sd, DAY))
        self.assertTrue(os.path.exists(board.path(self.sd, DAY)))
        self.assertEqual(sent, [])


class FakeDocPhone(object):
    def __init__(self, fail=False):
        self.docs, self.fail = [], fail

    def scrub(self, t):
        return str(t)

    def send_document(self, path, caption=""):
        if self.fail:
            raise phone.PhoneError("Telegram sendDocument failed: boom")
        self.docs.append((path, caption))


class Run(Base):
    def test_built_saved_and_sent_with_a_short_caption(self):
        self.full_day()
        ph, pushed, said = FakeDocPhone(), [], []
        p = board.run(self.sd, CFG, DAY, phone=ph, say=said.append, push=pushed.append)
        self.assertEqual(ph.docs, [(p, board.M().board_caption())])
        self.assertIn("tap to open", ph.docs[0][1])
        self.assertEqual(pushed, [])

    def test_a_failed_send_is_one_short_message_and_never_raises(self):
        self.full_day()
        pushed = []
        board.run(self.sd, CFG, DAY, phone=FakeDocPhone(fail=True), say=lambda m: None, push=pushed.append)
        self.assertEqual(len(pushed), 1)
        self.assertIn("couldn't make today's strategy board", pushed[0])

    def test_a_failed_build_is_one_short_message_and_never_raises(self):
        pushed = []
        real = board.render
        board.render = lambda b: 1 / 0
        try:
            self.assertIsNone(board.run(self.sd, CFG, DAY, phone=FakeDocPhone(), say=lambda m: None, push=pushed.append))
        finally:
            board.render = real
        self.assertEqual(len(pushed), 1)

    def test_the_source_has_no_way_to_write_to_the_broker(self):
        with open(os.path.join(ROOT, "trader-broker", "board.py")) as fh:
            src = fh.read().replace("never places, cancels or changes", "")
        for word in ("cmd_place", ".post(", ".delete(", "guard_market", "eod_close(", "make_asks", "H.save(",
                     "cancel", "Client(", "urlopen", "daily_bars", "fetch"):
            self.assertNotIn(word, src, word)


class SendDocument(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.reqs = []
        self.real = phone._open
        phone._open = self.fake

    def tearDown(self):
        phone._open = self.real
        shutil.rmtree(self.dir)

    def fake(self, req, timeout):
        self.reqs.append(req)
        if getattr(self, "error", None):
            raise self.error
        body = json.dumps({"ok": True, "result": {"message_id": 9}}).encode()

        class R(object):
            def read(self):
                return body

            def close(self):
                pass
        return R()

    def paired(self):
        p = phone.Phone(TOKEN, self.dir)
        p.state.update(chat_id=111, user_id=111)
        return p

    def test_a_multipart_upload_of_the_file(self):
        f = os.path.join(self.dir, "2026-11-20.html")
        with open(f, "w", encoding="utf-8") as fh:
            fh.write("<html>board £</html>")
        self.assertEqual(self.paired().send_document(f, "caption here"), 9)
        req = self.reqs[-1]
        self.assertTrue(req.full_url.endswith("/sendDocument"))
        self.assertTrue(req.get_header("Content-type").startswith("multipart/form-data; boundary="))
        body = req.data
        self.assertIn(b'name="chat_id"\r\n\r\n111\r\n', body)
        self.assertIn(b'name="caption"\r\n\r\ncaption here\r\n', body)
        self.assertIn(b'name="document"; filename="2026-11-20.html"', body)
        self.assertIn("<html>board £</html>".encode("utf-8"), body)

    def test_the_token_never_appears_in_an_error(self):
        f = os.path.join(self.dir, "b.html")
        with open(f, "w") as fh:
            fh.write("x")
        self.error = urllib.error.URLError("could not reach https://api.telegram.org/bot%s/sendDocument" % TOKEN)
        with self.assertRaises(phone.PhoneError) as cm:
            self.paired().send_document(f)
        self.assertNotIn(TOKEN, str(cm.exception))
        self.assertIn("<token>", str(cm.exception))

    def test_not_paired_is_refused(self):
        with self.assertRaises(phone.PhoneError):
            phone.Phone(TOKEN, self.dir).send_document(os.path.join(self.dir, "none.html"))
        self.assertEqual(self.reqs, [])


class AutopilotBoard(test_autopilot.PilotBase):
    def ready(self):
        self.write("state/broker.json", {"orders": [record(strategy="news", sell_date="2026-12-01", date=test_autopilot.TODAY)]})
        r = self.router
        r.add("GET", "/equity/positions", FakeResp([dict(held(7), averagePricePaid=364.0)]))
        r.add("GET", "/equity/orders", FakeResp([STOP]))
        r.add("GET", "/equity/account/summary", FakeResp(SUMMARY))
        test_autopilot.ap.mod("trader-broker", "drift").daily_bars = lambda sym, a, b: {}
        return self.pilot()

    def test_the_board_follows_the_review_and_changes_nothing(self):
        p = self.ready()
        p.daily_review(p.holdings_match())
        texts = [t for t, _ in self.phone.sent]
        self.assertEqual(len([t for t in texts if "Daily review" in t]), 1)
        self.assertEqual(len(self.phone.docs), 1)
        self.assertTrue(self.phone.docs[0][0].endswith(".html"))
        self.assertTrue(os.path.exists(self.phone.docs[0][0]))
        self.assertEqual([c[0] for c in self.router.calls if c[0] != "GET"], [])

    def test_a_board_that_cannot_be_sent_never_stops_the_review(self):
        p = self.ready()

        def boom(path, caption=""):
            raise phone.PhoneError("no")
        self.phone.send_document = boom
        p.daily_review(p.holdings_match())
        texts = [t for t, _ in self.phone.sent]
        self.assertEqual(len([t for t in texts if "Daily review" in t]), 1)
        self.assertEqual(len([t for t in texts if "couldn't make today's strategy board" in t]), 1)


if __name__ == "__main__":
    unittest.main()
