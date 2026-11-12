"""Stops are checked after they are placed: a REJECTED or missing stop switches that position to a
software stop (market sell at the stop) with a loud alert. Fake Trading 212, no network."""

import calendar
import io
import unittest
from contextlib import redirect_stdout, redirect_stderr

import test_t212_place
from test_t212_place import Base, FakeResp, CREDS, HELD7, http_error
from test_t212_guard import position, stop_order, entry_record, TICKER

t212 = test_t212_place.t212


def history(*orders):
    return FakeResp({"items": [{"order": o} for o in orders], "nextPagePath": None})


REJECTED = {"id": 7001, "ticker": TICKER, "type": "STOP", "quantity": -7, "filledQuantity": 0, "status": "REJECTED"}


class Place(Base):
    def test_the_stop_waits_until_the_shares_are_free_to_sell(self):
        settling = [{"instrument": {"ticker": TICKER}, "quantity": 7, "quantityAvailableForTrading": 0,
                     "currentPrice": 366.0}]
        self.account()
        self.router.routes[("GET", "/equity/positions")] = [FakeResp([]), FakeResp(settling), FakeResp(settling),
                                                            FakeResp(HELD7)]
        code, out, err = self.place()
        self.assertEqual(code, 0, err)
        order = [(c[0], c[1]) for c in self.router.calls]
        stop_at = order.index(("POST", "/equity/orders/stop"))
        self.assertEqual(order[:stop_at].count(("GET", "/equity/positions")), 4)   # 1 before, 3 waiting

    def test_a_stop_rejected_a_minute_later_switches_to_software_and_alarms(self):
        self.account()
        dead = {"id": 5002, "ticker": TICKER, "type": "STOP", "quantity": -7, "status": "REJECTED"}
        self.router.routes[("GET", "/equity/orders")] = [FakeResp([]), FakeResp([dead])]
        code, out, err = self.place()
        self.assertEqual(code, 3)
        self.assertIn("STOP REJECTED by Trading 212", err)
        self.assertIn(60, self.sleeps)
        rec = self.broker_log()[-1]
        self.assertEqual(rec["status"], "stop_rejected")
        sw = self.read("state/t212-guard.json")["software"][TICKER]
        self.assertEqual(sw["stop"], 358.32)
        self.assertTrue(any("TSLA: Trading 212 refused the safety net" in p for p in self.pushed))
        self.assertTrue(any("ENFORCES IT IN SOFTWARE" in n[1] for n in self.notes))

    def test_a_stop_gone_from_the_list_is_looked_up_in_the_history(self):
        self.account()
        self.router.routes[("GET", "/equity/orders")] = [FakeResp([]), FakeResp([])]
        self.router.add("GET", "/equity/history/orders", history(dict(REJECTED, id=5002)))
        code, _, err = self.place()
        self.assertEqual(code, 3)
        self.assertIn("REJECTED", err)

    def test_fractional_stops_software_sends_no_stop_order_for_a_fractional_size(self):
        self.write("config.json", dict(self.cfg, fractional_stops="software", fractional=True))
        self.account(filled=[{"instrument": {"ticker": TICKER}, "quantity": 7.35,
                              "quantityAvailableForTrading": 7.35, "currentPrice": 366.0}])
        self.set_tickets([test_t212_place.tsla_ticket(qty="7.35")])
        code, out, err = self.place()
        self.assertEqual(code, 0, err)
        self.assertEqual([(p[1], p[2]["quantity"]) for p in self.router.posts()], [("/equity/orders/market", 7.2)])
        self.assertIn("enforced by t212.py guard (fractional_stops software)", out)     # the preview says so too
        self.assertNotIn("POST /equity/orders/stop", out)
        self.assertEqual(self.broker_log()[-1]["status"], "software_stop")

    def test_a_bad_fractional_stops_value_is_refused(self):
        with self.assertRaises(t212.T212Error):
            t212.fractional_stops({"fractional_stops": "sometimes"})


