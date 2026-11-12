"""Trailing stops: the guard raises a stop (never lowers it), can sell half at the target,
and the Time Machine compares trail distances. Demo only, exits only."""

import gzip
import io
import json
import os
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX
import test_t212_place
from test_t212_place import Base, FakeResp, CREDS, TODAY, http_error

t212 = test_t212_place.t212
tm = load("trader-timemachine", "timemachine")

TICKER = "TSLA_US_EQ"
TSLA = os.path.join(FIX, "trader-timemachine", "TSLA-60d5m.json.gz")
with gzip.open(TSLA, "rt", encoding="utf-8") as _fh:
    TEXT = _fh.read()
PLAN = {"symbol": "TSLA", "side": "buy", "date": "2026-10-19", "setup": "orb", "entry": 364.18,
        "entry_time": "10:15 AM", "entry_bar": 8, "interval": "5m",
        "opening_range": {"high": 362.18, "low": 354.47, "minutes": 15}, "stop": 358.32,
        "stop_rule": "or_mid", "target": 370.04, "target_r": 1.0, "flat_by": "15:55"}
NOW_UTC = tm.parse_now("2026-10-19 10:50")
# entry 364.18, stop 358.32: risk 5.86 a share, target 370.04 (1R)
RECORD = {"date": TODAY, "index": 1, "fingerprint": "fp1", "symbol": "TSLA", "side": "buy", "qty": "7",
          "kind": "entry", "broker": "trading212", "env": "demo", "t212_ticker": TICKER, "stop": 358.32,
          "entry": 364.18, "target": 370.04, "status": "stop_placed", "at": "2026-11-05T10:30:00-05:00"}


def position(price, qty=7):
    return {"instrument": {"ticker": TICKER}, "quantity": qty, "quantityAvailableForTrading": qty,
            "currentPrice": price}


def stop_order(stop=358.32, qty=-7, oid=7001):
    return {"id": oid, "ticker": TICKER, "type": "STOP", "quantity": qty, "stopPrice": stop, "status": "NEW"}


class TrailOutcome(unittest.TestCase):
    E, S, T = 100.0, 98.0, 102.0          # risk 2

    def test_the_plan_row_is_stop_or_target_or_close(self):
        self.assertEqual(tm.trail_outcome([(102.5, 99.0, 101.0)], self.E, self.S, self.T, None, False), (1.0, "target"))
        self.assertEqual(tm.trail_outcome([(101.0, 97.5, 98.5)], self.E, self.S, self.T, None, False), (-1.0, "stop"))
        self.assertEqual(tm.trail_outcome([(101.0, 99.5, 101.0)], self.E, self.S, self.T, None, False), (0.5, "close"))

    def test_the_stop_trails_the_highest_close_and_is_never_lowered(self):
        path = [(104.5, 100.5, 104.0),     # close 104: stop -> 104 - 1R(2) = 102
                (104.2, 102.5, 103.0),     # lower close: stop stays 102
                (103.5, 101.5, 102.0)]     # low 101.5 <= 102: out at 102
        self.assertEqual(tm.trail_outcome(path, self.E, self.S, self.T, 1.0, False), (1.0, "trailed out"))

    def test_half_at_the_target_then_trail_the_rest(self):
        path = [(103.0, 100.5, 102.5),     # close >= 102: sell half at 102.5 (+1.25R on half), stop -> 100.5
                (106.5, 102.0, 106.0),     # stop -> 104
                (106.0, 103.5, 104.0)]     # out at 104 on the other half (+2R on half)
        r, kind = tm.trail_outcome(path, self.E, self.S, self.T, 1.0, True)
        self.assertAlmostEqual(r, 0.5 * 1.25 + 0.5 * 2.0)
        self.assertEqual(kind, "trailed out")

    def test_with_activation_the_trail_waits_for_1r_of_profit(self):
        path = [(101.8, 100.2, 101.5),     # close 101.5 < 1R (102): no raise (without activation: 99.5)
                (101.6, 99.2, 99.6)]       # low 99.2 is over the first stop 98: still in, out at the close
        r, kind = tm.trail_outcome(path, self.E, self.S, self.T, 1.0, False, 1.0)
        self.assertEqual((round(r, 6), kind), (-0.2, "close"))
        self.assertEqual(tm.trail_outcome(path, self.E, self.S, self.T, 1.0, False, 0.0)[1], "trailed out")
        path = [(102.5, 100.5, 102.0),     # close 102 = 1R: the stop moves to 102 - 2 = 100 (break-even)
                (101.0, 99.5, 100.5)]      # low 99.5 <= 100: out at the entry
        self.assertEqual(tm.trail_outcome(path, self.E, self.S, self.T, 1.0, False, 1.0), (0.0, "trailed out"))

    def test_a_full_loss_is_minus_one_r(self):
        self.assertEqual(tm.trail_outcome([(100.2, 97.0, 97.5)], self.E, self.S, self.T, 0.5, True), (-1.0, "stop"))


