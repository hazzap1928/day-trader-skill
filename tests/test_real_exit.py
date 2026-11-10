"""The real exit in the simulations (Time Machine and evidence virtual results), the open-risk daily
loss count, and the v1/v2 rules tag. Offline: fakes only, never the real config or Trading 212."""
import calendar
import os
import shutil
import tempfile
import unittest

from _load import load
import test_timemachine as T
import test_risk as R
from test_evidence import ev, bars, ts
from test_autopilot import PilotBase, ap

tm = T.tm
check = R.check
day_risk = load("trader-broker", "day_risk")

T212_CFG = {"broker": "trading212", "trailing_stop": {"enabled": True, "distance_r": 1.0, "sell_half_at_target": False},
            "eod_close": {"enabled": True, "at": "15:50"}}
EXITS = tm.exits_from(T212_CFG)


def walk(rows, entry=100.0, stop=99.0, target=102.0, exits=EXITS):
    g = tm.GuardExit(entry, stop, target, exits)
    for r in rows:
        g.bar(*r)
    return g.result()


class Exits(unittest.TestCase):
    def test_only_trading212_has_the_real_exit(self):
        self.assertIsNone(tm.exits_from({"broker": "alpaca"}))
        self.assertIsNone(tm.exits_from({}))
        self.assertEqual(EXITS, {"trail": {"distance_r": 1.0, "distance_pct": None, "activate_r": 1.0},
                                 "half": False, "eod": "15:50"})

    def test_half_needs_the_trail_like_the_guard(self):
        off = tm.exits_from({"broker": "trading212", "trailing_stop": {"enabled": False, "sell_half_at_target": True}})
        self.assertEqual((off["trail"], off["half"], off["eod"]), (None, False, None))
        on = tm.exits_from(dict(T212_CFG, trailing_stop={"enabled": True, "distance_r": 1, "sell_half_at_target": True}))
        self.assertTrue(on["half"])


class GuardExit(unittest.TestCase):
    def test_the_stop_and_a_gap_under_it(self):
        self.assertEqual(walk([(100.0, 100.5, 98.9, 99.2)]), (-1.0, "stop"))
        r, how = walk([(98.0, 98.5, 97.5, 98.0)])          # opens under the stop: sold at the open
        self.assertEqual((round(r, 2), how), (-2.0, "stop"))

    def test_no_target_exit_out_at_the_last_close(self):
        r, how = walk([(100.0, 102.5, 99.8, 100.5), (100.5, 100.9, 100.2, 100.6)])
        self.assertEqual((round(r, 2), how), (0.6, "close"))   # touched the 2R target, but it is not an exit

    def test_the_trail_starts_at_1r_and_trails_1r_under_the_highest_close(self):
        r, how = walk([(100.0, 100.9, 99.5, 100.8),           # under 1R profit: no raise
                       (100.8, 101.6, 100.7, 101.5),          # close 101.5 >= 101: stop to 100.50
                       (101.5, 102.6, 101.4, 102.5),          # close 102.5: stop to 101.50
                       (102.5, 102.6, 101.4, 101.6)])         # low 101.4 under 101.50: trailed out
        self.assertEqual((round(r, 2), how), (1.5, "trailed out"))

    def test_no_raise_smaller_than_the_step(self):
        g = tm.GuardExit(100.0, 99.0, 102.0, EXITS)
        g.bar(100.0, 101.1, 99.9, 101.0)
        self.assertEqual(g.cur, 100.0)                       # armed at 1R: stop 100.00
        g.bar(101.0, 101.1, 100.9, 101.05)                   # a 5 cent raise is under 0.1R (10 cents)
        self.assertEqual(g.cur, 100.0)

    def test_half_at_the_target_when_it_is_on(self):
        ex = dict(EXITS, half=True)
        r, how = walk([(100.0, 102.2, 99.9, 102.0), (102.0, 102.1, 99.5, 99.6)], exits=ex)
        # half at 102.00 (+1R on the whole), the rest stopped at the trail 101.00 (+0.5R)
        self.assertEqual((round(r, 2), how), (1.5, "trailed out"))

    def test_the_trail_off_is_the_stop_and_the_close_only(self):
        ex = {"trail": None, "half": False, "eod": "15:50"}
        r, how = walk([(100.0, 103.0, 99.5, 102.9), (102.9, 103.0, 101.0, 101.2)], exits=ex)
        self.assertEqual((round(r, 2), how), (1.2, "close"))


