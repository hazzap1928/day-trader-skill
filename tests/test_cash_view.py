"""11 Nov 2026 run: after BBY took about half the demo account, BDX, CBOE, CDW and a dozen more passed
trader-budget and trader-risk and were refused by Trading 212 for insufficient funds. Both checks read
trader/portfolio.json, which the autopilot never wrote, so each buy was checked against an empty account
funded with the whole budget. The autopilot now writes a snapshot of the real account (read-only, from
Trading 212) to trader/state/portfolio.json before sizing, and the free cash no longer counts a pending
buy twice when Trading 212 already holds its cash back. Fakes only: no network, no real order, no phone."""

import json
import os
import unittest

from _load import load
from test_t212_place import FakeResp
from test_autopilot import PilotBase, ap, iso, NOW
from test_risk import at, bracket, v1

t212 = ap.t212
check = load("trader-risk", "check")
size = load("trader-budget", "size")

RATE = 1.3273
# the demo account at 10:05 New York on 11 Nov, after the BBY buy (made-up round figures)
AFTER_BBY = {"currency": "GBP", "totalValue": 5000.0,
             "cash": {"availableToTrade": 2250.0, "reservedForOrders": 0, "inPies": 0}}
BBY_HELD = [{"instrument": {"ticker": "BBY_US_EQ"}, "quantity": 34.2, "currentPrice": 105.20}]
CHTR_WAITING = {"id": 9, "ticker": "CHTR_US_EQ", "type": "MARKET", "side": "BUY", "quantity": 20.65,
                "filledQuantity": 0, "status": "NEW"}
CHTR_RECORD = {"orders": [{"side": "buy", "t212_ticker": "CHTR_US_EQ", "entry": 134.89}]}


class Snapshot(unittest.TestCase):
    def test_cash_is_free_cash_before_the_buffer_in_dollars_and_positions_are_valued(self):
        doc = t212.portfolio_snapshot(AFTER_BBY, [], BBY_HELD, RATE)
        self.assertEqual(doc["currency"], "USD")
        self.assertEqual(doc["cash"], int(2250.0 * RATE * 100) / 100.0)          # rounded down, never up
        self.assertEqual(doc["positions"], [{"ticker": "BBY", "shares": 34.2,
                                             "lots": [{"shares": 34.2, "cost": round(34.2 * 105.20, 2)}]}])

    def test_a_buy_waiting_to_fill_is_taken_off_the_cash_and_counted_as_a_position(self):
        doc = t212.portfolio_snapshot(AFTER_BBY, [CHTR_WAITING], BBY_HELD, RATE, CHTR_RECORD)
        waiting = 20.65 * 134.89 / RATE * (1 + t212.FX_FEE)
        self.assertAlmostEqual(doc["cash_gbp"], 2250.0 - waiting, places=1)
        self.assertEqual([p["ticker"] for p in doc["positions"]], ["BBY", "CHTR"])
        self.assertEqual(doc["positions"][1]["lots"][0]["cost"], round(20.65 * 134.89, 2))

    def test_cash_trading_212_already_holds_back_is_not_taken_off_twice(self):
        waiting = round(20.65 * 134.89 / RATE * (1 + t212.FX_FEE), 2)
        held_back = dict(AFTER_BBY, cash=dict(AFTER_BBY["cash"], reservedForOrders=waiting))
        fc = t212.free_cash(held_back, [CHTR_WAITING], RATE, {"budget_gbp": 5000.0, "cash_buffer_pct": 10},
                            CHTR_RECORD)
        self.assertEqual(fc["waiting"], 0.0)
        self.assertEqual(fc["available"], round(2250.0 - 500.0, 2))
        fc = t212.free_cash(AFTER_BBY, [CHTR_WAITING], RATE, {"budget_gbp": 5000.0, "cash_buffer_pct": 10},
                            CHTR_RECORD)
        self.assertEqual(fc["waiting"], waiting)                                    # nothing held back: all of it
        doc = t212.portfolio_snapshot(held_back, [CHTR_WAITING], BBY_HELD, RATE, CHTR_RECORD)
        self.assertEqual(doc["cash_gbp"], 2250.0)

    def test_reservations_for_other_stocks_are_not_free(self):
        doc = t212.portfolio_snapshot(AFTER_BBY, [], BBY_HELD, RATE, reserved=1000.0)
        self.assertEqual(doc["cash_gbp"], 1250.0)

    def test_an_unknown_waiting_price_or_no_rate_refuses_rather_than_guesses(self):
        with self.assertRaises(t212.T212Error):
            t212.portfolio_snapshot(AFTER_BBY, [CHTR_WAITING], BBY_HELD, RATE, {})
        with self.assertRaises(t212.T212Error):
            t212.portfolio_snapshot(AFTER_BBY, [], BBY_HELD, None)