class GuardChecks(Base):
    def setUp(self):
        super().setUp()
        self.write("state/broker.json", {"orders": [entry_record(status="stop_placed")]})

    def guard(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = t212.main(["guard"] + list(extra) + ["--state-dir", self.dir], environ=CREDS)
        return code, out.getvalue(), err.getvalue()

    def track(self, oid=7001):
        self.write("state/t212-guard.json", {"verify": {TICKER: {"id": oid, "fingerprint": "abc123",
                                                                 "stop": 358.32, "placed_at": 0}}})

    def test_a_rejected_stop_found_on_a_cycle_goes_software_and_sells_at_the_stop(self):
        self.track()
        r = self.router
        r.add("GET", "/equity/positions", FakeResp([position(price=366.0)]))
        r.add("GET", "/equity/orders", FakeResp([]))
        r.add("GET", "/equity/history/orders", history(REJECTED))
        code, text, _ = self.guard()
        self.assertEqual(code, 0)
        self.assertEqual(self.router.posts(), [])                   # above the stop: it only watches
        self.assertIn("software stop", text)
        self.assertTrue(any("TSLA: Trading 212 refused the safety net" in p for p in self.pushed))
        self.assertIn(TICKER, self.read("state/t212-guard.json")["software"])
        r.routes.clear()
        r.add("GET", "/equity/positions", FakeResp([position(price=358.0)]))
        r.add("GET", "/equity/orders", FakeResp([]))
        r.add("POST", "/equity/orders/market", FakeResp({"id": 9500}))
        code, text, _ = self.guard()
        self.assertEqual(self.router.posts()[0][2], {"ticker": TICKER, "quantity": -7, "extendedHours": False})
        self.assertIn("SOLD AT STOP", text)
        self.assertEqual(self.router.posts("/equity/orders/stop"), [])   # never a stop order on top

    def test_a_rejected_stop_still_on_the_list_does_not_count_as_cover(self):
        self.track()
        self.router.add("GET", "/equity/positions", FakeResp([position()]))
        self.router.add("GET", "/equity/orders", FakeResp([dict(stop_order(), status="REJECTED")]))
        code, text, _ = self.guard()
        self.assertIn("software stop", text)
        self.assertTrue(any("Trading 212 refused the safety net" in p for p in self.pushed))
        self.assertEqual(self.router.posts(), [])

    def test_a_working_stop_needs_no_history_call(self):
        self.track()
        self.router.add("GET", "/equity/positions", FakeResp([position()]))
        self.router.add("GET", "/equity/orders", FakeResp([stop_order()]))
        code, text, _ = self.guard()
        self.assertIn("covered", text)
        self.assertNotIn("/equity/history/orders", [c[1] for c in self.router.calls])
        self.assertEqual(self.pushed, [])

    def test_a_stop_placed_before_checks_existed_is_tracked_from_the_guard_state(self):
        self.write("state/t212-guard.json", {"placed": [{"ticker": TICKER, "order_id": 7001, "stop": 358.32}]})
        self.router.add("GET", "/equity/positions", FakeResp([position()]))
        self.router.add("GET", "/equity/orders", FakeResp([stop_order()]))
        self.guard()
        self.assertEqual(self.read("state/t212-guard.json")["verify"][TICKER]["id"], 7001)

    def test_a_filled_stop_is_simply_forgotten(self):
        self.track()
        filled = dict(REJECTED, status="FILLED", filledQuantity=7)
        self.router.add("GET", "/equity/positions", FakeResp([position(qty=7)]))
        self.router.add("GET", "/equity/orders", FakeResp([]))
        self.router.add("GET", "/equity/history/orders", history(filled))
        self.router.add("POST", "/equity/orders/stop", FakeResp({"id": 9001}))
        self.guard()
        self.assertNotIn(TICKER, (self.read("state/t212-guard.json") or {}).get("software", {}))
        self.assertEqual(self.pushed, [])

    def test_a_new_guard_stop_is_tracked_and_rechecked_in_a_minute(self):
        self.write("state/broker.json", {"orders": [entry_record(status="stop_not_placed")]})
        self.clock[0] = float(calendar.timegm((2026, 11, 5, 15, 0, 0)))          # 10:00 New York
        r = self.router
        r.add("GET", "/equity/positions", FakeResp([position()]))
        r.add("GET", "/equity/orders", FakeResp([]), FakeResp([dict(stop_order(), id=9001)]))
        r.add("POST", "/equity/orders/stop", FakeResp({"id": 9001, "status": "NEW"}))
        code, text, _ = self.guard("--watch", "--every", "5", "--until", "10:03")
        self.assertEqual(len(self.router.posts("/equity/orders/stop")), 1)
        self.assertIn(60.0, self.sleeps)                                       # the next look is a minute later
        self.assertEqual(self.read("state/t212-guard.json")["verify"][TICKER]["id"], 9001)

    def test_a_refused_guard_stop_goes_software_at_once(self):
        self.write("state/broker.json", {"orders": [entry_record(status="stop_not_placed")]})
        self.router.add("GET", "/equity/positions", FakeResp([position()]))
        self.router.add("GET", "/equity/orders", FakeResp([]))
        self.router.add("POST", "/equity/orders/stop", http_error(400, "rejected"))
        self.guard()
        self.assertIn(TICKER, self.read("state/t212-guard.json")["software"])
        self.assertTrue(any("Trading 212 refused the safety net" in p for p in self.pushed))

    def test_fractional_stops_software_watches_a_fractional_position(self):
        self.write("config.json", dict(self.cfg, fractional_stops="software"))
        self.write("state/broker.json", {"orders": [entry_record(qty="10.51", status="software_stop")]})
        self.router.add("GET", "/equity/positions", FakeResp([position(qty=10.51)]))
        self.router.add("GET", "/equity/orders", FakeResp([]))
        code, text, _ = self.guard()
        self.assertEqual(self.router.posts(), [])
        self.assertIn("software stop", text)

    def test_a_rejected_order_is_no_cover(self):
        s, o = t212.guard_sell_orders([dict(stop_order(), status="REJECTED"), stop_order()], TICKER)
        self.assertEqual(len(s), 1)


if __name__ == "__main__":
    unittest.main()