class TrailStudy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = tm.trail_study(TEXT, tm.normalize_plan(PLAN), NOW_UTC, half=True, replays=2000, until="15:55")

    def test_the_plan_row_matches_run_exactly(self):
        run = tm.time_machine(TEXT, tm.normalize_plan(PLAN), NOW_UTC, replays=2000, until="15:55")
        plan_row = self.doc["rows"][0]
        self.assertEqual(plan_row["mode"], "plan: fixed target")
        self.assertEqual(plan_row["mc"]["avg_R"], run["mc"]["avg_R"])
        self.assertEqual(plan_row["history"]["avg_R"], run["history"]["avg_R"])
        self.assertEqual(plan_row["history"]["n"], run["history"]["triggered"])

    def test_every_distance_and_mode_is_compared_on_the_same_trades(self):
        rows = self.doc["rows"]
        self.assertEqual(len(rows), 1 + 2 * len(tm.TRAIL_DISTANCES))
        self.assertEqual({r["mc"]["n"] for r in rows}, {2000})
        self.assertEqual({r["history"]["n"] for r in rows}, {self.doc["history_trades"]})

    def test_the_suggestion_is_the_best_smoothed_score_and_ready_for_config(self):
        sg = self.doc["suggestion"]
        cands = [r for r in self.doc["rows"] if "score" in r]
        self.assertEqual(sg["score"], max(r["score"] for r in cands))
        self.assertIn(sg["distance_r"], tm.TRAIL_DISTANCES)
        self.assertEqual(sg["config"]["trailing_stop"]["distance_r"], sg["distance_r"])
        self.assertEqual(sg["config"]["trailing_stop"]["activate_r"], 1.0)     # the guard's default
        self.assertEqual(self.doc["basis"], "history")          # 33 real trades >= 15

    def test_same_seed_same_numbers(self):
        again = tm.trail_study(TEXT, tm.normalize_plan(PLAN), NOW_UTC, half=True, replays=2000, until="15:55")
        self.assertEqual(again["rows"], self.doc["rows"])

    def test_report_and_refusals(self):
        text = "\n".join(tm.trail_report(self.doc))
        self.assertIn("<- suggested", text)
        self.assertIn('"trailing_stop": {"enabled": true', text)
        with self.assertRaises(tm.TMError):
            tm.trail_study(TEXT, tm.normalize_plan(PLAN), NOW_UTC, distances=[0], replays=10)


class Plateau(unittest.TestCase):
    def rows(self, avgs):
        return [{"distance_r": d, "half": False, "history": {"avg_R": a}} for d, a in avgs]

    def test_a_flat_widest_stretch_is_called_no_trail(self):
        rows = self.rows([(1, 0.1), (2, 0.3), (3, 0.3), (4, 0.3)])
        self.assertEqual(tm._plateau(rows, rows[3], "history"), 2)

    def test_a_best_that_still_rises_is_not_a_plateau(self):
        rows = self.rows([(1, 0.1), (2, 0.2), (3, 0.3)])
        self.assertIsNone(tm._plateau(rows, rows[2], "history"))


