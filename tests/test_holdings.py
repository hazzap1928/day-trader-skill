"""The holdings record (trader-broker/holdings.py), the daily match against Trading 212
(holdings_t212.py, report only), and "keep and report" at the 15:50 close: no holding that is not a
plain day trade is ever sold there unless its sell date has come or you approved the sale.
A fake Trading 212, no network, no real order."""

import datetime
import io
import os
import unittest
from contextlib import redirect_stdout

from _load import load
import test_eod_close
import test_trailing
from test_eod_close import TICKER, STOP, held, record, ny
from test_t212_place import FakeResp, CREDS, SUMMARY

t212 = test_eod_close.t212
H = load("trader-broker", "holdings")
HT = load("trader-broker", "holdings_t212")
HT.t212, HT.holdings = t212, H
report = load("trader-broker", "report")
report.t212, report.tk = t212, t212.ticket_module()

TODAY = "2026-11-05"
RATE = 1.25


def view(positions=(), stops=(), buys=(), waiting=(), sales=(), soft=None):
    return {"broker": "trading212", "account": "demo", "usd_per_gbp": RATE, "account_gbp": 5000.0,
            "positions": list(positions), "stops": list(stops), "soft_stops": soft or {}, "waiting": list(waiting),
            "buys": list(buys), "sales": list(sales)}


def pos(sym="TSLA", qty=7, avg=364.0, price=366.0):
    return {"symbol": sym, "ref": sym + "_US_EQ", "qty": qty, "avg": avg, "price": price}


def stop(sym="TSLA", qty=7, px=358.32):
    return {"symbol": sym, "ref": sym + "_US_EQ", "qty": qty, "stop": px, "id": 7001}


def buy(sym="TSLA", strategy="news", date=TODAY, qty=7, sell_date="2026-12-01", **kw):
    return dict({"key": "fp-%s:%s" % (sym, date), "symbol": sym, "ref": sym + "_US_EQ", "strategy": strategy,
                 "date": date, "qty": qty, "entry": 364.0, "stop": 358.32, "sell_date": sell_date,
                 "cost_gbp": 2040.0, "max_loss_gbp": 32.0}, **kw)


def doc():
    return {"schema": 1, "since": None, "pages": [], "reported": {}}


