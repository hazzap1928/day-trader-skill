"""The stop guardian (t212.py guard): demo only, puts back a ticket's missing stop,
and never buys. A fake Trading 212 records every request it gets."""

import calendar
import io
import json
import os
import unittest
from contextlib import redirect_stdout, redirect_stderr

import test_t212_place
from test_t212_place import Base, FakeResp, CREDS, TODAY, http_error

t212 = test_t212_place.t212      # the same module Base fakes the network and the clock on

TICKER = "TSLA_US_EQ"


def position(qty=7, price=366.0, ticker=TICKER):
    return {"instrument": {"ticker": ticker}, "quantity": qty, "quantityAvailableForTrading": qty,
            "currentPrice": price}


def stop_order(qty=-7, stop=358.32, ticker=TICKER, typ="STOP"):
    return {"id": 7001, "ticker": ticker, "type": typ, "quantity": qty, "stopPrice": stop,
            "status": "NEW", "timeValidity": "GOOD_TILL_CANCEL"}


def entry_record(qty="7", stop=358.32, status="stop_not_placed", env="demo", ticker=TICKER):
    """What t212.py place writes to trader/state/broker.json for an entry."""
    return {"date": TODAY, "index": 1, "fingerprint": "abc123", "symbol": "TSLA", "side": "buy", "qty": qty,
            "kind": "entry", "broker": "trading212", "env": env, "t212_ticker": ticker, "stop": stop,
            "status": status, "at": "2026-11-05T10:30:00-05:00"}


