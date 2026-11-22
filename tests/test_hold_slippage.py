"""Slippage on held trades (hold_slippage.py), the entry timing shadow (entry_shadow.py) and Trading 212's fill
lookup (holdings_t212.fill_finder). Offline: temp files, fake minute bars, a fake Trading 212 history."""

import datetime
import json
import os
import re
import tempfile
import unittest

from _load import load, ROOT

hs = load("trader-broker", "hold_slippage")
es = load("trader-broker", "entry_shadow")
M = load("trader-broker", "messages")
HT = load("trader-broker", "holdings_t212")
HT.t212 = load("trader-broker", "t212")
DAY = datetime.date(2026, 11, 20)


def page(pid, sym, strategy="drift", status="open", buy="2026-11-20", buy_price=102.0, stop=71.0, result=None, qty=2.04):
    return {"id": pid, "symbol": sym, "strategy": strategy, "status": status, "buy_date": buy, "buy_price": buy_price,
            "stop": stop, "result": result, "qty": qty, "broker_ref": {"ref": sym + "_US_EQ"}}


def minutes(spec):
    """{"09:32": (o, h, l, c), ...} -> {NY minute: (o, h, l, c, v)} on DAY."""
    out = {}
    for t, bar in spec.items():
        h, m = [int(x) for x in t.split(":")]
        out[datetime.datetime.combine(DAY, datetime.time(h, m))] = tuple(bar) + (1000.0,)
    return out