class Pages(unittest.TestCase):
    def test_a_buy_gets_one_page_with_its_reason_and_sell_rule(self):
        d = doc()
        b = buy(reason="GTA 6 release", wrong_if="the release slips", event="GTA 6", event_date="2026-11-19",
                sources=[{"title": "t", "url": "https://x", "date": "2026-11-01"}], idea="2026-11-05-TSLA")
        ev = H.sync(d, view([pos()], [stop()], [b]), TODAY)
        self.assertEqual([e for e, _ in ev], ["page", "open"])
        p = d["pages"][0]
        self.assertEqual((p["symbol"], p["strategy"], p["status"], p["qty"], p["stop"]), ("TSLA", "news", "open", 7, 358.32))
        self.assertEqual(p["reason"], "GTA 6 release")
        self.assertEqual(p["wrong_if"], "the release slips")
        self.assertEqual(p["sell_rule"]["date"], "2026-12-01")
        self.assertEqual((p["broker"], p["account"], p["broker_ref"]["ref"]), ("trading212", "demo", "TSLA_US_EQ"))
        self.assertEqual(H.sync(d, view([pos()], [stop()], [b]), TODAY), [])      # the same look twice: one page
        self.assertEqual(len(d["pages"]), 1)

    def test_the_page_is_broker_neutral(self):
        d = doc()
        v = dict(view([dict(pos(), ref="265598")], [dict(stop(), ref="265598")], [dict(buy(), ref="265598")]),
                 broker="ibkr", account="paper")
        H.sync(d, v, TODAY)
        self.assertEqual(H.match(d, v, TODAY), [])
        self.assertEqual(d["pages"][0]["broker"], "ibkr")
        self.assertEqual(H.live(d, "trading212", "demo"), [])                    # another broker's pages are not mixed in

    def test_a_drift_and_an_intraday_page_say_what_sells_them(self):
        d = doc()
        H.sync(d, view([pos(), pos("AAPL", 3)], [], [buy(strategy="drift"), buy("AAPL", strategy=None, sell_date=None)]), TODAY)
        by = {p["symbol"]: p for p in d["pages"]}
        self.assertIn("20th trading day", by["TSLA"]["sell_rule"]["rule"])
        self.assertEqual(by["AAPL"]["strategy"], "intraday")
        self.assertIn("15:50", by["AAPL"]["sell_rule"]["rule"])

    def test_a_sale_in_the_brokers_fills_closes_the_page_with_its_result(self):
        d = doc()
        H.sync(d, view([pos()], [stop()], [buy()]), TODAY)
        sale = {"ref": TICKER, "qty": 7, "exit": 370.0, "date": "2026-11-09", "buy_date": TODAY, "pl_gbp": 21.5,
                "why": "stop"}
        ev = H.sync(d, view([], [], [buy()], sales=[sale]), "2026-11-09")
        self.assertEqual([e for e, _ in ev], ["closed"])
        p = d["pages"][0]
        self.assertEqual(p["status"], "closed")
        self.assertEqual(p["result"], {"date": "2026-11-09", "price": 370.0, "why": "stop", "pl_gbp": 21.5, "qty": 7})
        self.assertEqual(H.match(d, view(), "2026-11-09"), [])

    def test_a_sale_from_before_the_buy_does_not_close_it(self):
        d = doc()
        H.sync(d, view([pos()], [stop()], [buy()]), TODAY)
        old = {"ref": TICKER, "qty": 7, "exit": 300.0, "date": "2026-10-01", "buy_date": "2026-09-30", "pl_gbp": 1.0}
        H.sync(d, view([], [], [buy()], sales=[old]), "2026-11-06")
        self.assertEqual(d["pages"][0]["status"], "mismatch")

    def test_a_buy_waiting_to_fill_is_not_a_mismatch(self):
        d = doc()
        v = view([], [], [buy()], waiting=[{"symbol": "TSLA", "ref": TICKER, "qty": 7}])
        H.sync(d, v, "2026-11-06")
        self.assertEqual(d["pages"][0]["status"], "waiting")
        self.assertEqual(H.match(d, v, "2026-11-06"), [])

    def test_old_buys_from_before_the_record_began_get_no_page_unless_still_held(self):
        d = dict(doc(), since=TODAY)
        H.sync(d, view([], [], [buy(date="2026-10-01")]), TODAY)
        self.assertEqual(d["pages"], [])
        H.sync(d, view([pos()], [stop()], [buy(date="2026-10-01")]), TODAY)
        self.assertEqual(len(d["pages"]), 1)


