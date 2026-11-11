"""12 Nov 2026: cash allocation (demo only). A per-trade cap (max_position_pct) on every buy, soft
reservations for stocks with a scheduled catalyst that expire and free their cash, a free pool that
reservations can never claim, max_open_positions, and every decision logged. Plus the confidence score
(the Time Machine's average R before costs), logged on every signal and grouped in the Friday report.
Fakes only: no network, no real order, no phone, never the real config."""

import os
import tempfile
import shutil
import unittest

from _load import load
from test_t212_place import FakeResp, TODAY
from test_cash_and_late_fill import CashPilot
from test_evidence import ev, t212

allocation = load("trader-broker", "allocation")
size = load("trader-budget", "size")
briefing = load("trader-broker", "briefing")

# the demo account on 11 Nov 2026
GBP = {"currency": "GBP", "paper": True, "budget_gbp": 4934.72, "cash_buffer_pct": 10, "max_position_pct": 22,
       "no_entries_before": "09:45", "no_entries_after": "15:30",
       "allocation": {"enabled": True, "max_open_positions": 4, "free_pool_pct": 50, "max_reservations": 2,
                      "window_minutes": 90}}
SUMMARY = {"currency": "GBP", "totalValue": 5000.0, "cash": {"availableToTrade": 4100.25}}


