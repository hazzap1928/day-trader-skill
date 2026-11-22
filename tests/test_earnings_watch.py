"""Next results date during a hold (trader-broker/earnings_watch.py). REPORT ONLY. Fake Nasdaq answers, no network."""

import datetime
import json
import os
import re
import tempfile
import unittest

from _load import load, ROOT

ew = load("trader-broker", "earnings_watch")
M = load("trader-broker", "messages")
D = datetime.date


def answer(date=None, sure="expected", when="after market close"):
    if not date:
        return {"data": {"reportText": "Our vendor, Zacks Investment Research, hasn't provided us with the upcoming "
                                       "earnings report date.", "announcement": "Earnings announcement* for X: "}}
    m, d, y = date.month, date.day, date.year
    verb = "is expected* to" if sure == "expected" else "is estimated to"
    return {"data": {"reportText": "Acme Common Stock %s report earnings on  %02d/%02d/%d %s.  The report will be for "
                                   "the fiscal Quarter ending Nov %d." % (verb, m, d, y, when, y),
                     "announcement": "Earnings announcement* for X: %s" % date.strftime("%b %d, %Y")}}


class Fake(object):
    def __init__(self, by_sym):
        self.by_sym, self.calls = by_sym, []

    def __call__(self, url):
        sym = re.search(r"analyst/([^/]+)/", url).group(1)
        self.calls.append(sym)
        a = self.by_sym[sym]
        if isinstance(a, Exception):
            raise a
        return json.dumps(a).encode()


class Parse(unittest.TestCase):
    def test_expected_estimated_and_none(self):
        e = ew.parse(answer(D(2026, 12, 9)), "AVGO")
        self.assertEqual((e["date"], e["sure"], e["when"], e["quarter"]), ("2026-12-09", "expected", "after the close", "Nov 2026"))
        e = ew.parse(answer(D(2026, 12, 10), "estimated", "x"), "LULU")
        self.assertEqual((e["date"], e["sure"], e["when"]), ("2026-12-10", "estimated", "time not given"))
        e = ew.parse(answer(), "P")
        self.assertEqual((e["date"], e["why"]), (None, "the next results date is not announced yet"))
        self.assertEqual(ew.parse({"data": {"announcement": "Earnings announcement* for X: Jan 28, 2027"}}, "X")["date"],
                         "2027-01-28")

    def test_in_window(self):
        e = {"date": "2026-12-09"}
        self.assertTrue(ew.in_window(e, "2026-11-23", "2026-12-18"))
        self.assertTrue(ew.in_window(e, "2026-12-09", "2026-12-09"))
        self.assertFalse(ew.in_window(e, "2026-11-23", "2026-12-08"))
        self.assertFalse(ew.in_window({"date": None}, "2026-11-23", "2026-12-18"))


class Check(unittest.TestCase):
    def setUp(self):
        self.sd = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.sd, "state"))
        self.write([{"id": "a", "symbol": "AAA", "strategy": "news", "status": "open", "sell_rule": {"date": "2026-12-18"}},
                    {"id": "b", "symbol": "BBB", "strategy": "drift", "status": "open", "sell_rule": {"date": "2026-12-04"}},
                    {"id": "c", "symbol": "CCC", "strategy": "intraday", "status": "open"}])

    def write(self, pages):
        with open(os.path.join(self.sd, "state", "holdings.json"), "w") as fh:
            json.dump({"pages": pages}, fh)

    def test_off_by_default(self):
        f = Fake({})
        self.assertEqual(ew.run(self.sd, {}, D(2026, 11, 23), fetch=f), {"on": False, "by_page": {}, "errors": []})
        self.assertEqual(f.calls, [])

    def test_inside_the_hold_is_told_once_then_again_when_close_or_moved(self):
        f = Fake({"AAA": answer(D(2026, 12, 9)), "BBB": answer(D(2026, 12, 9))})
        cfg = {"earnings_watch": {"enabled": True}}
        got = ew.run(self.sd, cfg, D(2026, 11, 23), fetch=f)["by_page"]
        self.assertEqual(sorted(f.calls), ["AAA", "BBB"])            # day trades are not looked up
        self.assertEqual((got["a"]["in_hold"], got["a"]["tell"], got["a"]["days_to"]), (True, "new", 11))
        self.assertEqual((got["b"]["in_hold"], got["b"]["tell"]), (False, False))     # after its sell date
        self.assertFalse(ew.run(self.sd, cfg, D(2026, 11, 24), fetch=f)["by_page"]["a"]["tell"])
        self.assertEqual(ew.run(self.sd, cfg, D(2026, 12, 4), fetch=f)["by_page"]["a"]["tell"], "soon")   # 3 trading days
        self.assertFalse(ew.run(self.sd, cfg, D(2026, 12, 7), fetch=f)["by_page"]["a"]["tell"])
        f.by_sym["AAA"] = answer(D(2026, 12, 15))
        self.assertEqual(ew.run(self.sd, cfg, D(2026, 12, 8), fetch=f)["by_page"]["a"]["tell"], "new")    # it moved
        with open(os.path.join(self.sd, "state", "earnings-watch.jsonl")) as fh:
            self.assertEqual([json.loads(x)["date"] for x in fh], ["2026-12-09", "2026-12-09", "2026-12-15"])

    def test_a_failed_lookup_is_a_note(self):
        f = Fake({"AAA": OSError("timed out"), "BBB": answer()})
        out = ew.run(self.sd, {"earnings_watch": {"enabled": True}}, D(2026, 11, 23), fetch=f)
        self.assertEqual(list(out["by_page"]), ["b"])
        self.assertIn("AAA: timed out", out["errors"][0])

    def test_it_never_changes_the_holdings_record(self):
        p = os.path.join(self.sd, "state", "holdings.json")
        with open(p) as fh:
            before = fh.read()
        ew.run(self.sd, {"earnings_watch": {"enabled": True}}, D(2026, 11, 23),
               fetch=Fake({"AAA": answer(D(2026, 12, 9)), "BBB": answer()}))
        with open(p) as fh:
            self.assertEqual(fh.read(), before)

    def test_the_source_has_no_broker_or_order_code(self):
        with open(os.path.join(ROOT, "trader-broker", "earnings_watch.py")) as fh:
            src = fh.read()
        for rx in (r"t212", r"\bplace\(", r"\bcancel\w*\(", r"\bpush\(", r"holdings\.save|\.save\(sd, H"):
            self.assertIsNone(re.search(rx, src), rx)


class Words(unittest.TestCase):
    def test_phone_words(self):
        e = {"date": "2026-12-09", "in_hold": True, "tell": "new", "when": "after the close", "sure": "expected", "days_to": 11}
        self.assertEqual(M.earnings_words(e), ". \U0001f4c5 It reports results on Wed 9 Dec after the US market closes in 11 "
                                              "trading days (the company's date), before its sell date: the share can move a lot that day")
        self.assertIn("(an estimate from past dates)", M.earnings_words(dict(e, sure="estimated")))
        self.assertIn(" today", M.earnings_words(dict(e, days_to=0)))
        self.assertEqual(M.earnings_words(dict(e, tell=False)), "")
        self.assertEqual(M.earnings_words(dict(e, in_hold=False)), "")
        self.assertEqual(M.earnings_words(None), "")
        self.assertIn("INSIDE the hold", ew.words(dict(e, sell_date="2026-12-18")))


if __name__ == "__main__":
    unittest.main()