class Match(unittest.TestCase):
    def kinds(self, d, v, today=TODAY):
        return [m["kind"] for m in H.match(d, v, today)]

    def synced(self, v, today=TODAY):
        d = doc()
        H.sync(d, v, today)
        return d

    def test_everything_agrees(self):
        v = view([pos()], [stop()], [buy()])
        self.assertEqual(self.kinds(self.synced(v), v), [])

    def test_held_at_the_broker_with_no_page(self):
        v = view([pos()], [stop()])
        m = H.match(self.synced(v), v, TODAY)
        self.assertEqual([x["kind"] for x in m], ["no_record"])
        self.assertIn("no page", H.mismatch_line(m[0]))

    def test_the_record_says_held_and_nothing_is(self):
        v = view([pos()], [stop()], [buy()])
        d = self.synced(v)
        gone = view([], [], [buy()])
        H.sync(d, gone, "2026-11-06")
        self.assertEqual(self.kinds(d, gone, "2026-11-06"), ["not_held"])
        self.assertEqual(d["pages"][0]["status"], "mismatch")          # marked, not closed, not deleted

    def test_share_counts_differ(self):
        v = view([pos(qty=4)], [stop(qty=4)], [buy()])
        self.assertEqual(self.kinds(self.synced(v), v), ["qty"])

    def test_no_stop_a_part_stop_and_a_software_stop(self):
        v = view([pos()], [], [buy()])
        self.assertEqual(self.kinds(self.synced(v), v), ["no_stop"])
        v = view([pos()], [stop(qty=3)], [buy()])
        self.assertEqual(self.kinds(self.synced(v), v), ["part_stop"])
        v = view([pos()], [], [buy()], soft={TICKER: 358.32})
        self.assertEqual(self.kinds(self.synced(v), v), ["soft_stop"])

    def test_a_stop_for_a_share_that_is_not_held(self):
        v = view([], [stop()])
        self.assertEqual(self.kinds(self.synced(v), v), ["stray_stop"])
        v = view([], [stop()], [buy()], waiting=[{"symbol": "TSLA", "ref": TICKER, "qty": 7}])
        self.assertEqual(self.kinds(self.synced(v), v), [])            # its buy is still filling

    def test_an_unknown_label_and_a_missing_sell_date_are_reported(self):
        v = view([pos()], [stop()], [buy(strategy="swing")])
        self.assertEqual(self.kinds(self.synced(v), v), ["unknown_label"])
        v = view([pos()], [stop()], [buy(strategy="news", sell_date=None)])
        self.assertEqual(self.kinds(self.synced(v), v), ["no_sell_rule"])
        v = view([pos()], [stop()], [buy(strategy="intraday", sell_date=None)])
        self.assertEqual(self.kinds(self.synced(v), v), [])

    def test_match_changes_nothing(self):
        v = view([pos(qty=4)], [], [buy()])
        d = self.synced(v)
        before = repr(d)
        H.match(d, v, TODAY)
        self.assertEqual(repr(d), before)

    def test_a_mismatch_is_told_once_and_again_only_when_it_changes(self):
        v = view([pos()], [], [buy()])
        d = self.synced(v)
        self.assertEqual(len(H.fresh(d, H.match(d, v, TODAY))), 1)
        self.assertEqual(H.fresh(d, H.match(d, v, TODAY)), [])
        ok = view([pos()], [stop()], [buy()])
        self.assertEqual(H.fresh(d, H.match(d, ok, TODAY)), [])
        self.assertEqual(d["reported"], {})                            # gone: forgotten, so it is told again next time
        self.assertEqual(len(H.fresh(d, H.match(d, v, TODAY))), 1)


class OpenRisk(unittest.TestCase):
    def test_risk_at_the_live_stop_with_the_fee(self):
        r = H.open_risk(view([pos(qty=10, avg=100.0)], [stop(qty=10, px=95.0)], [buy(qty=10)]))
        want = 10 * (5.0 + 0.0015 * 195.0) / RATE
        self.assertAlmostEqual(r["open_risk_gbp"], round(want, 2))
        self.assertEqual(r["rows"][0]["how"], "stop order")

    def test_a_stop_above_the_price_paid_risks_nothing(self):
        r = H.open_risk(view([pos(qty=10, avg=100.0)], [stop(qty=10, px=120.0)]))
        self.assertEqual(r["open_risk_gbp"], 0.0)

    def test_no_stop_at_all_counts_the_whole_value(self):
        r = H.open_risk(view([pos(qty=10, avg=100.0, price=110.0)]))
        self.assertEqual(r["open_risk_gbp"], round(10 * 110.0 / RATE, 2))
        self.assertIn("whole value", r["rows"][0]["how"])

    def test_the_planned_stop_and_the_software_stop_count_when_no_order_is_in(self):
        b = buy(qty=10)
        r = H.open_risk(view([pos(qty=10, avg=364.0)], [], [b]))
        self.assertEqual(r["rows"][0]["how"], "planned stop")
        r = H.open_risk(view([pos(qty=10, avg=364.0)], [], [b], soft={TICKER: 360.0}))
        self.assertEqual((r["rows"][0]["how"], r["rows"][0]["stop"]), ("software stop", 360.0))

    def test_a_buy_waiting_to_fill_counts_its_planned_loss(self):
        r = H.open_risk(view([], [], [buy()], waiting=[{"symbol": "TSLA", "ref": TICKER, "qty": 7}]))
        self.assertEqual(r["open_risk_gbp"], 32.0)

    def test_cost_by_strategy(self):
        v = view([pos(qty=10, avg=100.0), pos("AAPL", 5, 200.0)], [],
                 [buy(strategy="drift"), buy("AAPL", strategy="news")])
        c = H.cost_by_strategy(v)
        self.assertAlmostEqual(c["drift"], round(1000 / RATE * 1.0015, 2))
        self.assertAlmostEqual(c["news"], round(1000 / RATE * 1.0015, 2))