class TempDir(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.clock = [1794400000.0]
        self._now = allocation._now
        allocation._now = lambda: self.clock[0]

    def tearDown(self):
        allocation._now = self._now
        shutil.rmtree(self.dir)

    def events(self):
        return [(e["event"], e.get("symbol")) for e in allocation.load_log(self.dir)]


class Settings(TempDir):
    def test_the_numbers_on_the_demo_account(self):
        f = allocation.figures(GBP)
        self.assertEqual((f["investable_gbp"], f["per_trade_gbp"], f["reservable_gbp"]), (4441.25, 1085.63, 2220.62))
        self.assertLessEqual(4 * f["per_trade_gbp"] * 1.0015, f["investable_gbp"])   # four buys fit, FX fee in

    def test_demo_only(self):
        self.assertTrue(allocation.active(GBP))
        self.assertFalse(allocation.active(dict(GBP, paper=False)))
        self.assertFalse(allocation.active(dict(GBP, live_enabled=True)))
        self.assertFalse(allocation.active({k: v for k, v in GBP.items() if k != "paper"}))
        self.assertFalse(allocation.active(dict(GBP, allocation={"enabled": False})))

    def test_bad_settings_are_refused(self):
        for bad in ({"max_open_positions": 0}, {"free_pool_pct": 120}, {"window_minutes": "90"}, {"enabled": "yes"}):
            with self.subTest(bad), self.assertRaises(allocation.AllocationError):
                allocation.settings({"allocation": bad})


class Plan(TempDir):
    def test_at_most_two_reservations_ranked_and_never_into_the_free_pool(self):
        cands = [{"symbol": s, "reason": "earnings before the open"} for s in ("BBY", "XYZ", "ABC")]
        plan = allocation.make_plan(GBP, TODAY, cands)
        self.assertEqual([r["symbol"] for r in plan["reservations"]], ["BBY", "XYZ"])
        self.assertEqual({(r["start"], r["end"], r["amount_gbp"]) for r in plan["reservations"]},
                         {("09:45", "11:15", 1085.63)})
        self.assertGreaterEqual(plan["free_pool_gbp"], 0.5 * plan["figures"]["investable_gbp"])
        one = allocation.make_plan(dict(GBP, allocation=dict(GBP["allocation"], free_pool_pct=80)), TODAY, cands)
        self.assertEqual(len(one["reservations"]), 0)        # 20 % of 4,441 is under one 1,086 block

    def test_blocked_tickers_are_never_reserved(self):
        plan = allocation.make_plan(dict(GBP, blocked_tickers=["BBY"]), TODAY, [{"symbol": "BBY"}, {"symbol": "XYZ"}])
        self.assertEqual([r["symbol"] for r in plan["reservations"]], ["XYZ"])

    def test_catalysts_are_listed_stocks_reporting_around_the_open_ranked_by_money_traded(self):
        today = [{"symbol": "AAA", "when": "before the open"}, {"symbol": "BBB", "when": "after the close"},
                 {"symbol": "NOTLISTED", "when": "before the open"}, {"symbol": "HELD", "when": "before the open"}]
        prev = [{"symbol": "CCC", "when": "after the close"}, {"symbol": "DDD", "when": "time not given"}]
        out = allocation.catalysts(today, prev, ["AAA", "BBB", "CCC", "DDD", "HELD"], {"AAA": 1e8, "CCC": 5e8},
                                   held={"HELD"})
        self.assertEqual([c["symbol"] for c in out], ["CCC", "AAA"])

    def test_a_second_plan_the_same_day_never_resets_the_first(self):
        allocation.save_plan(self.dir, allocation.make_plan(GBP, TODAY, [{"symbol": "BBY"}]))
        allocation.settle(self.dir, TODAY, "BBY", "used", "bought")
        kept = allocation.save_plan(self.dir, allocation.make_plan(GBP, TODAY, [{"symbol": "XYZ"}]))
        self.assertEqual([(r["symbol"], r["status"]) for r in kept["reservations"]], [("BBY", "used")])
        self.assertEqual(self.events(), [("plan", None), ("reserved", "BBY"), ("used", "BBY")])

    def test_the_morning_line(self):
        plan = allocation.make_plan(GBP, TODAY, [{"symbol": "BBY", "reason": "earnings before the open"}])
        self.assertEqual(allocation.plan_text(plan),
                         "Allocation (demo): max per trade £1,086. Reserved: £1,086 BBY (earnings before the open, "
                         "09:45 to 11:15 NY). Free pool: £3,356. Max 4 positions at once. "
                         "Reservations are a plan, not a buy.")


class ReservationExpires(TempDir):
    """(a) a reservation that expires and frees cash."""

    def test_an_expired_reservation_goes_back_to_the_free_pool(self):
        allocation.save_plan(self.dir, allocation.make_plan(GBP, TODAY, [{"symbol": "BBY"}]))
        held = allocation.reserved_for_others(self.dir, TODAY, "TSLA", "10:30")
        self.assertEqual(held, 1085.63)
        before = t212.free_cash(SUMMARY, [], 1.25, GBP, reserved=held)["available"]
        self.assertEqual(allocation.reserved_for_others(self.dir, TODAY, "BBY", "10:30"), 0.0)   # not from itself
        freed = allocation.reserved_for_others(self.dir, TODAY, "TSLA", "11:15")                 # its window ended
        self.assertEqual(freed, 0.0)
        after = t212.free_cash(SUMMARY, [], 1.25, GBP, reserved=freed)["available"]
        self.assertAlmostEqual(after - before, 1085.63, places=2)
        self.assertEqual(self.events()[-1], ("expired", "BBY"))
        self.assertEqual(allocation.load_plan(self.dir, TODAY)["reservations"][0]["status"], "expired")
        self.assertIsNone(allocation.settle(self.dir, TODAY, "BBY", "used", "too late"))      # gone: nothing to use

    def test_the_cash_text_names_the_reservations(self):
        fc = t212.free_cash(SUMMARY, [], 1.25, GBP, reserved=1085.63)
        self.assertIn("minus GBP 1085.63 reserved for other stocks", t212.no_cash_text(fc))


class PerTradeCap(unittest.TestCase):
    """(c) a large risk-based size capped by max_position_pct."""

    PLAN = {"symbol": "BBY", "side": "buy", "entry": 100.0, "stop": 99.5, "target": 101.0, "date": TODAY}

    def test_trader_budget_caps_a_tight_stop_at_max_position_pct(self):
        cfg = {"budget": 5000, "cash_buffer_pct": 10, "max_loss_per_trade_usd": 50, "max_order_usd": 0,
               "max_position_pct": 22, "min_reward_risk": 1.0}
        wide = size.size_trade(self.PLAN, dict(cfg, max_position_pct=75))
        self.assertEqual((wide["qty"], wide["bound_by"]), (37, "position cap"))   # 75 %: most of the account
        s = size.size_trade(self.PLAN, cfg)
        self.assertEqual((s["qty"], s["bound_by"]), (11, "position cap"))         # max loss alone allows 100
        self.assertLessEqual(s["cost"], 5000 * 0.22)
        self.assertEqual(s["max_loss"], 5.5)                                        # risk shrinks with the size

    def test_placing_caps_it_again_if_a_ticket_came_through_too_big(self):
        cfg = {"currency": "GBP", "budget_gbp": 4934.72, "max_position_pct": 22, "max_loss_per_trade_gbp": 49.34,
               "fractional": True}
        sz = t212.size_entry(100.0, 99.5, 100, 1.25, 4000.0, cfg)
        self.assertEqual(sz["binding"], "max_position_pct")
        self.assertLessEqual(sz["value_gbp"], 4934.72 * 0.22 * t212.POSITION_CAP_ROOM)
        self.assertLess(sz["max_loss_gbp"], 49.34)
        same = t212.size_entry(100.0, 99.5, 13.5, 1.25, 4000.0, cfg)               # a ticket sized right is untouched
        self.assertEqual(same["qty"], 13.5)


class AllocationPilot(CashPilot):
    def setUp(self):
        super().setUp()
        self.cfg = dict(self.cfg, allocation={"enabled": True, "max_open_positions": 4})
        self.write("config.json", self.cfg)
        self._an = allocation._now

    def acct(self, **kw):
        """The clean fake account, with the autopilot's look at positions, then its count of open positions."""
        self.router.add("GET", "/equity/positions", FakeResp([]), FakeResp([]))
        self.router.add("GET", "/equity/orders", FakeResp([]))
        self.account(**kw)

    def reserve(self, sym, amount):
        plan = allocation.make_plan(GBP, TODAY, [{"symbol": sym, "reason": "earnings before the open"}])
        plan["reservations"][0]["amount_gbp"] = amount
        plan["reserved_gbp"] = amount
        allocation._write(allocation.path(self.dir), plan)

    def log(self):
        return [(e["event"], e.get("symbol")) for e in allocation.load_log(self.dir)]

    def status(self, sym):
        return allocation.reservation_for(allocation.load_plan(self.dir, TODAY), sym)


class UnplannedTrade(AllocationPilot):
    """(b) an unplanned trade that buys from the free pool while reservations exist."""

    def test_an_unplanned_buy_uses_the_free_pool_and_leaves_the_reservation(self):
        self.reserve("AAPL", 1000.0)          # 4,100.25 cash - 500 buffer - 1,000 for AAPL = 2,600 >= TSLA's 2,336
        self.acct()
        self.pilot().cycle()
        self.assertEqual([x[1] for x in self.router.posts()], ["/equity/orders/market", "/equity/orders/stop"])
        trade = ev.load(self.dir, "trades")[-1]
        self.assertEqual((trade["pool"], trade["sizing"]), ("free", "v2"))
        self.assertEqual(trade["confidence"]["level"], "high")
        self.assertIsNotNone(trade["risk_gbp"])
        self.assertEqual(self.status("AAPL")["status"], "active")                    # still held for AAPL
        self.assertIn(("used", "TSLA"), self.log())

    def test_reservations_hold_cash_back_from_other_stocks(self):
        self.reserve("AAPL", 1500.0)          # 2,100 left: TSLA's 2,336 does not fit
        self.acct()
        p = self.pilot()
        p.cycle()
        self.assertEqual(self.router.posts(), [])
        self.assertEqual(p.state["tried"]["TSLA"], "skipped: no free cash")
        self.assertIn(("skipped", "TSLA"), self.log())
        self.assertTrue(any("reserved for other stocks" in o for o in self.out), self.out)

    def test_the_reserved_stock_may_use_its_own_reservation(self):
        self.reserve("TSLA", 3000.0)
        self.acct()
        self.pilot().cycle()
        self.assertEqual(len(self.router.posts("/equity/orders/market")), 1)
        self.assertEqual(ev.load(self.dir, "trades")[-1]["pool"], "reserved:TSLA")
        plan = allocation.load_plan(self.dir, TODAY)
        self.assertEqual(plan["reservations"][0]["status"], "used")

    def test_a_reserved_stock_that_fails_the_checks_releases_its_cash(self):
        self.reserve("TSLA", 1000.0)
        self.pipe.verdict = "BLOCK"
        self.acct()
        self.pilot().cycle()
        self.assertEqual(self.router.posts(), [])
        r = allocation.load_plan(self.dir, TODAY)["reservations"][0]
        self.assertEqual(r["status"], "released")
        self.assertIn("did not pass the checks", r["why"])


class MaxOpenPositions(AllocationPilot):
    def test_no_new_buy_with_four_positions_open_but_the_signal_is_logged(self):
        held = [{"instrument": {"ticker": "%s_US_EQ" % s}, "quantity": 1} for s in ("A", "B", "C")]
        waiting = [{"id": 7, "ticker": "D_US_EQ", "type": "MARKET", "side": "BUY", "quantity": 2, "filledQuantity": 0}]
        self.router.add("GET", "/equity/positions", FakeResp(held))
        self.router.add("GET", "/equity/orders", FakeResp(waiting))
        p = self.pilot()
        line = p.cycle()
        self.assertEqual(self.router.posts(), [])
        self.assertIn("TSLA: skipped, 4 positions open or waiting to fill", line)
        sig = ev.load(self.dir, "signals")[-1]
        self.assertTrue(sig["decision"].startswith("skipped: 4 positions open"))     # still gets a virtual result
        self.assertIn(("skipped", "TSLA"), self.log())

    def test_three_open_still_buys(self):
        one = [{"instrument": {"ticker": "A_US_EQ"}, "quantity": 1}]
        self.router.add("GET", "/equity/positions", FakeResp(one), FakeResp(one))
        self.router.add("GET", "/equity/orders", FakeResp([]))
        self.account(positions=one)
        self.pilot().cycle()
        self.assertEqual(len(self.router.posts("/equity/orders/market")), 1)

    def test_off_on_a_config_that_is_not_paper(self):
        p = self.pilot()
        p.cfg = dict(self.cfg, paper=False)
        self.reserve("AAPL", 3000.0)
        self.assertFalse(p.alloc_on())
        self.assertEqual(p.reserved_others("TSLA"), 0.0)
        self.assertIsNone(p.positions_full("TSLA"))


class Confidence(AllocationPilot):
    def test_every_signal_carries_a_confidence_level(self):
        self.assertEqual(ev.confidence(0.3)["level"], "high")
        self.assertEqual(ev.confidence(0.05)["level"], "medium")
        self.assertEqual(ev.confidence(-0.01)["level"], "low")
        self.assertIsNone(ev.confidence(None))
        self.assertEqual(ev.confidence(0.05, {"evidence": {"confidence_bands": [-0.1, 0.04]}})["level"], "high")
        with self.assertRaises(ev.EvidenceError):
            ev.confidence_bands({"evidence": {"confidence_bands": [0.2, 0.1]}})

    def test_logged_on_the_signal_and_on_the_trade(self):
        self.acct()
        self.pilot().cycle()
        sig = ev.load(self.dir, "signals")[-1]
        self.assertEqual(sig["confidence"], {"score": 0.3, "level": "high",
                                             "source": "Time Machine average R before costs"})
        self.assertEqual(ev.load(self.dir, "trades")[-1]["confidence"], sig["confidence"])

    def test_the_size_is_the_same_whatever_the_confidence(self):
        sizes = []
        for i, score in enumerate((0.3, -0.2)):
            if i:
                self.tearDown()
                self.setUp()
            saved = ev.confidence
            ev.confidence = lambda s, cfg=None, x=score: saved(x, cfg)
            try:
                self.acct()
                self.pilot().cycle()
                sizes.append(self.router.posts("/equity/orders/market")[0][2]["quantity"])
                self.assertEqual(ev.load(self.dir, "signals")[-1]["confidence"]["level"], "high" if score > 0 else "low")
            finally:
                ev.confidence = saved
        self.assertEqual(sizes[0], sizes[1])

    def test_the_friday_report_groups_results_in_r_by_level(self):
        sigs = [{"rules": "v2", "tm_avg_R": 0.2, "virtual": {"R_net": 0.5}},
                {"rules": "v2", "tm_avg_R": 0.15, "virtual": {"R_net": -1.0}},
                {"rules": "v2", "tm_avg_R": 0.05, "virtual": {"R_net": 0.25}},
                {"rules": "v2", "confidence": {"level": "low"}, "tm_avg_R": 0.5, "virtual": {"R_net": -0.5}},
                {"rules": "v1", "tm_avg_R": 0.3, "virtual": {"R_net": 2.0}}]          # v1: a different simulation
        g = ev.by_confidence(sigs, [], [], ev.CONFIDENCE_BANDS)
        self.assertEqual((g["high"]["virtual"], g["high"]["virtual_avg_R"], g["high"]["virtual_win"]), (2, -0.25, 50.0))
        self.assertEqual((g["medium"]["virtual"], g["low"]["virtual"]), (1, 1))
        r = {"by_confidence": g, "confidence_bands": ev.CONFIDENCE_BANDS}
        lines = [l for l in self._report_lines(r) if "high:" in l or l.startswith("Confidence")]
        self.assertTrue(lines[0].startswith("Confidence (Time Machine average R before costs"), lines)
        self.assertIn("high: virtual 2 logged, 2 resolved, average -0.250R after costs, 50% won", lines[1])

    def _report_lines(self, extra):
        base = {"start": "2026-11-09", "today": "2026-11-13", "min_trades": 20, "real": [], "real_avg_gbp": None,
                "real_avg_ret": None, "real_total_gbp": 0.0, "signals": 0, "virtual": 0, "virtual_avg_R": None,
                "virtual_avg_ret": None, "virtual_win": None, "slip_entry": None, "slip_exit": None, "slip_n": (0, 0),
                "kronos": {"agree": (0, None), "disagree": (0, None), "other": 0},
                "kronos_real": {"agree": 0, "disagree": 0}}
        p = {"from": "2026-11-09", "to": "2026-11-13", "n": 0, "min_trades": 20, "avg_gbp": None, "total_gbp": 0.0,
             "strategy_ret": None, "start_value": None, "tracker_ret": None, "status": "TOO FEW TRADES TO JUDGE",
             "lean": None}
        base.update(week=p, since_start=p, **extra)
        return ev.report_text(base).split("\n")


class Briefing(TempDir):
    def setUp(self):
        super().setUp()
        self._all = briefing.all_earnings
        self._wu = briefing.t212.with_universe

    def tearDown(self):
        briefing.all_earnings = self._all
        briefing.t212.with_universe = self._wu
        super().tearDown()

    def test_the_morning_line_and_the_plan_file(self):
        import datetime
        rows = {"2026-11-05": [{"symbol": "BBY", "when": "before the open"}],
                "2026-11-04": [{"symbol": "CHTR", "when": "after the close"}]}
        briefing.all_earnings = lambda d: rows.get(d.isoformat(), [])
        briefing.t212.with_universe = lambda cfg, sd: dict(cfg, allowed_tickers=["BBY", "CHTR"])
        line = briefing.allocation_line(GBP, self.dir, datetime.date(2026, 11, 5), datetime.date(2026, 11, 4), set())
        self.assertIn("Reserved: £1,086 CHTR (earnings after yesterday's close, 09:45 to 11:15 NY), "
                      "£1,086 BBY (earnings before the open, 09:45 to 11:15 NY)", line)
        self.assertIn("Free pool: £2,270. Max 4 positions", line)
        self.assertEqual(len(allocation.load_plan(self.dir, "2026-11-05")["reservations"]), 2)
        self.assertIsNone(briefing.allocation_line(dict(GBP, paper=False), self.dir, datetime.date(2026, 11, 5),
                                                   datetime.date(2026, 11, 4), set()))


if __name__ == "__main__":
    unittest.main()