# Everpure, 20 Nov 2026, the real 1-minute prices around the buy (Yahoo) and its real fill (Trading 212: 09:33:21, $102.37)
EVERPURE = minutes({"09:31": (104.05, 104.05, 100.51, 100.54), "09:32": (100.63, 102.29, 100.51, 101.71),
                    "09:33": (101.51, 101.68, 101.10, 101.36), "09:34": (101.36, 102.16, 100.86, 101.62),
                    "09:45": (99.20, 99.5, 98.9, 99.1), "10:00": (97.0, 97.2, 96.8, 97.1), "10:30": (95.0, 95.5, 94.8, 95.2),
                    "15:50": (92.10, 92.2, 92.0, 92.05)})


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

    def everpure(self):
        self.put("state/holdings.json", {"pages": [page("d1", "P", buy_price=102.3677)]})
        self.put("state/drift/buys.jsonl", [{"symbol": "P", "day": "2026-11-20", "status": "bought", "price": 101.715}])


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
        got = hs.record(self.sd, DAY + datetime.timedelta(days=4))
        rows = hs.load(self.sd)["rows"]
        self.assertEqual(sorted(got["new"]), ["d1:buy", "n1:buy", "n1:sell", "n2:buy", "n2:sell", "n3:buy", "n3:sell"])
        self.assertAlmostEqual(rows["d1:buy"]["slip_pct"], 100 * (102.3677 / 101.715 - 1), places=3)   # paid 0.64% more
        self.assertAlmostEqual(rows["n1:sell"]["slip_pct"], 100 * (1 - 44.80 / 45.0), places=3)         # under the stop
        self.assertAlmostEqual(rows["n2:sell"]["slip_pct"], 100 * (1 - 21.0 / 21.1), places=3)
        self.assertNotIn("slip_pct", rows["n3:sell"])                                                    # sold by hand
        self.assertNotIn("slip_pct", rows["n2:buy"])                                                     # no quote logged
        self.assertEqual(hs.record(self.sd, DAY + datetime.timedelta(days=4))["new"], [])                # once
        s = hs.summary(self.sd)
        self.assertEqual((s["n"], s["buy"]["n"], s["sell"]["n"], s["stop"]["n"], s["judged"]), (4, 2, 2, 1, False))
        self.assertIn("too few to judge", "\n".join(hs.lines(s)))

    def test_the_fill_minute_splits_the_slippage_into_market_and_broker(self):
        self.everpure()
        fills = []

        def fill_fn(row):
            fills.append(row["ref"])
            return {"at": "2026-11-20T14:33:21Z", "price": 102.37, "method": "OTC", "order_id": 58300794273}
        hs.record(self.sd, DAY, fill_fn=fill_fn, minutes_fn=lambda sym, d: EVERPURE)
        r = hs.load(self.sd)["rows"]["d1:buy"]
        self.assertEqual((r["fill_price"], r["fill_method"], r["fill_from"]), (102.37, "OTC", "broker order history"))
        m = r["market"]
        self.assertEqual((m["minute"], m["in_range"]), ("09:33", False))       # above every trade in that minute
        self.assertAlmostEqual(m["market_gap_pct"], 100 * (102.37 / ((101.68 + 101.10) / 2) - 1), places=3)
        self.assertAlmostEqual(m["move_pct"], 100 * (((101.68 + 101.10) / 2) / 101.715 - 1), places=3)
        self.assertAlmostEqual(r["slip_pct"], 100 * (102.37 / 101.715 - 1), places=3)
        hs.record(self.sd, DAY, fill_fn=fill_fn, minutes_fn=lambda sym, d: EVERPURE)
        self.assertEqual(fills, ["P_US_EQ"])                                     # looked up once
        line = M.join(M.friday_slippage(hs.summary(self.sd)))
        self.assertIn("Trading 212's price against the real market that minute: +0.97%; the market moving: −0.32%", line)

    def test_old_fills_are_not_looked_up_after_the_minute_data_is_gone(self):
        self.everpure()
        got = hs.record(self.sd, DAY + datetime.timedelta(days=40), fill_fn=lambda r: 1 / 0,
                        minutes_fn=lambda s, d: 1 / 0)
        r = hs.load(self.sd)["rows"]["d1:buy"]
        self.assertTrue(r["market"]["gone"])
        self.assertNotIn("fill_error", r)
        self.assertEqual(got["completed"], ["d1:buy"])

    def test_a_failed_lookup_is_kept_as_a_note(self):
        self.everpure()
        hs.record(self.sd, DAY, fill_fn=lambda r: (_ for _ in ()).throw(RuntimeError("rate limited")),
                  minutes_fn=lambda s, d: EVERPURE)
        r = hs.load(self.sd)["rows"]["d1:buy"]
        self.assertIn("rate limited", r["fill_error"])
        self.assertNotIn("market", r)                                           # no fill time, no minute yet

    def test_judged_once_at_ten_fills(self):
        pages, quotes = [], []
        for i in range(10):
            pages.append(page("p%d" % i, "S%d" % i, buy_price=100.5 if i < 8 else 100.1))
            quotes.append({"symbol": "S%d" % i, "day": "2026-11-20", "status": "bought", "price": 100.0})
        self.put("state/holdings.json", {"pages": pages[:9]})
        self.put("state/drift/buys.jsonl", quotes)
        self.assertIsNone(hs.record(self.sd, DAY)["flag"])
        self.put("state/holdings.json", {"pages": pages})
        flag = hs.record(self.sd, DAY)["flag"]
        self.assertEqual((flag["n"], flag["over_bar"], flag["consistent"]), (10, 8, True))
        self.assertIsNone(hs.record(self.sd, DAY)["flag"])                     # told once
        msg = M.slippage_judged(flag)
        self.assertIn("Consistently above", M.join(M.friday_slippage(hs.summary(self.sd))))
        self.assertIn("consistently above 0.30% a side", msg)

    def test_not_consistent_when_only_a_few_are_high(self):
        doc = {"rows": {"r%d" % i: {"kind": "buy", "day": "2026-11-20", "slip_pct": 2.0 if i < 3 else 0.1} for i in range(10)}}
        s = hs.summary_of(doc)
        self.assertTrue(s["judged"])
        self.assertFalse(s["consistent"])                                       # average 0.67% but only 3 of 10 over
        self.assertIn("not consistently", M.slippage_judged(s))

    def test_the_source_has_no_order_code_and_no_page_edits(self):
        for name in ("hold_slippage", "entry_shadow"):
            with open(os.path.join(ROOT, "trader-broker", name + ".py")) as fh:
                src = fh.read()
            for rx in (r"\bplace\(", r"\bcancel\w*\(", r"\bpush\(", r"H\.save|holdings\.save", r"\bt212\.\w+\(",
                       r"Client\("):
                self.assertIsNone(re.search(rx, src), (name, rx))