class Lean(unittest.TestCase):
    def test_the_rule_is_the_old_rule_on_two_outcomes(self):
        # wins of +2R and losses of -1R only: break-even 1 / (1 + 2), exactly the fixed-target rule
        mc = {"exit_model": "real", "p_win": 0.4, "p_loss": 0.6, "avg_win_R": 2.0, "avg_loss_R": -1.0}
        gap, share, even = tm.lean(mc)
        self.assertAlmostEqual(even, 1 / 3.0)
        self.assertAlmostEqual(gap, 0.4 - 1 / 3.0)
        old = tm.lean({"p_target": 0.4, "p_stop": 0.6, "r_at_target": 2.0})
        self.assertAlmostEqual(old[0], gap)

    def test_it_passes_exactly_when_the_average_r_is_positive(self):
        for pw, aw, al in ((0.5, 0.7, -0.68), (0.3, 1.0, -0.5), (0.6, 0.2, -0.5)):
            mc = {"exit_model": "real", "p_win": pw, "p_loss": 1 - pw, "avg_win_R": aw, "avg_loss_R": al}
            avg = pw * aw + (1 - pw) * al
            self.assertEqual(tm.lean(mc)[0] > 0, avg > 0, (pw, aw, al))


class Machine(unittest.TestCase):
    def test_the_real_exit_on_the_fixture(self):
        doc = T.machine(T.plan(target=375.90), exits=EXITS)
        m = doc["mc"]
        self.assertEqual((m["exit_model"], m["until"]), ("real", "15:50"))
        self.assertAlmostEqual(m["p_out_stop"] + m["p_out_trail"] + m["p_out_close"], 1.0, places=3)
        self.assertTrue(doc["verdict"].startswith("Close to break-even on the real exit (stop, 1R trail from 1R "
                                                  "profit, out at 15:50)"))
        # the same plan judged on a fixed 2R target leans strongly to the stop and would not pass
        old = T.machine(T.plan(target=375.90))
        self.assertLess(tm.lean(old["mc"])[0], 0)
        self.assertGreater(tm.lean(m)[0], 0)
        self.assertTrue(all("exit" in t for t in doc["history"]["trades"]))

    def test_the_target_does_not_change_the_real_result_without_half(self):
        a = T.machine(T.plan(target=375.90), exits=EXITS)["mc"]["avg_R"]
        b = T.machine(T.plan(target=369.0), exits=EXITS)["mc"]["avg_R"]
        self.assertEqual(a, b)

    def test_until_is_cut_to_the_end_of_day_close(self):
        doc = T.machine(T.plan(), until="15:55", exits=EXITS)
        self.assertEqual(doc["mc"]["until"], "15:50")

    def test_the_cli_reads_the_exits_from_the_config(self):
        import json
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "config.json")
            with open(p, "w") as fh:
                json.dump(T212_CFG, fh)
            code, out, err = T.run(["run", "--offline", os.path.join(T.DIR, "TSLA-60d5m.json.gz"), "--now", T.NOW,
                                    "--symbol", "TSLA", "--date", "2026-10-19", "--entry", "364.18", "--stop",
                                    "358.32", "--target", "375.90", "--entry-time", "10:15 AM", "--config", p])
            self.assertEqual(code, 0, err)
            self.assertIn("on the real exit", out)
        finally:
            shutil.rmtree(d)


