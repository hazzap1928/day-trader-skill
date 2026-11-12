"""Budget from the account value: 90% may be invested, 10% kept as cash, 1% max loss a trade, 3% max loss a
day of the value at the start of the day, an optional max_budget_gbp cap. Demo and live. Fakes only."""

import io
import json
import os
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load
import test_t212_place
from test_t212_place import Base, FakeResp, CREDS
from test_autopilot import PilotBase, ap

t212 = test_t212_place.t212
ab = t212.budget_module()
check = load("trader-risk", "check")
size = load("trader-budget", "size")

ON = {"enabled": True, "budget_pct": 90, "max_loss_pct": 1, "daily_loss_pct": 3, "max_budget_gbp": None}


def summary(value, currency="GBP"):
    return {"id": 1, "currency": currency, "totalValue": value, "cash": {"availableToTrade": value / 2}}


class Figures(unittest.TestCase):
    def s(self, **kw):
        return ab.settings({"currency": "GBP", "account_budget": dict(ON, **kw)})

    def test_todays_account(self):
        r = ab.compute(5026.83, self.s())
        self.assertEqual((r["invest_gbp"], r["cash_gbp"]), (4524.14, 502.69))
        self.assertEqual((r["max_loss_per_trade_gbp"], r["max_daily_loss_gbp"]), (50.26, 150.80))
        self.assertEqual((r["budget_gbp"], r["cash_buffer_pct"]), (5026.83, 10.0))
        self.assertFalse(r["capped"])

    def test_the_cap_limits_the_money_invested_and_the_risk_with_it(self):
        r = ab.compute(100000.0, self.s(max_budget_gbp=5000))
        self.assertTrue(r["capped"])
        self.assertEqual(r["invest_gbp"], 5000.0)
        self.assertEqual(r["max_loss_per_trade_gbp"], 55.55)          # 1% of 5,000 / 90%
        self.assertEqual(r["cash_gbp"], 95000.0)
        self.assertFalse(ab.compute(4000.0, self.s(max_budget_gbp=5000))["capped"])

    def test_off_bad_settings_and_bad_values_are_refused(self):
        self.assertIsNone(ab.settings({"currency": "GBP"}))
        self.assertIsNone(ab.settings({"currency": "GBP", "account_budget": dict(ON, enabled=False)}))
        for bad in (dict(budget_pct=150), dict(max_loss_pct=0), dict(daily_loss_pct="3"), dict(max_budget_gbp=-1)):
            with self.subTest(bad):
                with self.assertRaises(ab.BudgetError):
                    self.s(**bad)
        with self.assertRaises(ab.BudgetError):
            ab.settings({"currency": "USD", "account_budget": ON})
        for v in (None, 0, -5):
            with self.assertRaises(ab.BudgetError):
                ab.compute(v, self.s())

    def test_plain_english(self):
        text = ab.plain(ab.compute(5026.83, self.s()), "2026-11-09")
        for want in ("Budget from the account value on 2026-11-09: GBP 5,026.83", "Can be invested: GBP 4,524.14 (90%",
                     "Kept as cash: GBP 502.69", "Max loss a trade: GBP 50.26 (1% of the account)",
                     "Max loss a day: GBP 150.80 (3% of the account at the start of the day)", "No max_budget_gbp cap set."):
            self.assertIn(want, text)