class EntryShadow(Base):
    def test_later_times_and_a_limit_order_on_the_real_everpure_morning(self):
        self.everpure()
        hs.record(self.sd, DAY, fill_fn=lambda r: {"at": "2026-11-20T14:33:21Z", "price": 102.37},
                  minutes_fn=lambda s, d: EVERPURE)
        self.assertEqual(es.run(self.sd, DAY, lambda s, d: EVERPURE), 1)
        b = es.load(self.sd)["buys"]["d1"]
        mid = (101.68 + 101.10) / 2
        self.assertEqual(b["order_minute"], "09:33")
        self.assertAlmostEqual(b["times"]["10:00"]["vs_market_pct"], 100 * (97.0 / mid - 1), places=3)
        self.assertAlmostEqual(b["times"]["10:00"]["vs_fill_pct"], 100 * (97.0 / 102.37 - 1), places=3)
        lim = b["limit"]
        self.assertEqual((lim["price"], lim["filled"], lim["minute"], lim["fill_price"]), (101.71, True, "09:33", 101.71))
        self.assertEqual(es.run(self.sd, DAY, lambda s, d: 1 / 0), 0)          # once a buy
        s = es.summary(self.sd)
        self.assertEqual((s["n"], s["limit"]["filled"], s["times"]["10:30"]["cheaper"]), (1, 1, 1))
        out = M.join(M.friday_entry_shadow(s))
        self.assertIn("A limit order at the 09:32 price would have filled 1 of 1 times", out)
        self.assertIn("Nothing is bought differently", out)

    def test_a_limit_that_never_fills_keeps_the_close(self):
        rising = minutes({"09:32": (100, 100, 100, 100), "09:33": (100.5, 101, 100.4, 101), "10:00": (102, 102, 101.5, 102),
                          "15:50": (105, 105, 105, 105)})
        r = {"page": "x", "symbol": "X", "strategy": "news", "fill_price": 101.0, "fill_at": "2026-11-20T14:33:30Z", "kind": "buy"}
        b = es.evaluate(r, rising, DAY)
        self.assertFalse(b["limit"]["filled"])
        self.assertAlmostEqual(b["limit"]["close_vs_fill_pct"], 100 * (105 / 101.0 - 1), places=3)
        self.assertIn("error", b["times"]["09:45"])                             # no trades then

    def test_without_a_fill_time_it_uses_the_strategys_order_time(self):
        r = {"page": "x", "symbol": "X", "strategy": "news", "fill_price": 100.0, "kind": "buy"}
        flat = minutes({"09:32": (100, 100, 100, 100), "09:35": (100, 100.2, 99.8, 100)})
        self.assertEqual(es.evaluate(r, flat, DAY)["order_minute"], "09:35")


class FillFinder(unittest.TestCase):
    def test_trading_212s_fill_for_the_buy_day_read_only(self):
        calls = []

        class Client(object):
            def get(self, path, params=None):
                calls.append((path, params))
                return {"items": [
                    {"order": {"id": 1, "side": "SELL", "type": "STOP"}, "fill": {"price": 70, "filledAt": "2026-11-20T15:00:00Z", "quantity": 2.04}},
                    {"order": {"id": 2, "side": "BUY", "type": "MARKET"}, "fill": {"price": 99, "filledAt": "2026-11-19T14:33:00Z", "quantity": 2.04}},
                    {"order": {"id": 58300794273, "side": "BUY", "type": "MARKET"},
                     "fill": {"price": 102.37, "filledAt": "2026-11-20T14:33:21.000Z", "quantity": 2.04, "tradingMethod": "OTC"}}]}
        find = HT.fill_finder(Client())
        got = find({"ref": "PSTG_US_EQ", "kind": "buy", "day": "2026-11-20", "qty": 2.04})
        self.assertEqual(got, {"at": "2026-11-20T14:33:21Z", "price": 102.37, "method": "OTC", "order_id": 58300794273})
        find({"ref": "PSTG_US_EQ", "kind": "sell", "day": "2026-11-20", "qty": 2.04})
        self.assertEqual(len(calls), 1)                                         # one read per ticker
        self.assertEqual(calls[0][0], HT.t212.HISTORY)


if __name__ == "__main__":
    unittest.main()
