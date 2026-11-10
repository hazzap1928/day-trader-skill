"""Stage 1 of post-earnings drift: buy records carry a strategy ("intraday" or "drift").
The 15:50 close keeps a drift position until its sell_date, a position with no buy record is
alerted rather than sold, and the trail and half-sell never touch a drift position.
Nothing buys drift yet: these tests write drift records by hand."""

import datetime
import io
import unittest
from contextlib import redirect_stdout

import test_eod_close
import test_trailing
from test_eod_close import TICKER, STOP, held, record, ny
from test_t212_place import FakeResp

t212 = test_eod_close.t212


class CloseKeepsDrift(test_eod_close.Close):
    """2026-11-05 (a Thursday) is today in New York in these tests."""

    def setUp(self):
        super().setUp()
        self.clock[0] = ny(15, 50)

    def records(self, *recs):
        self.write("state/broker.json", {"orders": list(recs)})

    def writes(self):
        return [(c[0], c[1]) for c in self.router.calls if c[0] in ("DELETE", "POST")]

    def test_a_drift_position_before_its_sell_date_survives_with_its_stop(self):
        self.records(record(strategy="drift", sell_date="2026-11-06"))
        self.account([held(7)])
        res = self.close()
        self.assertEqual(self.writes(), [])                     # the stop is not cancelled, nothing is sold
        self.assertEqual(res["closed"], [])
        self.assertEqual(res["failed"], [])
        self.assertIn("drift, sell date 2026-11-06", res["kept"][0])
        self.assertIn("kept: TSLA_US_EQ", self.pushed[-1])
        self.assertFalse(res["nothing"])
        self.assertIn("kept", self.read("state/t212-guard.json")["eod"])

    def test_a_drift_position_is_sold_on_its_sell_date(self):
        self.records(record(strategy="drift", sell_date="2026-11-05"))
        self.account([held(7)])
        res = self.close()
        self.assertEqual(self.writes(), [("DELETE", "/equity/orders/7001"), ("POST", "/equity/orders/market")])
        self.assertEqual(self.router.posts()[0][2]["quantity"], -7)
        self.assertIn("drift sell date 2026-11-05", res["closed"][0])
        self.assertEqual(res["kept"], [])

    def test_an_overdue_drift_position_is_sold(self):
        self.records(record(strategy="drift", sell_date="2026-11-02"))
        self.account([held(7)])
        res = self.close()
        self.assertEqual(self.writes(), [("DELETE", "/equity/orders/7001"), ("POST", "/equity/orders/market")])
        self.assertIn("overdue", res["closed"][0])

    def test_a_drift_record_without_a_sell_date_is_sold_like_intraday(self):
        for bad in (None, "", "soon"):
            self.router.routes.clear()
            self.router.calls.clear()
            self.records(record(strategy="drift", sell_date=bad))
            self.account([held(7)])
            res = self.close()
            self.assertEqual(len(self.router.posts()), 1, bad)
            self.assertIn("no readable sell_date", res["closed"][0])

    def test_intraday_and_unlabelled_positions_are_closed_exactly_as_before(self):
        for rec in (record(), record(strategy="intraday"), record(strategy="INTRADAY")):
            self.router.routes.clear()
            self.router.calls.clear()
            self.records(rec)
            self.account([held(7)])
            res = self.close()
            self.assertEqual(self.writes(), [("DELETE", "/equity/orders/7001"), ("POST", "/equity/orders/market")])
            self.assertEqual(res["closed"], ["TSLA_US_EQ: SELL 7 at market, order id 9300, cancelled STOP 358.32"])
            self.assertEqual((res["kept"], res["unknown"]), ([], []))

    def test_a_position_with_no_buy_record_is_alerted_not_sold(self):
        self.records()
        self.account([held(7)])
        res = self.close()
        self.assertEqual(self.writes(), [])
        self.assertEqual(res["closed"], [])
        self.assertIn("no buy record", res["unknown"][0])
        self.assertTrue(any(p.startswith("GUARD ALERT (DEMO): END-OF-DAY CLOSE: TSLA_US_EQ") for p in self.pushed))
        self.assertTrue(any("no buy record" in n[1] for n in self.notes))          # the Mac too

    def test_a_refused_buy_is_not_a_buy_record(self):
        self.records(record(status="failed"))
        self.account([held(7)])
        res = self.close()
        self.assertEqual(self.router.posts(), [])
        self.assertEqual(len(res["unknown"]), 1)

    def test_a_record_from_the_other_account_does_not_count(self):
        self.records(record(env="live"))
        self.account([held(7)])
        res = self.close()
        self.assertEqual(self.router.posts(), [])
        self.assertEqual(len(res["unknown"]), 1)

    def test_a_mixed_book_sells_only_what_is_due(self):
        aapl = {"id": 7002, "ticker": "AAPL_US_EQ", "type": "STOP", "quantity": -3, "stopPrice": 250.0, "status": "NEW"}
        self.records(record(), record("AAPL_US_EQ", strategy="drift", sell_date="2026-11-10"))
        r = self.router
        r.add("GET", "/equity/positions", FakeResp([held(7), held(3, "AAPL_US_EQ"), held(1, "MSFT_US_EQ")]))
        r.add("GET", "/equity/orders", FakeResp([STOP, aapl]), FakeResp([aapl]))
        r.add("DELETE", "/equity/orders/7001", FakeResp({}))
        r.add("POST", "/equity/orders/market", FakeResp({"id": 9300}))
        res = self.close()
        self.assertEqual(self.writes(), [("DELETE", "/equity/orders/7001"), ("POST", "/equity/orders/market")])
        self.assertEqual(self.router.posts()[0][2]["ticker"], TICKER)
        self.assertEqual(len(res["closed"]), 1)
        self.assertIn("AAPL_US_EQ", res["kept"][0])
        self.assertIn("MSFT_US_EQ", res["unknown"][0])