class Virtual(unittest.TestCase):
    DAY = "2026-11-05"

    def result(self, rows, exits=EXITS):
        return ev.virtual_result(bars(self.DAY, rows), ts(2026, 11, 5, 10, 30), 100.0, 99.0, 102.0, exits=exits)

    def test_no_target_on_the_real_exit(self):
        rows = [("10:30", 100.0, 101.0, 99.5, 100.8), ("10:35", 100.8, 102.3, 100.5, 102.0),
                ("10:40", 102.0, 102.1, 100.9, 101.0)]
        self.assertEqual(ev.virtual_result(bars(self.DAY, rows), ts(2026, 11, 5, 10, 30), 100.0, 99.0, 102.0)["how"],
                         "target")
        r = self.result(rows)
        self.assertEqual((r["how"], r["exit"], r["R"], r["model"]), ("trailed out", 101.0, 1.0, "real exit"))

    def test_out_at_1550_on_the_real_exit(self):
        r = self.result([("10:30", 100.0, 100.5, 99.5, 100.3), ("15:45", 100.3, 100.6, 100.1, 100.5),
                         ("15:50", 100.5, 103.0, 100.4, 102.9)])
        self.assertEqual((r["how"], r["exit"]), ("15:50 close", 100.5))

    def test_v1_rows_keep_the_fixed_target_v2_rows_get_the_real_exit(self):
        d = tempfile.mkdtemp()
        try:
            rows = [("10:30", 100.0, 102.5, 99.5, 102.0), ("10:35", 102.0, 102.1, 100.9, 101.0)]
            base = {"day": self.DAY, "at": ts(2026, 11, 5, 10, 30), "entry": 100.0, "stop": 99.0, "target": 102.0}
            ev.append(d, "signals", dict(base, symbol="OLD", id="old"))         # written before the tag
            ev.log_signal(d, dict(base, symbol="NEW"))
            self.assertEqual(ev.resolve(d, self.DAY, lambda s: bars(self.DAY, rows), cfg=T212_CFG), 2)
            got = {r["symbol"]: r for r in ev.load(d, "signals")}
            self.assertEqual(got["OLD"]["virtual"]["how"], "target")
            self.assertEqual(got["NEW"]["virtual"]["how"], "trailed out")
            self.assertEqual((ev.rules_of(got["OLD"]), got["NEW"]["rules"]), ("v1", "v2"))
        finally:
            shutil.rmtree(d)


class Tags(unittest.TestCase):
    def test_tag_marks_old_rows_v1_and_new_rows_are_v2(self):
        d = tempfile.mkdtemp()
        try:
            ev.append(d, "signals", {"day": "2026-11-09", "symbol": "A", "id": "a"})
            ev.append(d, "trades", {"day": "2026-11-09", "symbol": "A"})
            ev.log_signal(d, {"day": "2026-11-11", "symbol": "B"})
            ev.log_trade(d, {"day": "2026-11-11", "symbol": "B"})
            ev.tag_lists(d, {})
            self.assertEqual([r["rules"] for r in ev.load(d, "signals")], ["v1", "v2"])
            self.assertEqual([r["rules"] for r in ev.load(d, "trades")], ["v1", "v2"])
        finally:
            shutil.rmtree(d)

    def test_the_report_shows_the_two_rule_sets(self):
        r = {"start": "2026-11-09", "today": "2026-11-13", "min_trades": 20, "real": [], "real_avg_gbp": None,
             "real_avg_ret": None, "real_total_gbp": 0.0, "signals": 2, "virtual": 2, "virtual_avg_R": 0.1,
             "virtual_avg_ret": 0.001, "virtual_win": 50.0, "slip_entry": None, "slip_exit": None, "slip_n": (0, 0),
             "kronos": {"agree": (0, None), "disagree": (0, None), "other": 2},
             "kronos_real": {"agree": 0, "disagree": 0}, "by_list": {}, "universe_from": "2026-11-10",
             "by_rules": {n: {"real": 0, "real_avg_gbp": None, "real_total_gbp": 0, "signals": 1, "virtual": 1,
                              "virtual_avg_R": v, "virtual_win": 100.0 if v > 0 else 0.0, "days": (a, b)}
                          for n, v, a, b in (("v1", -0.5, "2026-11-09", "2026-11-10"),
                                             ("v2", 0.7, "2026-11-11", "2026-11-13"))}}
        week = {"from": "2026-11-09", "to": "2026-11-13", "n": 0, "min_trades": 20, "avg_gbp": None, "total_gbp": 0.0,
                "start_value": None, "strategy_ret": None, "tracker_ret": None, "status": "TOO FEW TRADES TO JUDGE",
                "lean": None}
        r["week"] = r["since_start"] = week
        text = ev.report_text(r)
        self.assertIn("Old rules (2R target in the simulations, full-risk daily loss count), 2026-11-09 to 2026-11-10: "
                      "real 0 closed", text)
        self.assertIn("New rules (real exit in the simulations, open-risk daily loss count), 2026-11-11 to "
                      "2026-11-13", text)