class ApplyAndTools(Base):
    def setUp(self):
        super().setUp()
        self.gbp = {"currency": "GBP", "budget_gbp": 2500, "cash_buffer_pct": 10, "max_position_pct": 75,
                    "min_order_gbp": 5, "max_loss_per_trade_gbp": 30, "max_daily_loss_gbp": 75, "min_reward_risk": 1.0,
                    "require_stop": True, "paper": True, "broker": "trading212", "fractional": True,
                    "costs": {"fx_fee_pct": 0, "slippage_pct": 0}, "account_budget": dict(ON)}
        self.write("config.json", self.gbp)
        self.path = os.path.join(self.dir, "config.json")

    def test_apply_writes_the_keys_every_tool_reads_once_a_day(self):
        r, changed = ab.apply(5026.83, self.path, "2026-11-09")
        self.assertTrue(changed)
        c = self.read("config.json")
        self.assertEqual((c["budget_gbp"], c["cash_buffer_pct"], c["max_loss_per_trade_gbp"], c["max_daily_loss_gbp"]),
                         (5026.83, 10.0, 50.26, 150.80))
        self.assertEqual(c["account_budget"]["last"]["date"], "2026-11-09")
        self.assertEqual(c["min_order_gbp"], 5)                        # nothing else touched
        r, changed = ab.apply(9999.0, self.path, "2026-11-09")
        self.assertFalse(changed)
        self.assertEqual(self.read("config.json")["budget_gbp"], 5026.83)
        r, changed = ab.apply(5100.0, self.path, "2026-11-09", force=True)
        self.assertEqual(self.read("config.json")["max_loss_per_trade_gbp"], 51.0)

    def test_a_bad_reading_changes_nothing(self):
        with self.assertRaises(ab.BudgetError):
            ab.apply(0, self.path, "2026-11-09")
        self.assertEqual(self.read("config.json")["budget_gbp"], 2500)

    def test_the_risk_check_and_sizing_use_the_new_figures(self):
        ab.apply(5026.83, self.path, "2026-11-09")
        cfg = self.read("config.json")
        m = check.money_config(cfg, fx_rate=1.25)
        self.assertEqual(m["max_loss_per_trade_usd"], 62.82)                 # GBP 50.26 x 1.25, down to the cent
        self.assertEqual(m["max_daily_loss_usd"], 188.5)
        sm = size.money_config(cfg, fx_rate=1.25)
        self.assertAlmostEqual(sm["budget"] * (1 - sm["cash_buffer_pct"] / 100.0), 5026.83 * 1.25 * 0.9, places=1)

    def run_cmd(self, *extra, environ=CREDS):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = t212.main(["budget"] + list(extra) + ["--state-dir", self.dir], environ=environ)
        return code, out.getvalue(), err.getvalue()

    def test_t212_budget_reads_the_account_and_writes_only_with_apply(self):
        self.router.add("GET", "/equity/account/summary", FakeResp(summary(5026.83)))
        code, out, err = self.run_cmd()
        self.assertEqual(code, 0, err)
        self.assertIn("Max loss a trade: GBP 50.26", out)
        self.assertIn("not written (add --apply)", out)
        self.assertEqual(self.read("config.json")["budget_gbp"], 2500)
        code, out, err = self.run_cmd("--apply")
        self.assertIn("written to", out)
        self.assertEqual(self.read("config.json")["max_daily_loss_gbp"], 150.80)
        self.assertEqual([c[0] for c in self.router.calls], ["GET", "GET"])          # reads only

    def test_live_works_the_same_and_only_reads(self):
        self.write("config.json", dict(self.gbp, live_enabled=True, paper=False))
        self.router.add("GET", "/equity/account/summary", FakeResp(summary(20000.0)))
        code, out, err = self.run_cmd("--apply", "--live", environ=dict(CREDS, T212_ENV="live"))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.read("config.json")["max_loss_per_trade_gbp"], 200.0)
        self.assertTrue(all(c[4].startswith("https://live.trading212.com/") for c in self.router.calls))
        self.assertEqual(self.router.posts(), [])

    def test_a_usd_account_is_refused(self):
        self.router.add("GET", "/equity/account/summary", FakeResp(summary(5000.0, "USD")))
        code, _, err = self.run_cmd("--apply")
        self.assertEqual(code, 2)
        self.assertEqual(self.read("config.json")["budget_gbp"], 2500)

    def test_run_guard_updates_the_budget_before_the_guard_and_never_stops_it(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "trader-broker", "run-guard.sh")) as fh:
            src = fh.read()
        line = 'trader-broker/t212.py budget --apply "${live[@]}" || echo'
        self.assertIn(line, src)
        self.assertLess(src.index(line), src.index("exec /usr/bin/python3 trader-broker/t212.py guard"))


class Autopilot(PilotBase):
    def setUp(self):
        super().setUp()
        self.cfg = dict(self.cfg, currency="GBP", account_budget=dict(ON))
        for k in [k for k in self.cfg if k.endswith("_usd") or k == "budget"]:
            self.cfg.pop(k)
        self.cfg.update(budget_gbp=2500, max_loss_per_trade_gbp=30, max_daily_loss_gbp=75)
        self.write("config.json", self.cfg)

    def test_the_morning_update_and_the_status_line(self):
        self.router.add("GET", "/equity/account/summary", FakeResp(summary(5026.83)))
        p = self.pilot()
        with redirect_stdout(io.StringIO()):
            p.apply_budget()
            p.apply_budget()                                       # once a day
        self.assertEqual(len([c for c in self.router.calls if c[1] == "/equity/account/summary"]), 1)
        self.assertEqual(self.read("config.json")["max_loss_per_trade_gbp"], 50.26)
        self.assertIn("Budget (from the GBP 5,026.83 account on 2026-11-05): invest up to GBP 4,524.14, keep GBP 502.69 "
                      "cash; max loss GBP 50.26 a trade, GBP 150.80 a day", p.budget_line())
        self.router.add("GET", "/equity/positions", FakeResp([]))
        self.assertIn("Most I'll risk: £50.26 a trade, £150.80 a day", p.status_text())

    def test_a_failed_reading_keeps_yesterdays_limits_and_says_so(self):
        self.router.add("GET", "/equity/account/summary", test_t212_place.http_error(500))
        p = self.pilot()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            p.apply_budget()
        self.assertEqual(self.read("config.json")["max_loss_per_trade_gbp"], 30)
        self.assertIn("Couldn't update today's spending limits", self.phone.sent[-1][0])


if __name__ == "__main__":
    unittest.main()