class GuardLeavesDriftAlone(test_trailing.GuardTrail):
    """At 380 the trade is far past 1R and the 370.04 target: an intraday position would trail and sell half."""

    def drift(self, **extra):
        self.write("state/broker.json", {"orders": [dict(test_trailing.RECORD, strategy="drift",
                                                         sell_date="2026-11-12", **extra)]})

    def test_an_intraday_position_still_trails_and_sells_half(self):
        self.set_trail(distance_r=1.0, sell_half_at_target=True)
        self.write("state/broker.json", {"orders": [dict(test_trailing.RECORD, strategy="intraday")]})
        self.set_account(380.0, after_cancel=[])
        self.guard()
        self.assertEqual(self.router.posts("/equity/orders/market")[0][2]["quantity"], -3)

    def test_no_trail_and_no_half_sale_for_a_drift_position(self):
        self.set_trail(distance_r=1.0, sell_half_at_target=True)
        self.drift()
        self.set_account(380.0, after_cancel=[])
        code, text, err = self.guard()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.router.posts(), [])
        self.assertEqual(self.calls("DELETE"), [])
        self.assertIn("drift position: no trailing or half sale", text)
        self.assertNotIn(test_trailing.TICKER, (self.read("state/t212-guard.json") or {}).get("trail", {}))

    def test_a_drift_position_with_a_missing_stop_gets_its_stop_back(self):
        self.set_trail(distance_r=1.0, sell_half_at_target=True)
        self.drift()
        self.set_account(380.0, pending=[])
        self.guard()
        posts = self.router.posts()
        self.assertEqual([p[1] for p in posts], ["/equity/orders/stop"])
        self.assertEqual(posts[0][2]["stopPrice"], 358.32)            # the ticket's stop, not a trailed one

    def test_market_stop_mode_no_half_sale_and_no_raise_for_drift(self):
        self.write("config.json", dict(self.cfg, stop_mode="market",
                                       trailing_stop={"enabled": True, "distance_r": 1.0, "sell_half_at_target": True}))
        self.drift()
        self.set_account(380.0, pending=[])
        self.guard()
        self.assertEqual(self.router.posts(), [])
        self.assertEqual(self.read("state/t212-guard.json")["trail"][test_trailing.TICKER]["stop"], 358.32)

    def test_market_stop_mode_still_sells_a_drift_position_at_its_stop(self):
        self.write("config.json", dict(self.cfg, stop_mode="market",
                                       trailing_stop={"enabled": True, "distance_r": 1.0, "sell_half_at_target": True}))
        self.drift()
        self.set_account(350.0, pending=[])
        self.guard()
        posts = self.router.posts("/equity/orders/market")
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][2]["quantity"], -7)

    def test_a_software_stop_drift_position_is_not_half_sold(self):
        self.set_trail(distance_r=1.0, sell_half_at_target=True)
        self.drift(status="software_stop")
        self.set_account(380.0, pending=[])
        self.guard()
        self.assertEqual(self.router.posts(), [])