class TheElevenNovemberBuys(unittest.TestCase):
    """BDX at 10:04: 13.77 shares at $213.61, about $2,941, with a little over that free, before the buffer."""

    BDX = bracket("BDX", 13.77, 213.61, 210.95, 218.93)
    CFG = dict(v1(budget=round(5000.0 * RATE, 2), max_order_usd=None, max_position_pct=75), fractional=True,
               broker="trading212")

    def risk(self, portfolio, cfg=None):
        return check.check([self.BDX], cfg or self.CFG, portfolio=portfolio, prices={"BDX": 213.61},
                           now=at("2026-11-11 10:05"))["results"][0]

    def test_without_the_snapshot_trader_risk_saw_an_empty_account_and_passed_it(self):
        self.assertEqual(self.risk(None)["verdict"], "PASS")

    def test_with_the_snapshot_trader_risk_blocks_it_for_cash(self):
        res = self.risk(t212.portfolio_snapshot(AFTER_BBY, [], BBY_HELD, RATE))
        self.assertEqual(res["verdict"], "BLOCK")
        self.assertTrue(any(r.startswith("cash: ") for r in res["reasons"]), res["reasons"])

    def test_the_position_cap_now_binds_on_the_whole_account(self):
        snap = t212.portfolio_snapshot(dict(AFTER_BBY, cash={"availableToTrade": 5000.0}), [], [], RATE)
        res = self.risk(snap, dict(self.CFG, max_position_pct=22))
        self.assertEqual(res["verdict"], "BLOCK")
        self.assertTrue(any("over your 22% cap" in r for r in res["reasons"]), res["reasons"])

    def test_trader_budget_sizes_to_the_real_cash(self):
        plan = {"symbol": "BDX", "side": "buy", "entry": 213.61, "stop": 210.95, "target": 218.93}
        cfg = dict(self.CFG, max_loss_per_trade_usd=65.0, min_reward_risk=1.5)
        empty = size.size_trade(plan, cfg)
        cash = t212.portfolio_snapshot(dict(AFTER_BBY, cash={"availableToTrade": 900.0}), [], BBY_HELD, RATE)["cash"]
        real = size.size_trade(plan, cfg, cash=cash)
        self.assertLess(real["qty"] * 213.61, cash - 5000.0 * RATE * 0.10 + 0.01)    # inside cash minus the buffer
        self.assertLess(real["qty"], empty["qty"])


class PilotWritesIt(PilotBase):
    def setUp(self):
        super().setUp()
        ap.Pilot.write_portfolio = self._wp                       # the real one (PilotBase turns it off)

    def test_it_writes_the_snapshot_trader_budget_and_trader_risk_read(self):
        r = self.router
        r.add("GET", "/equity/account/summary", FakeResp(AFTER_BBY))
        r.add("GET", "/equity/orders", FakeResp([]))
        r.add("GET", "/equity/positions", FakeResp(BBY_HELD))
        p = self.pilot()
        doc = p.write_portfolio("TSLA")
        on_disk = self.read("state/portfolio.json")
        self.assertEqual(on_disk, doc)
        self.assertEqual(doc["cash_gbp"], 2250.0)
        self.assertEqual(doc["usd_per_gbp"], 1.25)                               # the autopilot's fx.json rate
        self.assertEqual(doc["as_of"], iso(NOW))
        self.assertEqual([c[1] for c in r.calls if c[0] != "GET"], [])          # read-only

    def test_the_pipeline_passes_the_snapshot_to_both(self):
        self.router.add("GET", "/equity/account/summary", FakeResp(AFTER_BBY))
        self.router.add("GET", "/equity/orders", FakeResp([]))
        self.router.add("GET", "/equity/positions", FakeResp([]))
        p = self.pilot()
        p.build("TSLA", p.p("config.json"), p.p("fx.json"))
        want = os.path.join(self.dir, "state", "portfolio.json")
        for step in ("size.py", "check.py"):
            args = [c for c in self.pipe.calls if c[0].endswith(step)][0]
            self.assertEqual(args[args.index("--portfolio") + 1], want, step)

    def test_a_failed_read_removes_an_old_snapshot_and_says_so(self):
        self.write("state/portfolio.json", {"cash": 99999})
        self.router.add("GET", "/equity/account/summary", FakeResp({"error": "x"}, status=500))
        p = self.pilot()
        self.assertIsNone(p.write_portfolio("TSLA"))
        self.assertFalse(os.path.exists(os.path.join(self.dir, "state", "portfolio.json")))
        self.assertTrue(any("account snapshot for TSLA failed" in o and "the cash check at placing time decides" in o
                            for o in self.out), self.out)


if __name__ == "__main__":
    unittest.main()