class DailyMatch(test_eod_close.Close):
    """holdings_t212.daily_match against the fake Trading 212: it only reads."""

    def setUp(self):
        super().setUp()
        self.clock[0] = ny(16, 5)

    def account_is(self, positions, orders):
        r = self.router
        r.add("GET", "/equity/positions", FakeResp(positions))
        r.add("GET", "/equity/orders", FakeResp(orders))
        r.add("GET", "/equity/account/summary", FakeResp(SUMMARY))

    def run_match(self):
        return HT.daily_match(self.client, self.dir, RATE, TODAY, report)

    def test_it_reads_and_never_writes_to_the_broker(self):
        self.write("state/broker.json", {"orders": [record(strategy="news", sell_date="2026-12-01")]})
        self.account_is([held(7)], [STOP])
        res = self.run_match()
        self.assertEqual(res["mismatches"], [])
        self.assertEqual([c[0] for c in self.router.calls if c[0] != "GET"], [])
        pages = self.read("state/holdings.json")["pages"]
        self.assertEqual([(p["symbol"], p["strategy"], p["status"]) for p in pages], [("TSLA", "news", "open")])
        self.assertTrue(os.path.exists(os.path.join(self.dir, "state", "holdings-log.jsonl")))
        self.assertGreater(res["risk"]["open_risk_gbp"], 0)

    def test_a_position_with_no_record_and_one_without_a_stop_are_reported_not_fixed(self):
        self.write("state/broker.json", {"orders": [record(strategy="drift", sell_date="2026-12-01")]})
        self.account_is([held(7), held(2, "MSFT_US_EQ")], [])
        res = self.run_match()
        kinds = sorted((m["kind"], m["symbol"]) for m in res["mismatches"])
        self.assertEqual(kinds, [("no_record", "MSFT"), ("no_stop", "MSFT"), ("no_stop", "TSLA")])
        self.assertEqual([c[0] for c in self.router.calls if c[0] != "GET"], [])     # nothing placed, nothing cancelled
        self.assertEqual(len(res["new"]), 3)
        self.router.calls.clear()
        self.assertEqual(self.run_match()["new"], [])                                # told once
        self.assertIn("MSFT", HT.text(res))

    def test_a_refused_buy_makes_no_page(self):
        self.write("state/broker.json", {"orders": [record(status="failed")]})
        self.account_is([], [])
        self.run_match()
        self.assertEqual(self.read("state/holdings.json")["pages"], [])

    def test_a_news_page_carries_the_idea(self):
        self.write("state/broker.json", {"orders": [record(strategy="news", sell_date="2026-12-01", idea="2026-11-05-TSLA")]})
        self.account_is([held(7)], [STOP])
        idea = {"why": "robotaxi launch", "wrong_if": "the launch is delayed", "event": "launch", "event_date": "2026-11-20",
                "sell_rule": "a week after the launch", "sources": [{"title": "t", "url": "https://x", "date": "2026-11-01"}]}
        HT.daily_match(self.client, self.dir, RATE, TODAY, report, ideas={"2026-11-05-TSLA": idea})
        p = self.read("state/holdings.json")["pages"][0]
        self.assertEqual((p["reason"], p["wrong_if"], p["event_date"]), ("robotaxi launch", "the launch is delayed", "2026-11-20"))
        self.assertEqual(p["sell_rule"], {"date": "2026-12-01", "rule": "a week after the launch"})


