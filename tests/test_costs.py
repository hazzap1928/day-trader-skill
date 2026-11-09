"""Costs: Trading 212's 0.15 % FX fee on the buy and on the sell, plus a slippage allowance, in the
Time Machine (average R after costs), trader-risk (reward:risk after costs) and the autopilot."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load
from test_currency import GBP_CFG, RATE, NOW_UTC, bracket

tm = load("trader-timemachine", "timemachine")
check = load("trader-risk", "check")

GBP = {k: v for k, v in GBP_CFG.items() if k != "costs"}          # the defaults: 0.15 % fee, 0.05 % slip
USD = {"currency": "USD", "broker": "alpaca"}


def doc(entry=229.03, risk=3.13, avg_r=0.053, trades=()):
    return {"plan": {"entry": entry, "risk_per_share": risk, "side": "buy"}, "mc": {"avg_R": avg_r},
            "history": {"avg_R": 0.0, "trades": [dict(t) for t in trades]}, "verdict_notes": ["v"]}


class TimeMachine(unittest.TestCase):
    def test_defaults_count_costs_for_a_gbp_or_trading_212_config_only(self):
        self.assertEqual(tm.costs_from(GBP)["fx_fee_pct"], 0.15)
        self.assertEqual(tm.costs_from(GBP)["slippage_pct"], 0.05)
        self.assertEqual(tm.costs_from({"broker": "trading212"})["fx_fee_pct"], 0.15)
        self.assertEqual((tm.costs_from(USD)["fx_fee_pct"], tm.costs_from(USD)["slippage_pct"]), (0.0, 0.0))

    def test_config_then_flags_win(self):
        c = tm.costs_from(dict(USD, costs={"fx_fee_pct": 0, "slippage_pct": 0.1}))
        self.assertEqual((c["fx_fee_pct"], c["slippage_pct"], c["from"]), (0, 0.1, "trader/config.json costs"))
        c = tm.costs_from(GBP, fee_pct=0.0, slip_pct=0.02)
        self.assertEqual((c["fx_fee_pct"], c["slippage_pct"]), (0.0, 0.02))
        for bad in (-1, 3, "0.15", True):
            with self.assertRaises(tm.TMError):
                tm.costs_from({"costs": {"fx_fee_pct": bad}})

    def test_dhr_plan_after_costs(self):
        d = tm.apply_costs(doc(), tm.costs_from(GBP))
        c = d["costs"]
        self.assertAlmostEqual(c["mc_cost_R"], 0.002 * (2 * 229.03 / 3.13 + 0.053), places=3)
        self.assertEqual(c["mc_avg_R_net"], -0.24)
        self.assertEqual(c["stop_pct"], 1.367)
        self.assertIn("After costs (FX fee 0.15 % on the buy and on the sell, slippage 0.05 % a fill", d["verdict_notes"][-1])
        self.assertIn("this plan loses on average", d["verdict_notes"][-1])

    def test_each_history_trade_is_costed_at_its_own_exit(self):
        t = {"entry": 100.0, "stop": 99.0, "R": 1.5}                    # out at 101.50
        d = tm.apply_costs(doc(entry=100.0, risk=1.0, trades=[t]), tm.costs_from(GBP))
        self.assertAlmostEqual(d["history"]["trades"][0]["R_net"], 1.5 - 0.002 * (100 + 101.5) / 1.0, places=3)
        self.assertEqual(d["costs"]["history_green_net"], 1)

    def test_no_costs_no_note(self):
        d = tm.apply_costs(doc(), tm.costs_from(USD))
        self.assertEqual(d["verdict_notes"], ["v"])
        self.assertEqual(d["costs"]["mc_avg_R_net"], 0.053)

    def test_run_reads_costs_from_the_config_it_is_given(self):
        import test_timemachine as ttm
        with tempfile.TemporaryDirectory() as d:
            cfgp, planp = os.path.join(d, "config.json"), os.path.join(d, "plan.json")
            with open(cfgp, "w") as fh:
                json.dump(GBP, fh)
            with open(planp, "w") as fh:
                json.dump(ttm.PLAN, fh)
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                code = tm.main(["run", "--plan", planp, "--offline", ttm.TSLA, "--now", ttm.NOW,
                                "--config", cfgp, "--json"])
            self.assertEqual(code, 0)
            got = json.loads(out.getvalue())["costs"]
            self.assertEqual((got["fx_fee_pct"], got["slippage_pct"]), (0.15, 0.05))
            self.assertLess(got["mc_avg_R_net"], got["mc_avg_R"])


class Risk(unittest.TestCase):
    def cfg(self, **kw):
        return check.money_config(dict(GBP, **kw), fx_rate=RATE)

    def test_a_15_to_1_plan_is_blocked_after_costs(self):
        res = check.check([bracket("6")], self.cfg(), now=NOW_UTC)          # 1.51 before costs
        r = res["results"][0]
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertEqual(r["reward_risk"], 1.51)
        self.assertEqual(r["reward_risk_net"], 1.01)
        self.assertTrue(any(x.startswith("reward:risk after costs: 1.01 (1.51 before; FX fee 0.15 % each way + "
                                         "slippage 0.05 % a fill") for x in r["reasons"]), r["reasons"])

    def test_a_wide_enough_plan_passes_and_says_its_cost(self):
        o = bracket("2", target="400.00")                    # reward 35.82, risk 5.86: 6.11 before costs
        res = check.check([o], self.cfg(), now=NOW_UTC)
        r = res["results"][0]
        self.assertEqual(r["verdict"], "PASS", r["reasons"])
        self.assertTrue(any(x.startswith("after costs (FX fee 0.15 % each way + slippage 0.05 % a fill): "
                                         "reward:risk") for x in r["info"]), r["info"])

    def test_a_usd_config_counts_no_costs_unless_it_says_so(self):
        cfg = {k: v for k, v in check.money_config(dict(GBP, costs={"fx_fee_pct": 0, "slippage_pct": 0}),
                                                    fx_rate=RATE).items()}
        res = check.check([bracket("6")], cfg, now=NOW_UTC)
        self.assertEqual(res["results"][0]["verdict"], "PASS", res["results"][0]["reasons"])
        self.assertIsNone(res["results"][0]["reward_risk_net"])

    def test_bad_costs_are_refused(self):
        with self.assertRaises(check.RiskError):
            check.check([bracket("6")], self.cfg(costs={"slippage_pct": 5}), now=NOW_UTC)

    def test_an_exit_is_never_blocked_by_costs(self):
        pf = {"cash": 1000, "positions": [{"ticker": "TSLA", "shares": 6}]}
        sell = {"symbol": "TSLA", "side": "sell", "qty": "6", "type": "market", "time_in_force": "day"}
        res = check.check([sell], self.cfg(), portfolio=pf, prices={"TSLA": 360.0}, now=NOW_UTC)
        self.assertEqual(res["results"][0]["verdict"], "PASS")


if __name__ == "__main__":
    unittest.main()