class GuardTrail(Base):
    def setUp(self):
        super().setUp()
        self.write("state/broker.json", {"orders": [RECORD]})
        self.set_trail(distance_r=1.0)

    def set_trail(self, **t):
        cfg = dict(self.cfg, trailing_stop=dict({"enabled": True}, **t))
        self.write("config.json", cfg)

    def set_account(self, price, pending=None, after_cancel=(), stop_reply=None, market_reply=None, qty=7):
        r = self.router
        r.add("GET", "/equity/positions", FakeResp([position(price, qty)]))
        r.add("GET", "/equity/orders", FakeResp(pending if pending is not None else [stop_order()]),
              FakeResp(list(after_cancel)))
        r.add("DELETE", "/equity/orders/7001", FakeResp({}))
        for reply in (stop_reply if isinstance(stop_reply, list) else [stop_reply]):
            r.add("POST", "/equity/orders/stop", reply or FakeResp({"id": 8001, "status": "NEW"}))
        r.add("POST", "/equity/orders/market", market_reply or FakeResp({"id": 8100, "status": "NEW", "side": "SELL"}))

    def guard(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = t212.main(["guard", "--state-dir", self.dir], environ=CREDS)
        return code, out.getvalue(), err.getvalue()

    def calls(self, method):
        return [c for c in self.router.calls if c[0] == method]

    def assert_exits_only(self):
        for _, path, body, _, _ in self.router.posts():
            self.assertLess(body["quantity"], 0, body)

    def test_a_new_high_raises_the_stop(self):
        self.set_account(372.0)                 # high 372 - 1R (5.86) = 366.14
        code, text, err = self.guard()
        self.assertEqual(code, 0, err)
        self.assertEqual([c[1] for c in self.calls("DELETE")], ["/equity/orders/7001"])
        posts = self.router.posts("/equity/orders/stop")
        self.assertEqual(posts[0][2], {"ticker": TICKER, "quantity": -7, "stopPrice": 366.14,
                                       "timeValidity": "GOOD_TILL_CANCEL"})
        self.assertIn("RAISED the stop 358.32 -> 366.14", text)
        self.assertEqual(self.read("state/t212-guard.json")["trail"][TICKER]["stop"], 366.14)
        self.assert_exits_only()

    def test_no_raise_until_the_trade_is_1r_in_profit(self):
        # entry 364.18, risk 5.86: 1R of profit is 370.04. At 368 the old trail would have raised to 362.14
        self.set_account(368.0)
        code, text, err = self.guard()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.router.posts(), [])
        self.assertEqual(self.calls("DELETE"), [])
        self.assertIn("the trail starts at 1R profit (370.04), high so far 368.00", text)
        self.assertEqual(self.read("state/t212-guard.json")["trail"][TICKER]["high"], 368.0)

    def test_at_1r_of_profit_the_first_raise_is_to_break_even(self):
        self.set_account(370.04)
        self.guard()
        self.assertEqual(self.router.posts("/equity/orders/stop")[0][2]["stopPrice"], 364.18)

    def test_the_gddy_case_three_small_raises_under_1r_never_happen(self):
        # GDDY 10 Nov: stop 112.11, risk 1.17 (1R at 114.45); highs 113.43, 113.58, 113.85 each raised it
        rec = dict(RECORD, entry=113.28, stop=112.11, target=115.62, qty="44.5")
        self.write("state/broker.json", {"orders": [rec]})
        for high in (113.43, 113.58, 113.85):
            self.router.routes.clear()
            self.set_account(high, pending=[stop_order(stop=112.11, qty=-44.5)], qty=44.5)
            self.guard()
        self.assertEqual(self.router.posts(), [])
        self.assertEqual(self.calls("DELETE"), [])

    def test_activate_r_zero_trails_from_the_entry_as_before(self):
        self.set_trail(distance_r=1.0, activate_r=0)
        self.set_account(368.0)                 # 368 - 5.86 = 362.14
        self.guard()
        self.assertEqual(self.router.posts("/equity/orders/stop")[0][2]["stopPrice"], 362.14)

    def test_a_raise_is_tracked_so_the_next_check_finds_the_new_stop(self):
        # place tracks the first stop (7001); the raise cancels it and places 8001
        self.write("state/t212-guard.json", {"verify": {TICKER: {"id": 7001, "fingerprint": "fp1",
                                                                 "stop": 358.32, "placed_at": 0}}})
        self.set_account(372.0)
        code, text, err = self.guard()
        self.assertEqual(code, 0, err)
        self.assertIn("RAISED the stop 358.32 -> 366.14", text)
        st = self.read("state/t212-guard.json")
        self.assertEqual(st["verify"][TICKER]["id"], 8001)
        self.assertEqual(st["verify"][TICKER]["stop"], 366.14)
        # 5 minutes later: Trading 212 lists 8001, and 7001 is CANCELLED in the history (by the guard)
        self.router.routes.clear()
        self.set_account(372.0, pending=[stop_order(stop=366.14, oid=8001)])
        self.router.add("GET", "/equity/history/orders", FakeResp({"items": [{"order": dict(
            stop_order(), status="CANCELLED")}], "nextPagePath": None}))
        code, text, err = self.guard()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.pushed, [])
        self.assertEqual(self.notes, [])
        self.assertNotIn("GONE", text + err)
        st = self.read("state/t212-guard.json")
        self.assertNotIn(TICKER, st.get("software", {}))
        self.assertNotIn("/equity/history/orders", [c[1] for c in self.router.calls])   # no lookup needed

    def test_an_old_tracked_id_the_guard_cancelled_itself_is_not_an_alarm(self):
        # state written by the old code: verify still on 7001, which the guard cancelled to raise it
        self.write("state/t212-guard.json", {
            "verify": {TICKER: {"id": 7001, "fingerprint": "fp1", "stop": 358.32, "placed_at": 0}},
            "cancelled": [{"at": "x", "ticker": TICKER, "order_id": 7001}],
            "trail": {TICKER: {"fingerprint": "fp1", "high": 372.0, "stop": 366.14, "half_sold": False}}})
        self.set_account(372.0, pending=[stop_order(stop=366.14, oid=8001)])
        self.router.add("GET", "/equity/history/orders", FakeResp({"items": [{"order": dict(
            stop_order(), status="CANCELLED")}], "nextPagePath": None}))
        code, text, err = self.guard()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.pushed, [])
        st = self.read("state/t212-guard.json")
        self.assertEqual(st["verify"][TICKER]["id"], 8001)
        self.assertNotIn(TICKER, st.get("software", {}))

    def test_a_stop_cancelled_by_someone_else_still_raises_the_alarm(self):
        self.write("state/t212-guard.json", {"verify": {TICKER: {"id": 7001, "fingerprint": "fp1",
                                                                 "stop": 358.32, "placed_at": 0}}})
        self.set_account(368.0, pending=[])
        self.router.add("GET", "/equity/history/orders", FakeResp({"items": [{"order": dict(
            stop_order(), status="CANCELLED")}], "nextPagePath": None}))
        self.guard()
        self.assertTrue(any("Trading 212 dropped the safety net" in p for p in self.pushed))
        self.assertIn(TICKER, self.read("state/t212-guard.json")["software"])

    def test_the_guards_own_cancels_are_remembered(self):
        self.set_account(372.0)
        self.guard()
        own = self.read("state/t212-guard.json")["cancelled"]
        self.assertEqual([(x["ticker"], x["order_id"]) for x in own], [(TICKER, 7001)])

    def test_it_never_lowers_the_stop(self):
        self.write("state/t212-guard.json", {"trail": {TICKER: {"fingerprint": "fp1", "high": 372.0,
                                                                "stop": 366.14, "half_sold": False}}})
        self.set_account(367.0, pending=[stop_order(stop=366.14)])
        code, text, _ = self.guard()
        self.assertEqual(self.router.posts(), [])
        self.assertEqual(self.calls("DELETE"), [])
        self.assertIn("no raise yet", text)

    def test_a_small_rise_does_not_churn_orders(self):
        self.set_account(364.5)                 # trail 358.64, only 0.32 over the stop (< 0.1R)
        self.guard()
        self.assertEqual(self.router.posts(), [])
        self.assertEqual(self.calls("DELETE"), [])

    def test_a_refused_raise_puts_the_old_stop_back(self):
        self.set_account(372.0, stop_reply=[http_error(400, "bad"), FakeResp({"id": 8002})])
        code, text, _ = self.guard()
        prices = [c[2]["stopPrice"] for c in self.router.posts("/equity/orders/stop")]
        self.assertEqual(prices, [366.14, 358.32])
        self.assertIn("was put back at 358.32", text)

    def test_an_unclear_raise_is_not_followed_by_anything(self):
        self.set_account(372.0, stop_reply=[http_error(503)])
        code, text, _ = self.guard()
        self.assertEqual(len(self.router.posts()), 1)
        self.assertIn("UNCLEAR", text)
        self.assertIn(TICKER, self.read("state/t212-guard.json")["unclear"])

    def test_half_at_the_target_then_trail_the_rest(self):
        self.set_trail(distance_r=1.0, sell_half_at_target=True)
        self.set_account(371.0)                 # >= target 370.04; trail 371 - 5.86 = 365.14
        code, text, err = self.guard()
        self.assertEqual(code, 0, err)
        market = self.router.posts("/equity/orders/market")
        self.assertEqual(market[0][2]["quantity"], -3)                    # half of 7, whole shares
        stops = self.router.posts("/equity/orders/stop")
        self.assertEqual((stops[0][2]["quantity"], stops[0][2]["stopPrice"]), (-4, 365.14))
        self.assertIn("SOLD HALF (3) at the target", text)
        order = [c[0] for c in self.router.calls if c[0] in ("DELETE", "POST")]
        self.assertEqual(order, ["DELETE", "POST", "POST"])               # cancel, sell half, stop the rest
        self.assertTrue(self.read("state/t212-guard.json")["trail"][TICKER]["half_sold"])
        self.assert_exits_only()

    def test_half_is_sold_only_once(self):
        self.set_trail(distance_r=1.0, sell_half_at_target=True)
        self.write("state/t212-guard.json", {"trail": {TICKER: {"fingerprint": "fp1", "high": 371.0,
                                                                "stop": 365.14, "half_sold": True}}})
        self.set_account(371.5, pending=[stop_order(stop=365.14, qty=-4)], qty=4)
        self.guard()
        self.assertEqual(self.router.posts("/equity/orders/market"), [])

    def test_a_missing_stop_comes_back_at_the_trailed_level_not_lower(self):
        self.write("state/t212-guard.json", {"trail": {TICKER: {"fingerprint": "fp1", "high": 372.0,
                                                                "stop": 366.14, "half_sold": False}}})
        self.set_account(369.0, pending=[])
        self.guard()
        self.assertEqual(self.router.posts("/equity/orders/stop")[0][2]["stopPrice"], 366.14)

    def test_bad_settings_are_refused(self):
        for bad in ({"distance_r": 0}, {"distance_r": 1, "distance_pct": 2}, {},
                    {"distance_r": 1, "activate_r": -1}, {"distance_r": 1, "activate_r": "soon"}):
            with self.subTest(bad=bad):
                self.set_trail(**bad)
                code, _, err = self.guard()
                self.assertEqual(code, 2)
                self.assertIn("trailing_stop", err)

    def test_off_by_default(self):
        self.write("config.json", self.cfg)
        self.set_account(380.0)
        self.guard()
        self.assertEqual(self.router.calls[-1][0], "GET")
        self.assertEqual(self.router.posts(), [])

    def test_place_records_entry_and_target_for_the_trail(self):
        self.write("state/broker.json", {"orders": []})
        self.account()
        code, _, err = self.place()
        self.assertEqual(code, 0, err)
        rec = [r for r in self.broker_log() if r.get("status") == "placed"][0]
        self.assertEqual((rec["entry"] > 0, rec["target"]), (True, 370.04))


if __name__ == "__main__":
    unittest.main()
