"""A GBP config ("currency": "GBP", *_gbp limits) is read as pounds by every tool.

Prices are in US dollars, so the pound limits are converted at the GBP/USD rate.
The trap these tests guard against: a pound figure read as dollars (GBP 30 taken
as $30), or a dollar default ($50 max loss) quietly filling a gap."""

import datetime
import io
import json
import os
import re
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest import mock

from _load import load, ROOT, FIX

size = load("trader-budget", "size")
check = load("trader-risk", "check")
ticket = load("trader-broker", "ticket")
t212 = load("trader-broker", "t212")

RATE = 1.30     # USD per GBP
# the user's settings, as written in trader/config.json
GBP_CFG = {
    "currency": "GBP", "budget_gbp": 2500, "cash_buffer_pct": 10, "max_position_pct": 75,
    "min_order_gbp": 5, "max_loss_per_trade_gbp": 30, "max_daily_loss_gbp": 75,
    "max_trades_per_day": 5, "max_orders_per_day": 10, "min_reward_risk": 1.5, "require_stop": True,
    "no_entries_before": "09:45", "no_entries_after": "15:30", "flat_by": "15:55",
    "fractional": True, "broker": "trading212", "paper": True, "blocked": [],
    "allow_options": False, "allow_margin": False, "allow_short": False, "pdt_guard": False,
}
NOW = "2026-10-19 10:15"                                  # New York
NOW_UTC = datetime.datetime(2026, 10, 19, 14, 15)
# TSLA: entry 364.18, stop 358.32 (5.86 a share), target 373.00 (reward:risk 1.51)
with open(os.path.join(FIX, "trader-budget", "plan-tsla.json"), encoding="utf-8") as _fh:
    PLAN = dict(json.load(_fh), target=373.00)


def bracket(qty, target="373.00"):
    return {"symbol": "TSLA", "side": "buy", "qty": str(qty), "type": "market", "time_in_force": "day",
            "order_class": "bracket", "take_profit_limit_price": target, "stop_loss_stop_price": "358.32",
            "plan": {"entry": 364.18, "stop": 358.32, "target": float(target)}}


def read(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def write(path, doc):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)