class CloseKeepsHoldings(test_eod_close.Close):
    """Held shares survive the 15:50 close. 2026-11-05 (a Thursday) is today in New York."""

    def setUp(self):
        super().setUp()
        self.clock[0] = ny(15, 50)

    def records(self, *recs):
        self.write("state/broker.json", {"orders": list(recs)})

    def writes(self):
        return [(c[0], c[1]) for c in self.router.calls if c[0] in ("DELETE", "POST")]

    def test_a_news_holding_before_its_sell_date_survives_with_its_stop(self):
        self.records(record(strategy="news", sell_date="2026-12-01"))
        self.account([held(7)])
        res = self.close()
        self.assertEqual(self.writes(), [])
        self.assertEqual((res["closed"], res["failed"], res["unknown"]), ([], [], []))
        self.assertIn("news, sell date 2026-12-01", res["kept"][0])

    def test_a_news_holding_is_sold_on_its_sell_date(self):
        self.records(record(strategy="news", sell_date=TODAY))
        self.account([held(7)])
        res = self.close()
        self.assertEqual(self.writes(), [("DELETE", "/equity/orders/7001"), ("POST", "/equity/orders/market")])
        self.assertIn("news sell date 2026-11-05", res["closed"][0])

    def test_an_unknown_label_is_kept_and_reported_never_sold(self):
        for label in ("swing", "longterm", "NEWS2"):
            self.router.routes.clear()
            self.router.calls.clear()
            self.pushed[:] = []
            self.records(record(strategy=label, sell_date=TODAY))
            self.account([held(7)])
            res = self.close()
            self.assertEqual(self.writes(), [], label)
            self.assertEqual(res["closed"], [], label)
            self.assertIn("KEPT", res["unknown"][0])
            self.assertTrue(self.pushed, label)                  # you are told

    def test_a_holding_with_no_readable_sell_date_is_kept_and_reported(self):
        for strategy in ("drift", "news"):
            for bad in (None, "", "soon"):
                self.router.routes.clear()
                self.router.calls.clear()
                self.pushed[:] = []
                self.records(record(strategy=strategy, sell_date=bad))
                self.account([held(7)])
                res = self.close()
                self.assertEqual(self.writes(), [], (strategy, bad))
                self.assertIn("no readable sell date", res["unknown"][0])
                self.assertTrue(self.pushed)

    def test_a_position_with_no_record_is_not_sold(self):
        self.records()
        self.account([held(7)])
        res = self.close()
        self.assertEqual(self.writes(), [])
        self.assertIn("no buy record", res["unknown"][0])

    def test_a_day_trade_is_still_sold_at_the_close(self):
        self.records(record(), record("AAPL_US_EQ", strategy="news", sell_date="2026-12-01"))
        aapl = {"id": 7002, "ticker": "AAPL_US_EQ", "type": "STOP", "quantity": -3, "stopPrice": 250.0, "status": "NEW"}
        r = self.router
        r.add("GET", "/equity/positions", FakeResp([held(7), held(3, "AAPL_US_EQ")]))
        r.add("GET", "/equity/orders", FakeResp([STOP, aapl]), FakeResp([aapl]))
        r.add("DELETE", "/equity/orders/7001", FakeResp({}))
        r.add("POST", "/equity/orders/market", FakeResp({"id": 9300}))
        res = self.close()
        self.assertEqual(self.writes(), [("DELETE", "/equity/orders/7001"), ("POST", "/equity/orders/market")])
        self.assertEqual(self.router.posts()[0][2]["ticker"], TICKER)
        self.assertIn("AAPL_US_EQ", res["kept"][0])

    def test_a_sale_you_approved_is_sold_at_the_close_and_only_that_one(self):
        self.records(record(strategy="news", sell_date="2026-12-01"))
        page = {"id": "p1", "symbol": "TSLA", "strategy": "news", "status": "selling", "broker": "trading212",
                "account": "demo", "broker_ref": {"ref": TICKER}, "sell_rule": {"date": "2026-12-01"}}
        self.write("state/holdings.json", {"schema": 1, "pages": [page], "reported": {}})
        self.account([held(7)])
        res = self.close()
        self.assertEqual(self.writes(), [("DELETE", "/equity/orders/7001"), ("POST", "/equity/orders/market")])
        self.assertIn("a sale you approved", res["closed"][0])

    def test_an_approval_on_the_other_account_or_only_asked_does_not_sell(self):
        self.records(record(strategy="news", sell_date="2026-12-01"))
        for change in ({"account": "live"}, {"status": "sell_asked"}, {"status": "open"}):
            self.router.routes.clear()
            self.router.calls.clear()
            page = dict({"id": "p1", "symbol": "TSLA", "strategy": "news", "status": "selling", "broker": "trading212",
                         "account": "demo", "broker_ref": {"ref": TICKER}}, **change)
            self.write("state/holdings.json", {"schema": 1, "pages": [page], "reported": {}})
            self.account([held(7)])
            self.close()
            self.assertEqual(self.writes(), [], change)