class PlaceLabelsItsBuys(test_trailing.GuardTrail):
    def test_a_buy_from_place_is_labelled_intraday(self):
        self.write("state/broker.json", {"orders": []})
        self.account()
        code, _, err = self.place()
        self.assertEqual(code, 0, err)
        buys = [r for r in self.broker_log() if r.get("side") == "buy"]
        self.assertTrue(buys)
        self.assertEqual({r.get("strategy") for r in buys}, {"intraday"})
        self.assertFalse(any("sell_date" in r for r in buys))


class SellDate(unittest.TestCase):
    def test_counts_trading_days_forward(self):
        self.assertEqual(t212.drift_sell_date("2026-11-10", 1), "2026-11-11")      # Tue -> Wed
        self.assertEqual(t212.drift_sell_date("2026-11-10", 3), "2026-11-13")
        self.assertEqual(t212.drift_sell_date(datetime.date(2026, 11, 10), 3), "2026-11-13")

    def test_skips_weekends(self):
        self.assertEqual(t212.drift_sell_date("2026-11-13", 1), "2026-11-16")      # Fri -> Mon
        self.assertEqual(t212.drift_sell_date("2026-11-14", 1), "2026-11-16")      # from a Saturday
        self.assertEqual(t212.drift_sell_date("2026-11-09", 10), "2026-11-23")     # two whole weeks

    def test_skips_nyse_holidays(self):
        self.assertEqual(t212.drift_sell_date("2026-11-25", 1), "2026-11-27")      # Thanksgiving
        self.assertEqual(t212.drift_sell_date("2026-12-24", 1), "2026-12-28")      # Christmas, then the weekend
        self.assertEqual(t212.drift_sell_date("2026-12-31", 1), "2027-01-04")      # New Year's Day
        self.assertEqual(t212.drift_sell_date("2027-03-25", 1), "2027-03-29")      # Good Friday 2027
        self.assertEqual(t212.drift_sell_date("2026-11-20", 5), "2026-11-30")      # a week with Thanksgiving

    def test_refuses_past_the_holiday_list_and_bad_input(self):
        self.assertEqual(t212.drift_sell_date("2027-12-30", 1), "2027-12-31")
        for args in (("2027-12-30", 2), ("2026-11-10", 0), ("2026-11-10", -1), ("2026-11-10", 1.5),
                     ("2026-11-10", True), ("not a date", 1), (None, 1)):
            with self.assertRaises(t212.T212Error, msg=args):
                t212.drift_sell_date(*args)


class Labels(unittest.TestCase):
    def test_strategy_of_a_record(self):
        self.assertEqual(t212.position_strategy({}), "intraday")
        self.assertEqual(t212.position_strategy(None), "intraday")
        self.assertEqual(t212.position_strategy({"strategy": "intraday"}), "intraday")
        self.assertEqual(t212.position_strategy({"strategy": " Drift "}), "drift")
        self.assertEqual(t212.position_strategy({"strategy": "swing"}), "intraday")
        self.assertFalse(t212.is_drift(None))
        self.assertTrue(t212.is_drift({"strategy": "drift"}))

    def test_drift_due(self):
        rec = {"strategy": "drift", "sell_date": "2026-11-12"}
        self.assertFalse(t212.drift_due(rec, "2026-11-11")[0])
        self.assertTrue(t212.drift_due(rec, "2026-11-12")[0])
        self.assertTrue(t212.drift_due(rec, datetime.date(2026, 11, 13))[0])
        self.assertTrue(t212.drift_due({"strategy": "drift"}, "2026-11-11")[0])


if __name__ == "__main__":
    unittest.main()