def fp_placed(sym, fp, max_loss=46.88):
    return dict(R.placed(sym, max_loss=max_loss), fingerprint=fp, t212_ticker=sym + "_US_EQ", kind="entry",
                stop=99.0, entry=100.0)


def built_at(text):
    return calendar.timegm(R.at(text).timetuple())


class DailyLossCount(unittest.TestCase):
    THREE = {"orders": [fp_placed("AAPL", "f1"), fp_placed("AMD", "f2"), fp_placed("NVDA", "f3")]}

    def ledger(self, counted, at=R.NOW, day=R.TODAY):
        return {"date": day, "built_at": built_at(at),
                "entries": [{"fingerprint": f, "counted_usd": v} for f, v in counted.items()]}

    def blocked_daily(self, res):
        return "daily loss:" in R.reasons(res)

    def test_the_old_count_is_the_default(self):
        res = check.check([R.tsla()], R.cfg(max_trades_per_day=5), state=self.THREE, now=R.at(R.NOW),
                          day_risk=self.ledger({"f1": 0, "f2": 0, "f3": 0}))
        self.assertTrue(self.blocked_daily(res))
        self.assertEqual(res["today"]["daily_loss_count"], "full_risk")

    def test_closed_winners_free_their_risk(self):
        c = R.cfg(max_trades_per_day=5, daily_loss_count="open_risk")
        res = check.check([R.tsla()], c, state=self.THREE, now=R.at(R.NOW),
                          day_risk=self.ledger({"f1": 0, "f2": 0, "f3": 46.88}))
        self.assertEqual(R.verdicts(res), ["PASS"])
        self.assertIn("$46.88 of $150 daily loss", res["results"][0]["info"][-1])
        self.assertEqual(res["today"]["daily_loss_count"], "open_risk")

    def test_realised_losses_still_count(self):
        c = R.cfg(max_trades_per_day=5, daily_loss_count="open_risk")
        res = check.check([R.tsla()], c, state=self.THREE, now=R.at(R.NOW),
                          day_risk=self.ledger({"f1": 47.5, "f2": 47.0, "f3": 20.0}))
        self.assertTrue(self.blocked_daily(res))
        self.assertIn("lost or still at risk at the stops", R.reasons(res))

    def test_an_entry_missing_from_the_file_counts_in_full(self):
        c = R.cfg(max_trades_per_day=5, daily_loss_count="open_risk")
        res = check.check([R.tsla()], c, state=self.THREE, now=R.at(R.NOW), day_risk=self.ledger({"f1": 0}))
        self.assertIn("$93.76 of $150 daily loss", res["results"][0]["info"][-1])

    def test_a_stale_or_missing_file_falls_back_to_the_old_count(self):
        c = R.cfg(max_trades_per_day=5, daily_loss_count="open_risk")
        for doc in (None, self.ledger({"f1": 0, "f2": 0, "f3": 0}, at="2026-10-19 09:50"),
                    self.ledger({"f1": 0, "f2": 0, "f3": 0}, day="2026-10-16")):
            res = check.check([R.tsla()], c, state=self.THREE, now=R.at(R.NOW), day_risk=doc)
            self.assertTrue(self.blocked_daily(res))
            self.assertTrue(any("daily loss counted the old way" in w for w in res["warnings"]))

    def test_a_bad_setting_is_refused(self):
        with self.assertRaises(check.RiskError):
            check.check([R.tsla()], R.cfg(daily_loss_count="half"), state=self.THREE, now=R.at(R.NOW))