class GuardLeavesHoldingsAlone(test_trailing.GuardTrail):
    """At 380 the trade is far past 1R and the target: a day trade would trail and sell half. A holding never."""

    def hold(self, label, **extra):
        self.write("state/broker.json", {"orders": [dict(test_trailing.RECORD, strategy=label,
                                                         sell_date="2026-12-01", **extra)]})

    def test_no_trail_and_no_half_sale_for_news_or_an_unknown_label(self):
        for label in ("news", "swing"):
            self.router.routes.clear()
            self.router.calls.clear()
            self.set_trail(distance_r=1.0, sell_half_at_target=True)
            self.hold(label)
            self.set_account(380.0, after_cancel=[])
            code, text, err = self.guard()
            self.assertEqual(code, 0, err)
            self.assertEqual(self.router.posts(), [], label)
            self.assertEqual(self.calls("DELETE"), [], label)
            self.assertIn("no trailing or half sale", text)

    def test_a_news_holding_with_a_missing_stop_gets_its_stop_back(self):
        self.set_trail(distance_r=1.0, sell_half_at_target=True)
        self.hold("news")
        self.set_account(380.0, pending=[])
        self.guard()
        posts = self.router.posts()
        self.assertEqual([p[1] for p in posts], ["/equity/orders/stop"])
        self.assertEqual(posts[0][2]["stopPrice"], 358.32)


class Labels(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(t212.position_strategy({}), "intraday")
        self.assertEqual(t212.position_strategy({"strategy": " News "}), "news")
        self.assertEqual(t212.position_strategy({"strategy": "swing"}), "swing")       # kept as written
        for label in ("drift", "news", "swing"):
            self.assertTrue(t212.is_hold({"strategy": label}), label)
        self.assertFalse(t212.is_hold({"strategy": "intraday"}))
        self.assertFalse(t212.is_hold({}))
        self.assertFalse(t212.is_hold(None))

    def test_hold_due(self):
        rec = {"strategy": "news", "sell_date": "2026-11-12"}
        self.assertEqual(t212.hold_due(rec, "2026-11-11")[::2], (False, False))
        self.assertTrue(t212.hold_due(rec, "2026-11-12")[0])
        self.assertTrue(t212.hold_due(rec, datetime.date(2026, 11, 13))[0])
        self.assertEqual(t212.hold_due({"strategy": "news"}, "2026-11-11")[::2], (False, True))
        self.assertEqual(t212.hold_due({"strategy": "swing", "sell_date": "2026-11-01"}, "2026-11-11")[::2], (False, True))
        self.assertTrue(t212.hold_due(rec, "2026-11-11", approved=True)[0])

if __name__ == "__main__":
    unittest.main()