def run(main, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


class SameBlockEverywhere(unittest.TestCase):
    def test_the_money_block_is_identical_in_all_three_tools(self):
        def block(rel):
            with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
                s = fh.read()
            i = s.index("# ---------------------------------------------------------------- money: USD or GBP config")
            j = s.index("\nclass ", i)
            return s[i:j]
        a = block("trader-budget/size.py")
        self.assertEqual(a, block("trader-risk/check.py"))
        self.assertEqual(a, block("trader-broker/ticket.py"))


class Conversion(unittest.TestCase):
    def test_pound_limits_become_dollars_at_the_rate(self):
        c = size.money_config(GBP_CFG, fx_rate=RATE)
        self.assertEqual(c["currency"], "GBP")
        self.assertEqual(c["budget"], 3250.0)                 # 2500 x 1.30
        self.assertEqual(c["max_loss_per_trade_usd"], 39.0)   # 30 x 1.30, not $30 and not the $50 default
        self.assertEqual(c["max_daily_loss_usd"], 97.5)       # 75 x 1.30
        self.assertEqual(c["min_order_usd"], 6.5)
        self.assertIsNone(c["max_order_usd"])                 # not set: no $4,000 default sneaks in
        self.assertEqual(c["money"]["usd_per_gbp"], RATE)

    def test_limits_round_down_and_the_minimum_rounds_up(self):
        c = size.money_config(GBP_CFG, fx_rate=1.33333)
        self.assertEqual(c["max_loss_per_trade_usd"], 39.99)  # 39.9999 down, never over GBP 30
        self.assertEqual(c["min_order_usd"], 6.67)            # 6.66665 up

    def test_after_load_config_no_dollar_default_is_left(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.json")
            write(p, GBP_CFG)
            c = size.load_config(p, fx_rate=RATE)
        self.assertEqual(c["max_loss_per_trade_usd"], 39.0)
        self.assertNotEqual(c["max_loss_per_trade_usd"], size.DEFAULTS["max_loss_per_trade_usd"])
        self.assertIsNone(c["max_order_usd"])
        self.assertEqual(c["min_reward_risk"], 1.5)
        self.assertEqual(c["max_trades_per_day"], 5)

    def test_a_dollar_config_is_untouched(self):
        cfg = {"budget": 5000, "max_loss_per_trade_usd": 50}
        c = size.money_config(cfg)
        self.assertEqual((c["currency"], c["budget"], c["max_loss_per_trade_usd"]), ("USD", 5000, 50))

    def test_mixed_configs_are_refused(self):
        for bad in (dict(GBP_CFG, max_loss_per_trade_usd=30),        # GBP file, a dollar name
                    dict(GBP_CFG, budget=2500),
                    {"budget_gbp": 2500, "max_loss_per_trade_gbp": 30},   # pound names, no currency
                    dict(GBP_CFG, currency="EUR")):
            with self.subTest(bad=sorted(bad)):
                with self.assertRaises(size.CurrencyError):
                    size.money_config(bad, fx_rate=RATE)

    def test_the_rate_file_must_exist_and_be_fresh(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "fx.json")
            with self.assertRaisesRegex(size.CurrencyError, "no GBP/USD rate"):
                size.money_config(GBP_CFG, fx_path=p, now_utc=NOW_UTC)
            write(p, {"usd_per_gbp": RATE, "fetched_at": "2026-10-19T01:00:00Z", "source": "test"})
            with self.assertRaisesRegex(size.CurrencyError, "more than 12 hours old"):
                size.money_config(GBP_CFG, fx_path=p, now_utc=NOW_UTC)
            write(p, {"usd_per_gbp": RATE, "fetched_at": "2026-10-19T13:00:00Z", "source": "test"})
            c = size.money_config(GBP_CFG, fx_path=p, now_utc=NOW_UTC)
            self.assertEqual(c["max_loss_per_trade_usd"], 39.0)
            write(p, {"usd_per_gbp": 130, "fetched_at": "2026-10-19T13:00:00Z"})
            with self.assertRaisesRegex(size.CurrencyError, "looks wrong"):
                size.money_config(GBP_CFG, fx_path=p, now_utc=NOW_UTC)


class Budget(unittest.TestCase):
    def test_size_reads_the_max_loss_as_pounds(self):
        with tempfile.TemporaryDirectory() as d:
            cfg, plan, out = (os.path.join(d, n) for n in ("config.json", "plan.json", "orders.json"))
            write(cfg, GBP_CFG)
            write(plan, PLAN)
            write(os.path.join(d, "prices.json"), {})
            code, text, err = run(size.main, ["--config", cfg, "--plan", plan, "--now", NOW, "--fx-rate",
                                              str(RATE), "--portfolio", os.path.join(d, "none.json"),
                                              "--prices", os.path.join(d, "prices.json"), "--write", out])
            self.assertEqual(code, 0, err)
            order = read(out)["orders"][0]
        # $39.00 / 5.86 = 6.65 shares (fractional on Trading 212). Read as dollars it would be 5.11.
        self.assertEqual(order["qty"], "6.65")
        self.assertLessEqual(order["plan"]["max_loss"], 39.0)
        self.assertIn("GBP 30", order["why"])
        self.assertIn("Your limits are in GBP, at 1.3000 USD per GBP", text)

    def test_reward_risk_minimum_applies(self):
        c = size.money_config(GBP_CFG, fx_rate=RATE)
        s = size.size_trade(dict(PLAN, target=370.04), c, today="2026-10-19")   # reward:risk 1.0
        self.assertIn("under your minimum 1.5", s["refusal"])

    def test_an_unconverted_gbp_config_and_a_dollar_budget_flag_are_refused(self):
        with self.assertRaisesRegex(size.SizeError, "not converted"):
            size.size_trade(PLAN, GBP_CFG, today="2026-10-19")
        with self.assertRaisesRegex(size.SizeError, "budget_gbp"):
            size.size_trade(PLAN, size.money_config(GBP_CFG, fx_rate=RATE), budget=2500, today="2026-10-19")


class Risk(unittest.TestCase):
    def cfg(self):
        return check.money_config(GBP_CFG, fx_rate=RATE)

    def test_a_loss_over_30_dollars_but_under_30_pounds_passes(self):
        # 6.65 x 5.86 = $38.97: over $30, under GBP 30 ($39.00)
        res = check.check([bracket("6.65")], self.cfg(), now=NOW_UTC)
        self.assertEqual(res["results"][0]["verdict"], "PASS", res["results"][0]["reasons"])

    def test_a_loss_over_30_pounds_is_blocked_and_says_pounds(self):
        res = check.check([bracket("7")], self.cfg(), now=NOW_UTC)          # 7 x 5.86 = $41.02
        r = res["results"][0]
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertTrue(any("$39 (GBP 30) max loss" in x for x in r["reasons"]), r["reasons"])

    def test_the_daily_loss_limit_is_75_pounds(self):
        placed = {"orders": [{"date": "2026-10-19", "status": "placed", "symbol": s, "side": "buy",
                              "kind": "entry", "max_loss": 30.0} for s in ("AAA", "BBB")]}
        res = check.check([bracket("6.65")], self.cfg(), state=placed, now=NOW_UTC)
        r = res["results"][0]                    # $60 + $38.97 = $98.97, over $97.50 (GBP 75)
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertTrue(any("GBP 75" in x and "daily loss" in x for x in r["reasons"]), r["reasons"])

    def test_the_fresh_account_is_the_budget_in_dollars(self):
        # GBP 2,500 = $3,250; 75% cap = $2,437.50. 6.65 TSLA = $2,421.80 fits; read as $2,500 it would not
        res = check.check([bracket("6.65")], self.cfg(), now=NOW_UTC)
        self.assertEqual(res["results"][0]["verdict"], "PASS", res["results"][0]["reasons"])
        self.assertEqual(res["cash_after"], round(3250 - 6.65 * 364.18, 2))

    def test_fractions_only_for_trading_212(self):
        res = check.check([bracket("6.65")], dict(self.cfg(), broker="alpaca"), now=NOW_UTC)
        self.assertTrue(any("whole shares only" in x for x in res["results"][0]["reasons"]))
        res = check.check([bracket("6.655")], self.cfg(), now=NOW_UTC)
        self.assertTrue(any("0.01 of a share" in x for x in res["results"][0]["reasons"]))

    def test_check_main_needs_a_rate(self):
        with tempfile.TemporaryDirectory() as d:
            cfg, orders = os.path.join(d, "config.json"), os.path.join(d, "orders.json")
            write(cfg, GBP_CFG)
            write(orders, {"orders": [bracket("6.65")]})
            base = ["--config", cfg, "--orders", orders, "--now", NOW, "--no-write",
                    "--portfolio", os.path.join(d, "x"), "--prices", os.path.join(d, "x"),
                    "--state", os.path.join(d, "x")]
            code, _, err = run(check.main, base + ["--fx", os.path.join(d, "fx.json")])
            self.assertEqual(code, 1)
            self.assertIn("no GBP/USD rate", err)
            code, text, err = run(check.main, base + ["--fx-rate", str(RATE)])
            self.assertEqual(code, 0, err + text)
            self.assertIn("max loss per trade $39 (GBP 30)", text)


class Ticket(unittest.TestCase):
    def test_a_new_portfolio_starts_with_the_budget_in_dollars(self):
        with tempfile.TemporaryDirectory() as d:
            pf, fx = os.path.join(d, "portfolio.json"), os.path.join(d, "fx.json")
            now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            write(fx, {"usd_per_gbp": RATE, "fetched_at": now, "source": "test"})
            with mock.patch.object(ticket, "FX_PATH", fx):
                ticket.apply_fill(pf, GBP_CFG, "TSLA", "buy", 1, 100.0, "2026-10-19")
            self.assertEqual(read(pf)["cash"], 3250.0 - 100.0)

    def test_a_mixed_config_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.json")
            write(p, dict(GBP_CFG, max_daily_loss_usd=75))
            with self.assertRaisesRegex(ticket.TicketError, "max_daily_loss_gbp"):
                ticket.read_config(p)


class Trading212(unittest.TestCase):
    def test_place_sizes_with_the_pound_limit_directly(self):
        s = t212.size_entry(364.18, 358.32, 100, RATE, 10 ** 6, GBP_CFG, fractional=True)
        self.assertEqual(s["limit_gbp"], 30.0)
        self.assertLessEqual(s["max_loss_gbp"], 30.0)
        self.assertEqual(s["limit_src"], "max_loss_per_trade_gbp GBP 30.00")

    def test_fractions_follow_the_instrument_type(self):
        self.assertTrue(t212.fractional_for(GBP_CFG, {"type": "STOCK"}))
        self.assertTrue(t212.fractional_for(GBP_CFG, {"type": "ETF"}))
        self.assertFalse(t212.fractional_for(GBP_CFG, {"type": "WARRANT"}))
        self.assertFalse(t212.fractional_for(dict(GBP_CFG, fractional=False), {"type": "STOCK"}))
        whole = t212.size_entry(364.18, 358.32, 100, RATE, 10 ** 6, GBP_CFG, fractional=False)
        self.assertEqual(whole["qty"], int(whole["qty"]))

    def test_fx_command_writes_a_rate_the_other_tools_accept(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(t212, "fetch_gbpusd", return_value=(RATE, "2026-11-06T09:00:00Z", "test")):
                code, text, err = run(t212.main, ["fx", "--state-dir", d])
            self.assertEqual(code, 0, err)
            doc = read(os.path.join(d, "fx.json"))
            self.assertEqual(doc["usd_per_gbp"], RATE)
            at = datetime.datetime.strptime(doc["fetched_at"], "%Y-%m-%dT%H:%M:%SZ")
            c = size.money_config(GBP_CFG, fx_path=os.path.join(d, "fx.json"), now_utc=at)
            self.assertEqual(c["max_loss_per_trade_usd"], 39.0)


if __name__ == "__main__":
    unittest.main()
