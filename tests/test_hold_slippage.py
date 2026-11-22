"""Slippage on held trades (trader-broker/hold_slippage.py): price got against price expected. Offline, temp files."""

import json
import os
import re
import tempfile
import unittest

from _load import load, ROOT

hs = load("trader-broker", "hold_slippage")
M = load("trader-broker", "messages")


def page(pid, sym, strategy="drift", status="open", buy="2026-11-20", buy_price=102.0, stop=71.0, result=None):
    return {"id": pid, "symbol": sym, "strategy": strategy, "status": status, "buy_date": buy, "buy_price": buy_price,
            "stop": stop, "result": result, "broker_ref": {"ref": sym + "_US_EQ"}}


class Base(unittest.TestCase):
    def setUp(self):
        self.sd = tempfile.mkdtemp()
        for d in ("state/drift", "state/news"):
            os.makedirs(os.path.join(self.sd, d))

    def put(self, rel, doc):
        with open(os.path.join(self.sd, rel), "w") as fh:
            if rel.endswith(".jsonl"):
                fh.write("".join(json.dumps(r) + "\n" for r in doc))
            else:
                json.dump(doc, fh)


class Record(Base):
    def test_buys_and_sales_against_what_was_expected_once_each(self):
        self.put("state/holdings.json", {"pages": [
            page("d1", "P", buy_price=102.3677),
            page("n1", "AAA", "news", "closed", buy_price=50.0, stop=45.0,
                 result={"date": "2026-11-24", "price": 44.80, "why": "stop"}),
            page("n2", "BBB", "news", "closed", buy_price=20.0, result={"date": "2026-11-23", "price": 21.0, "why": "sold at the close"}),
            page("n3", "CCC", "news", "closed", buy_price=10.0, result={"date": "2026-11-23", "price": 9.9, "why": "sold in the Trading 212 app"}),
            page("w1", "DDD", status="waiting"), page("i1", "EEE", "intraday")]})
        self.put("state/drift/buys.jsonl", [{"symbol": "P", "day": "2026-11-20", "status": "bought", "price": 101.715},
                                            {"symbol": "ZZZ", "day": "2026-11-20", "status": "skipped", "price": 1}])
        self.put("state/news/buys.jsonl", [{"symbol": "AAA", "day": "2026-11-20", "status": "bought", "price": 49.90}])
        self.put("state/t212-guard.json", {"eod": {"date": "2026-11-23", "sold": [{"ticker": "BBB_US_EQ", "price_seen": 21.1}]}})
        new = hs.record(self.sd)
        got = {(r["page"], r["kind"]): r for r in new}
        self.assertEqual(sorted(got), [("d1", "buy"), ("n1", "buy"), ("n1", "sell"), ("n2", "buy"), ("n2", "sell"),
                                       ("n3", "buy"), ("n3", "sell")])
        self.assertAlmostEqual(got[("d1", "buy")]["slip_pct"], 100 * (102.3677 / 101.715 - 1), places=3)   # paid 0.64% more
        self.assertAlmostEqual(got[("n1", "sell")]["slip_pct"], 100 * (1 - 44.80 / 45.0), places=3)         # under the stop
        self.assertAlmostEqual(got[("n2", "sell")]["slip_pct"], 100 * (1 - 21.0 / 21.1), places=3)
        self.assertIsNone(got[("n3", "sell")]["slip_pct"])                                                 # sold by hand
        self.assertIsNone(got[("n2", "buy")]["slip_pct"])                                                  # no quote logged
        self.assertEqual(hs.record(self.sd), [])                                                           # once
        s = hs.summary(self.sd)
        self.assertEqual((s["buy"]["n"], s["buy"]["unmeasured"], s["sell"]["n"], s["stop"]["n"]), (2, 2, 2, 1))
        self.assertIn("## Slippage on held trades", "\n".join(hs.lines(s)))

    def test_nothing_yet_says_nothing(self):
        self.assertEqual(hs.record(self.sd), [])
        self.assertEqual(M.friday_slippage(hs.summary(self.sd)), [])

    def test_the_source_has_no_order_code_and_no_page_edits(self):
        with open(os.path.join(ROOT, "trader-broker", "hold_slippage.py")) as fh:
            src = fh.read()
        for rx in (r"\bplace\(", r"\bcancel\w*\(", r"\bpush\(", r"H\.save|holdings\.save"):
            self.assertIsNone(re.search(rx, src), rx)


class Words(unittest.TestCase):
    def test_the_friday_lines_carry_the_practice_account_note(self):
        s = {"assumed_pct": 0.05, "note": hs.NOTE, "buy": {"n": 1, "unmeasured": 0, "avg_pct": 0.642, "worst_pct": 0.642},
             "sell": {"n": 0, "unmeasured": 1, "avg_pct": None, "worst_pct": None}, "stop": {"n": 0, "unmeasured": 0}}
        out = M.join(M.friday_slippage(s))
        self.assertIn("Buys: 1 measured, on average 0.64% worse than the price just before the order (the plan assumes 0.05%)", out)
        self.assertIn("Sales: none measured yet; 1 with no price to compare", out)
        self.assertIn("Practice account fills are simulated by Trading 212, so they may understate real slippage", out)


if __name__ == "__main__":
    unittest.main()