class DayRisk(unittest.TestCase):
    def entries(self):
        return [dict(fp_placed(s, "f" + s), max_loss=50.0) for s in ("WIN", "LOSE", "OPEN", "TRAIL", "SOFT", "WAIT")]

    def test_compute(self):
        closed = [{"ticker": "WIN_US_EQ", "pl": 30.0}, {"ticker": "LOSE_US_EQ", "pl": -32.0}]
        positions = [{"ticker": t + "_US_EQ", "quantity": 10, "averagePricePaid": 100.0} for t in ("OPEN", "TRAIL", "SOFT")]
        pending = [{"ticker": "OPEN_US_EQ", "type": "STOP", "status": "NEW", "stopPrice": 99.0},
                   {"ticker": "TRAIL_US_EQ", "type": "STOP", "status": "NEW", "stopPrice": 100.5}]
        guard = {"software": {"SOFT_US_EQ": {"stop": 99.5}}}
        doc = day_risk.compute(self.entries(), closed, positions, pending, guard, 1.25)
        got = {e["symbol"]: e for e in doc["entries"]}
        self.assertEqual(got["WIN"]["counted_usd"], 0.0)                       # a closed winner frees its risk
        self.assertEqual(got["LOSE"]["counted_usd"], 40.0)                     # £32 loss at 1.25
        self.assertEqual(got["OPEN"]["counted_usd"], round(10 * (1.0 + 0.0015 * 199.0), 2))
        self.assertEqual(got["TRAIL"]["counted_usd"], 0.0)                     # stop over the price paid: 0
        self.assertEqual((got["SOFT"]["stop_from"], got["SOFT"]["stop_now"]), ("software stop", 99.5))
        self.assertEqual((got["WAIT"]["state"], got["WAIT"]["counted_usd"]), ("not seen yet", 50.0))
        self.assertAlmostEqual(doc["counted_usd"], sum(e["counted_usd"] for e in doc["entries"]), places=1)

    def test_the_setting(self):
        self.assertEqual(day_risk.count_of({}), "full_risk")
        self.assertEqual(day_risk.count_of({"daily_loss_count": "open_risk"}), "open_risk")
        with self.assertRaises(ValueError):
            day_risk.count_of({"daily_loss_count": "x"})


class PilotDayRisk(PilotBase):
    def test_built_only_with_open_risk_and_removed_when_it_fails(self):
        dr = ap.mod("trader-broker", "day_risk")
        saved = dr.build
        calls = []
        try:
            dr.build = lambda *a, **k: calls.append(1)
            p = self.pilot()
            p.cfg = dict(self.cfg)
            p.day_risk()
            self.assertEqual(calls, [])
            p.cfg = dict(self.cfg, daily_loss_count="open_risk")
            p.day_risk()
            self.assertEqual(calls, [1])

            def boom(*a, **k):
                raise ValueError("history down")
            dr.build = boom
            self.write(os.path.join("state", "day-risk.json"), {"date": "x"})
            p.day_risk()
            self.assertFalse(os.path.exists(dr.path(self.dir)))
            self.assertTrue(any("counts the old way" in x for x in self.out))
        finally:
            dr.build = saved


if __name__ == "__main__":
    unittest.main()