class Guard(Base):
    def setUp(self):
        super().setUp()
        self.write("state/broker.json", {"orders": [entry_record()]})

    def set_account(self, positions, pending=(), stop_reply=None):
        r = self.router
        r.add("GET", "/equity/positions", FakeResp(list(positions)))
        r.add("GET", "/equity/orders", FakeResp(list(pending)))
        r.add("POST", "/equity/orders/stop",
              stop_reply or FakeResp({"id": 9001, "status": "NEW", "type": "STOP", "quantity": -7}))

    def guard(self, *extra, environ=CREDS):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = t212.main(["guard"] + list(extra) + ["--state-dir", self.dir], environ=environ)
        return code, out.getvalue(), err.getvalue()

    def assert_never_bought(self):
        self.assertEqual(self.router.posts("/equity/orders/market"), [])
        for _, path, body, _, _ in self.router.posts():
            self.assertEqual(path, "/equity/orders/stop")
            self.assertLess(body["quantity"], 0)

    def test_place_sends_a_good_till_cancelled_stop(self):
        self.assertEqual(t212.stop_body(TICKER, 7, 358.32)["timeValidity"], "GOOD_TILL_CANCEL")

    def test_a_missing_stop_is_put_back(self):
        self.set_account([position()])
        code, text, err = self.guard()
        self.assertEqual(code, 0, err)
        posts = self.router.posts()
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][2], {"ticker": TICKER, "quantity": -7, "stopPrice": 358.32,
                                       "timeValidity": "GOOD_TILL_CANCEL"})
        self.assertIn("stop placed: SELL 7 at 358.32, good till cancelled", text)
        self.assertEqual(self.read("state/t212-guard.json")["placed"][0]["order_id"], 9001)
        self.assert_never_bought()
        self.assert_no_secrets(text, err)

    def test_a_covered_position_is_left_alone(self):
        self.set_account([position()], [stop_order()])
        code, text, _ = self.guard()
        self.assertEqual(code, 0)
        self.assertEqual(self.router.posts(), [])
        self.assertIn("covered", text)

    def test_a_part_covered_position_gets_only_the_rest(self):
        self.set_account([position(qty=10.52)], [stop_order(qty=-4)])
        self.write("state/broker.json", {"orders": [entry_record(qty="10.52")]})
        self.guard()
        self.assertEqual(self.router.posts()[0][2]["quantity"], -6.52)

    def test_never_more_than_the_ticket_bought(self):
        self.set_account([position(qty=12)])          # 5 of these were not bought by the ticket
        self.guard()
        self.assertEqual(self.router.posts()[0][2]["quantity"], -7)

    def test_no_ticket_no_stop_invented(self):
        self.write("state/broker.json", {"orders": []})
        self.set_account([position()])
        code, text, _ = self.guard()
        self.assertEqual(self.router.posts(), [])
        self.assertIn("no t212.py place ticket with a stop", text)

    def test_a_live_ticket_record_does_not_count_on_demo(self):
        self.write("state/broker.json", {"orders": [entry_record(env="live")]})
        self.set_account([position()])
        self.guard()
        self.assertEqual(self.router.posts(), [])

    def test_at_or_under_the_stop_it_does_not_sell_at_once(self):
        self.set_account([position(price=358.0)])
        code, text, _ = self.guard()
        self.assertEqual(self.router.posts(), [])
        self.assertIn("would sell at once", text)

    def test_a_pending_sell_means_no_stop(self):
        self.set_account([position()], [stop_order(typ="MARKET")])
        code, text, _ = self.guard()
        self.assertEqual(self.router.posts(), [])
        self.assertIn("exit pending", text)

    def test_an_unclear_stop_is_never_sent_twice(self):
        self.set_account([position()], stop_reply=http_error(503))
        code, text, _ = self.guard()
        self.assertIn("UNCLEAR", text)
        self.assertEqual(len(self.router.posts()), 1)
        self.router.add("POST", "/equity/orders/stop", FakeResp({"id": 9002}))
        code, text, _ = self.guard()
        self.assertEqual(len(self.router.posts()), 1)
        self.assertIn("Not sent again", text)

    def test_a_refused_stop_is_reported(self):
        self.set_account([position()], stop_reply=http_error(400, "bad stop"))
        code, text, _ = self.guard()
        self.assertIn("was refused", text)
        self.assertIsNone((self.read("state/t212-guard.json") or {}).get("unclear", {}).get(TICKER))

    def test_an_orphan_stop_is_reported_not_cancelled(self):
        self.set_account([], [stop_order()])
        code, text, _ = self.guard()
        self.assertEqual(self.router.posts(), [])
        self.assertNotIn("DELETE", [c[0] for c in self.router.calls])
        self.assertIn("you hold none", text)

    def test_it_cannot_send_a_buy(self):
        saved = t212.stop_body
        t212.stop_body = lambda ticker, qty, stop: {"ticker": ticker, "quantity": abs(qty), "stopPrice": stop,
                                                    "timeValidity": "GOOD_TILL_CANCEL"}
        try:
            self.set_account([position()])
            code, text, err = self.guard()
        finally:
            t212.stop_body = saved
        self.assertEqual(code, 2)
        self.assertIn("a stop must sell", err)
        self.assertEqual(self.router.posts(), [])

    def test_live_is_refused_before_any_request(self):
        code, _, err = self.guard("--live", environ=dict(CREDS, T212_ENV="live"))
        self.assertEqual(code, 2)
        self.assertIn("demo account only", err)
        self.assertEqual(self.router.calls, [])

    def test_watch_waits_for_the_open_and_stops_at_until(self):
        self.clock[0] = float(calendar.timegm((2026, 11, 5, 14, 0, 0)))     # 9:00 in New York
        self.set_account([position()], [stop_order()])
        code, text, err = self.guard("--watch", "--every", "60", "--until", "11:00")
        self.assertEqual(code, 0, err)
        self.assertIn("waiting for the open", text)
        self.assertEqual(text.count("stop guard on demo"), 2)                # 9:30 and 10:30
        self.assertIn("past 11:00 New York", text)
        self.assertEqual(self.router.posts(), [])

    def test_watch_does_nothing_on_a_weekend(self):
        self.clock[0] = float(calendar.timegm((2026, 11, 7, 15, 0, 0)))     # Saturday
        code, text, _ = self.guard("--watch")
        self.assertEqual(code, 0)
        self.assertIn("weekend", text)
        self.assertEqual(self.router.calls, [])


if __name__ == "__main__":
    unittest.main()
