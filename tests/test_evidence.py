"""Evidence mode: the demo forward test. Demo only, the cost block off, a 1.0 after-costs minimum, every
signal logged with a virtual result, slippage measured, Kronos in shadow, and the Friday report.
Fakes for Trading 212, Yahoo, Kronos and the phone: no network, no real order."""

import calendar
import datetime
import io
import json
import os
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load
import test_t212_place
from test_t212_place import Base, FakeResp, CREDS, NOW, TODAY, HELD7, tsla_ticket
import test_autopilot
from test_autopilot import PilotBase, Pipeline, ap
from test_currency import GBP_CFG, RATE, NOW_UTC, bracket

t212 = test_t212_place.t212
ev = ap.evidence
ev._T212[:] = [t212]                 # the same module the fakes patch
check = load("trader-risk", "check")
go_live = load("trader-broker", "go_live")

EVIDENCE = {"start": "2026-11-02", "min_trades": 2}


def ts(y, mo, d, h, mi):
    """New York time in November (UTC-5) as a unix timestamp."""
    return float(calendar.timegm((y, mo, d, h + 5, mi, 0)))


def bars(day, rows):
    """[(New York HH:MM, open, high, low, close)] -> Yahoo-style bars on a November day."""
    y, mo, d = (int(x) for x in day.split("-"))
    return [(ts(y, mo, d, int(t[:2]), int(t[3:])), o, h, l, c) for t, o, h, l, c in rows]


class Setting(unittest.TestCase):
    def test_on_and_demo_only(self):
        cfg = {"autopilot": {"evidence_mode": True}, "paper": True}
        self.assertTrue(ev.on(cfg))
        ev.require_demo(cfg, "demo")
        for bad_cfg, env in ((cfg, "live"), (dict(cfg, paper=False), "demo"), (dict(cfg, live_enabled=True), "demo")):
            with self.assertRaises(ev.EvidenceError):
                ev.require_demo(bad_cfg, env)

    def test_a_start_date_and_a_trade_minimum_and_no_end_date(self):
        self.assertEqual(ev.settings({"evidence": EVIDENCE}), ("2026-11-02", 2))
        self.assertEqual(ev.settings({"evidence": {"start": "2026-11-09"}}), ("2026-11-09", ev.MIN_TRADES))
        self.assertEqual(ev.settings({"evidence": {"start": "2026-11-09", "end": "2026-11-20"}}),
                         ("2026-11-09", ev.MIN_TRADES))                      # an old end date is ignored
        for bad in ({"start": "soon"}, {}, {"start": "2026-11-02", "min_trades": 0},
                    {"start": "2026-11-02", "min_trades": "20"}, {"start": "2026-11-02", "min_trades": True}):
            with self.assertRaises(ev.EvidenceError):
                ev.settings({"evidence": bad})

    def test_the_report_is_due_every_friday_from_the_start_forever(self):
        cfg = {"autopilot": {"evidence_mode": True}, "evidence": EVIDENCE}
        self.assertTrue(ev.due(cfg, "2026-11-06", 4))
        self.assertFalse(ev.due(cfg, "2026-11-05", 3))
        self.assertTrue(ev.due(cfg, "2026-11-13", 4))
        self.assertTrue(ev.due(cfg, "2026-11-20", 4))
        self.assertTrue(ev.due(cfg, "2029-06-01", 4))
        self.assertFalse(ev.due(cfg, "2026-10-30", 4))                  # before the start
        self.assertFalse(ev.due({"evidence": EVIDENCE}, "2026-11-06", 4))

    def test_the_tracker_history_reaches_back_to_the_start(self):
        self.assertEqual(ev.tracker_range("2026-11-09", "2026-11-13"), "3mo")
        self.assertEqual(ev.tracker_range("2026-11-09", "2027-06-01"), "1y")
        self.assertEqual(ev.tracker_range("2026-11-09", "2032-01-01"), "10y")


class Risk(unittest.TestCase):
    def cfg(self, **kw):
        return check.money_config(dict({k: v for k, v in GBP_CFG.items() if k != "costs"}, **kw), fx_rate=RATE)

    def test_evidence_mode_uses_a_10_after_costs_minimum(self):
        o = bracket("6", target="376.00")                 # 2.02 before costs, about 1.25 after
        off = check.check([o], self.cfg(), now=NOW_UTC)["results"][0]
        self.assertEqual(off["verdict"], "BLOCK")
        on = check.check([o], self.cfg(autopilot={"evidence_mode": True}), now=NOW_UTC)["results"][0]
        self.assertEqual(on["verdict"], "PASS", on["reasons"])

    def test_below_10_after_costs_is_still_blocked_and_says_evidence_mode(self):
        res = check.check([bracket("6", target="370.04")], self.cfg(autopilot={"evidence_mode": True},
                                                                     min_reward_risk=1.0), now=NOW_UTC)["results"][0]
        self.assertTrue(any("under your minimum 1 (evidence mode)" in x for x in res["reasons"]), res["reasons"])

    def test_every_other_limit_still_applies(self):
        res = check.check([bracket("40", target="376.00")], self.cfg(autopilot={"evidence_mode": True}),
                          now=NOW_UTC)["results"][0]
        self.assertEqual(res["verdict"], "BLOCK")
        self.assertTrue(any(x.startswith("loss:") for x in res["reasons"]), res["reasons"])

    def test_a_live_config_refuses_evidence_mode(self):
        with self.assertRaises(check.RiskError):
            check.check([bracket("6")], self.cfg(autopilot={"evidence_mode": True}, paper=False), now=NOW_UTC)


class LiveRefuses(Base):
    def test_place_on_live_refuses_with_evidence_mode_on(self):
        self.write("config.json", dict(self.cfg, autopilot={"evidence_mode": True}, live_enabled=True, paper=False))
        code, _, err = self.place("1", "--live", environ=dict(CREDS, T212_ENV="live"))
        self.assertEqual(code, 2)
        self.assertIn("evidence mode", err)
        self.assertEqual(self.router.calls, [])

    def test_go_live_lists_the_demo_only_settings(self):
        problems, _ = go_live.preflight(dict(self.cfg, autopilot={"evidence_mode": True, "auto_buy": True},
                                             allowed_tickers=["TSLA"]), self.dir, "", lambda: (True, "OK"),
                                        os.path.join(self.dir, "none.plist"))
        self.assertTrue(any("demo-only settings" in p for p in problems), problems)


class Pilot(PilotBase):
    def setUp(self):
        super().setUp()
        self.cfg = dict(self.cfg, autopilot={"auto_buy": True, "evidence_mode": True}, evidence=EVIDENCE)
        self.write("config.json", self.cfg)
        self.kronos_calls = []
        self._kv = ev.kronos_verdict
        ev.kronos_verdict = lambda sig, **kw: self.kronos_calls.append(sig["symbol"]) or {"verdict": "agree", "avg_R_net": 0.2}

    def tearDown(self):
        ev.kronos_verdict = self._kv
        super().tearDown()

    def cycle(self):
        self.router.add("GET", "/equity/positions", FakeResp([]))
        p = self.pilot()
        self.account()
        return p, p.cycle()

    def signals(self):
        return ev.load(self.dir, "signals")

    def test_the_cost_block_is_ignored_and_the_trade_is_placed(self):
        self.pipe.net_r = -0.24                         # the Time Machine says it loses after costs
        p, line = self.cycle()
        self.assertEqual([x[1] for x in self.router.posts()], ["/equity/orders/market", "/equity/orders/stop"])
        sig = self.signals()[0]
        self.assertTrue(sig["cost_block_ignored"])
        self.assertEqual(sig["decision"], "traded")
        self.assertIn("BOUGHT (demo, automatic, EVIDENCE MODE)", self.phone.sent[-1][0])

    def test_every_signal_is_logged_with_its_plan_and_expectation(self):
        self.pipe.triggered = ("TSLA", "AAPL")
        self.pipe.verdict = "PASS"
        p, line = self.cycle()
        rows = self.signals()
        self.assertEqual([r["symbol"] for r in rows], ["TSLA", "AAPL"])
        r = rows[0]
        for k, v in (("entry", 364.18), ("stop", 358.32), ("target", 370.04), ("tm_avg_R_net", 0.12),
                     ("rr", 1.0), ("risk_verdict", "PASS"), ("day", TODAY)):
            self.assertEqual(r[k], v, k)
        self.assertEqual(rows[1]["decision"], "not placed: " + rows[1]["decision"][len("not placed: "):])

    def test_a_blocked_signal_is_logged_too(self):
        self.pipe.verdict = "BLOCK"
        p, line = self.cycle()
        self.assertEqual(self.router.posts(), [])
        self.assertTrue(self.signals()[0]["decision"].startswith("trader-risk BLOCKED"))

    def test_kronos_gives_a_verdict_on_each_signal_after_the_trade_is_placed(self):
        p, line = self.cycle()
        self.assertEqual(self.kronos_calls, ["TSLA"])
        self.assertEqual(self.signals()[0]["kronos"]["verdict"], "agree")
        self.assertEqual(len(self.router.posts()), 2)          # placed before Kronos was even asked

    def test_kronos_disagreeing_blocks_nothing(self):
        ev.kronos_verdict = lambda sig, **kw: {"verdict": "disagree", "avg_R_net": -0.5}
        p, line = self.cycle()
        self.assertEqual(len(self.router.posts()), 2)

    def test_signals_and_trades_say_which_list_they_came_from(self):
        p, line = self.cycle()
        self.assertEqual(self.signals()[0]["list_from"], "allowed_tickers")
        self.assertEqual(ev.load(self.dir, "trades")[0]["list_from"], "allowed_tickers")
        p.cfg = dict(p.cfg, allowed_from="universe")
        self.assertEqual(p.list_from(), "universe")

    def test_the_expected_entry_is_recorded_for_slippage(self):
        p, line = self.cycle()
        t = ev.load(self.dir, "trades")[0]
        self.assertEqual((t["symbol"], t["t212_ticker"], t["expected_entry"]), ("TSLA", "TSLA_US_EQ", 364.5))

    def test_status_says_evidence_mode(self):
        p = self.pilot()
        p.command("/status")
        self.assertIn("EVIDENCE MODE forward test", self.phone.sent[-1][0])

    def test_ready_needs_auto_buy_and_a_window(self):
        self.assertEqual(ap.check_ready(self.cfg, "demo", self.phone), [])
        bad = dict(self.cfg, autopilot={"evidence_mode": True})
        self.assertTrue(any("needs autopilot.auto_buy" in x for x in ap.check_ready(bad, "demo", self.phone)))
        bad = dict(self.cfg, evidence={"start": "x"})
        self.assertTrue(any("evidence.start" in x for x in ap.check_ready(bad, "demo", self.phone)))
        self.assertTrue(any("demo only" in x for x in ap.check_ready(self.cfg, "live", self.phone)))

    def test_off_means_no_log_and_the_cost_block_back(self):
        self.write("config.json", dict(self.cfg, autopilot={"auto_buy": True}))
        self.pipe.net_r = -0.24
        p, line = self.cycle()
        self.assertEqual(self.router.posts(), [])
        self.assertEqual(self.signals(), [])

    def test_friday_after_the_close_resolves_and_sends_the_report(self):
        sent = []
        saved = (ev.resolve, ev.send_report)
        ev.resolve = lambda sd: 3
        ev.send_report = lambda *a, **k: sent.append(1) or (True, "text", {"status": "ON TRACK"})
        try:
            self.clock[0] = calendar.timegm((2026, 11, 6, 21, 0, 0))           # Friday 16:00 New York
            p = self.pilot()
            p.evidence_close(object())
        finally:
            ev.resolve, ev.send_report = saved
        self.assertEqual(sent, [1])


class Virtual(unittest.TestCase):
    DAY = "2026-11-05"

    def result(self, rows, at=(10, 30), entry=100.0, stop=99.0, target=102.0):
        return ev.virtual_result(bars(self.DAY, rows), ts(2026, 11, 5, *at), entry, stop, target)

    def test_stop_first(self):
        r = self.result([("10:30", 100.0, 100.5, 99.6, 100.2), ("10:35", 100.2, 100.4, 98.8, 99.0)])
        self.assertEqual((r["how"], r["exit"], r["R"]), ("stop", 99.0, -1.0))
        self.assertLess(r["R_net"], -1.0)

    def test_target_first(self):
        r = self.result([("10:30", 100.0, 101.0, 99.5, 100.8), ("10:35", 100.8, 102.3, 100.5, 102.0)])
        self.assertEqual((r["how"], r["exit"], r["R"]), ("target", 102.0, 2.0))
        self.assertAlmostEqual(r["R_net"], 2.0 - 0.002 * (100 + 102) / 1.0, places=3)

    def test_out_at_1550(self):
        r = self.result([("10:30", 100.0, 100.5, 99.5, 100.3), ("15:45", 100.3, 100.6, 100.1, 100.5),
                         ("15:50", 100.5, 103.0, 100.4, 102.9)])
        self.assertEqual((r["how"], r["exit"]), ("15:50 close", 100.5))

    def test_the_entry_is_the_first_bar_after_the_signal(self):
        r = self.result([("10:25", 90.0, 90.0, 90.0, 90.0), ("10:30", 100.4, 100.5, 100.0, 100.2)], at=(10, 27))
        self.assertEqual(r["entry"], 100.4)

    def test_a_stop_and_target_in_one_bar_counts_as_the_stop(self):
        r = self.result([("10:30", 100.0, 102.5, 98.5, 101.0)])
        self.assertEqual(r["how"], "stop")

    def test_resolve_fills_in_todays_signals_once(self):
        import tempfile, shutil
        d = tempfile.mkdtemp()
        try:
            ev.log_signal(d, {"day": self.DAY, "symbol": "TSLA", "at": ts(2026, 11, 5, 10, 30), "entry": 100.0,
                              "stop": 99.0, "target": 102.0})
            calls = []
            fetch = lambda s: calls.append(s) or bars(self.DAY, [("10:30", 100.0, 102.5, 99.5, 102.0)])
            self.assertEqual(ev.resolve(d, self.DAY, fetch), 1)
            self.assertEqual(ev.resolve(d, self.DAY, fetch), 0)
            self.assertEqual(ev.load(d, "signals")[0]["virtual"]["how"], "target")
            self.assertEqual(calls, ["TSLA"])
        finally:
            shutil.rmtree(d)


class Kronos(unittest.TestCase):
    def test_agree_and_disagree(self):
        self.assertEqual(ev.kronos_verdict({}, run=lambda p: '{"avg_R_net": 0.3}')["verdict"], "agree")
        self.assertEqual(ev.kronos_verdict({}, run=lambda p: '{"avg_R_net": -0.1}')["verdict"], "disagree")

    def test_failures_never_raise(self):
        self.assertEqual(ev.kronos_verdict({}, run=lambda p: "not json")["verdict"], "error")
        self.assertEqual(ev.kronos_verdict({}, run=lambda p: '{"error": "few bars"}')["verdict"], "error")

        def boom(p):
            raise OSError("no python")
        self.assertEqual(ev.kronos_verdict({}, run=boom)["verdict"], "error")

    def test_library_chatter_on_stdout_does_not_lose_the_verdict(self):
        # 9 Nov: the model loader printed this before the JSON, and every verdict was "error"
        out = "Loading weights from local directory\nLoading weights from local directory\n" \
              '{"model": "Kronos-small", "avg_R_net": -0.768}\n'
        res = ev.kronos_verdict({}, run=lambda p: out)
        self.assertEqual((res["verdict"], res["model"]), ("disagree", "Kronos-small"))

    def test_the_shadow_script_writes_only_json_to_stdout(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "trader-kronos", "kronos_shadow.py")) as fh:
            src = fh.read()
        main = src[src.index("def main():"):]
        self.assertIn("redirect_stdout(sys.stderr)", main)
        self.assertLess(main.index("redirect_stdout(sys.stderr)"), main.index("load_predictor()"))
        self.assertNotIn("print(", main)

    def test_not_set_up_is_unavailable(self):
        saved = ev.KRONOS_PY
        ev.KRONOS_PY = "/nonexistent/python"
        try:
            self.assertEqual(ev.kronos_verdict({"symbol": "X"})["verdict"], "unavailable")
        finally:
            ev.KRONOS_PY = saved

    def test_the_shadow_script_is_kept_apart(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, ".gitignore")) as fh:
            ig = fh.read()
        for p in ("trader-kronos/.venv/", "trader-kronos/vendor/", "trader-kronos/models/"):
            self.assertIn(p, ig)
        with open(os.path.join(root, "trader-kronos", "kronos_shadow.py")) as fh:
            src = fh.read()
        for word in ("/equity/orders", "trading212.com", "import t212", "post_order"):
            self.assertNotIn(word, src)


class Slippage(unittest.TestCase):
    def fill(self, side, price, typ="MARKET", oid=1, stop=None, at=datetime.datetime(2026, 11, 5, 15, 31)):
        return {"side": side, "ticker": "TSLA_US_EQ", "price": price, "type": typ, "order_id": oid,
                "stop_price": stop, "at": at}

    def test_entries_exits_and_the_guards_price_seen(self):
        trades = [{"t212_ticker": "TSLA_US_EQ", "expected_entry": 100.0, "at": calendar.timegm((2026, 11, 5, 15, 30, 0))}]
        fills = [self.fill("BUY", 100.1, oid=1), self.fill("SELL", 98.9, "STOP", oid=2, stop=99.0),
                 self.fill("SELL", 101.9, oid=3)]
        guard = {"eod": {"sold": [{"order_id": 3, "price_seen": 102.0}]}}
        ent, ex = ev.slippage(trades, fills, guard)
        self.assertAlmostEqual(ent[0], 0.001)
        self.assertAlmostEqual(ex[0], 1 - 98.9 / 99.0)
        self.assertAlmostEqual(ex[1], 1 - 101.9 / 102.0)


class FakeReport(object):
    """Stands in for report.py: one closed round trip, given."""

    def __init__(self, trips, fills=()):
        self.trips, self.fills = trips, list(fills)

    def history(self, client, since):
        return self.fills

    def round_trips(self, fills):
        return self.trips, {}


class Report(Base):
    def setUp(self):
        super().setUp()
        self.cfg = dict(self.cfg, autopilot={"evidence_mode": True, "auto_buy": True}, evidence=EVIDENCE)
        self.write("state/account-days.json", {"days": {"2026-11-02": {"start": 2000.0, "close": 2001.0}}})
        self.client = t212.Client("demo", "k", "s", state_dir=self.dir, log=lambda m: None)

    def trip(self, pl, cost=500.0, at=None):
        at = at or datetime.datetime(2026, 11, 4, 16, 0)
        return {"ticker": "TSLA_US_EQ", "qty": 1, "entry": 100, "exit": 101, "pl": pl, "cost_gbp": cost,
                "entry_at": at, "exit_at": at, "sell": {}}

    def report(self, trips, today, spy=(100.0, 101.0)):
        daily = lambda s: [(calendar.timegm((2026, 11, 2, 14, 30, 0)), spy[0], 0, 0, 100.5),
                           (calendar.timegm((2026, 11, 6, 14, 30, 0)), 100.5, 0, 0, spy[1])]
        return ev.build_report(self.client, self.dir, self.cfg, FakeReport(trips), fetch_daily=daily, today=today)

    def test_on_track_needs_both_tests(self):
        r = self.report([self.trip(12.0), self.trip(8.0)], "2026-11-06")
        self.assertEqual(r["status"], "ON TRACK")                     # +1.0% vs the tracker's +0.9%
        self.assertAlmostEqual(r["strategy_ret"], 0.01)
        r = self.report([self.trip(12.0), self.trip(8.0)], "2026-11-06", spy=(100.0, 103.0))
        self.assertEqual(r["status"], "NOT ON TRACK")
        r = self.report([self.trip(-5.0), self.trip(1.0)], "2026-11-06")
        self.assertEqual(r["status"], "NOT ON TRACK")

    def test_there_is_no_final_pass_or_fail_day(self):
        for today in ("2026-11-13", "2026-11-20", "2027-03-05"):
            r = self.report([self.trip(30.0), self.trip(1.0)], today)
            self.assertIn(r["status"], ("ON TRACK", "NOT ON TRACK"))
            self.assertNotIn("final", r)
            self.assertNotIn("PASS", ev.report_text(r))
            self.assertNotIn("FAIL", ev.report_text(r))

    def test_too_few_trades_to_judge_below_the_minimum(self):
        r = self.report([], "2026-11-06")
        self.assertEqual((r["status"], r["since_start"]["lean"], r["since_start"]["n"]), ("TOO FEW TRADES TO JUDGE", None, 0))
        r = self.report([self.trip(30.0)], "2026-11-06")
        self.assertEqual((r["status"], r["since_start"]["lean"]), ("TOO FEW TRADES TO JUDGE", "ON TRACK"))
        self.cfg = dict(self.cfg, evidence=dict(EVIDENCE, min_trades=1))
        self.assertEqual(self.report([self.trip(30.0)], "2026-11-06")["status"], "ON TRACK")
        self.cfg = dict(self.cfg, evidence={"start": "2026-11-02"})       # the default minimum
        self.assertEqual(self.report([self.trip(30.0)] * 3, "2026-11-06")["status"], "TOO FEW TRADES TO JUDGE")

    def test_this_week_and_since_the_start_each_get_a_status(self):
        self.write("state/account-days.json", {"days": {"2026-11-02": {"start": 2000.0},
                                                        "2026-11-09": {"start": 2040.0}}})
        week2 = datetime.datetime(2026, 11, 10, 16, 0)
        daily = lambda s: [(calendar.timegm((2026, 11, 2, 14, 30, 0)), 100.0, 0, 0, 100.5),
                           (calendar.timegm((2026, 11, 9, 14, 30, 0)), 101.0, 0, 0, 101.0),
                           (calendar.timegm((2026, 11, 13, 14, 30, 0)), 101.0, 0, 0, 101.2)]
        trips = [self.trip(30.0), self.trip(20.0), self.trip(-3.0, at=week2), self.trip(-2.0, at=week2)]
        r = ev.build_report(self.client, self.dir, self.cfg, FakeReport(trips), fetch_daily=daily, today="2026-11-13")
        w, a = r["week"], r["since_start"]
        self.assertEqual((w["from"], w["to"], w["n"], w["total_gbp"], w["status"]), ("2026-11-09", "2026-11-13", 2, -5.0, "NOT ON TRACK"))
        self.assertEqual((a["from"], a["n"], a["total_gbp"], a["status"]), ("2026-11-02", 4, 45.0, "ON TRACK"))
        self.assertAlmostEqual(w["strategy_ret"], -5.0 / 2040.0)
        text = ev.report_text(r)
        self.assertIn("This week (2026-11-09 to 2026-11-13): NOT ON TRACK, based on 2 closed trades.", text)
        self.assertIn("Since the start (2026-11-02 to 2026-11-13): ON TRACK, based on 4 closed trades.", text)
        self.assertIn("no end date", text)
        r = ev.build_report(self.client, self.dir, self.cfg, FakeReport(trips[:2]), fetch_daily=daily, today="2026-11-13")
        self.assertIn("This week (2026-11-09 to 2026-11-13): too few trades to judge (0 closed trades, minimum 2).",
                      ev.report_text(r))

    def test_the_week_never_reaches_back_before_the_start(self):
        self.cfg = dict(self.cfg, evidence=dict(EVIDENCE, start="2026-11-04"))
        self.assertEqual(self.report([], "2026-11-06")["week"]["from"], "2026-11-04")

    def test_the_text_covers_everything_asked_for(self):
        ev.log_signal(self.dir, {"day": "2026-11-04", "symbol": "A", "entry": 1, "stop": 0.9, "target": 1.2,
                                 "virtual": {"R_net": 0.5, "ret_net": 0.04}, "kronos": {"verdict": "agree"}})
        ev.log_signal(self.dir, {"day": "2026-11-04", "symbol": "B", "entry": 1, "stop": 0.9, "target": 1.2,
                                 "virtual": {"R_net": -1.1, "ret_net": -0.1}, "kronos": {"verdict": "disagree"}})
        text = ev.report_text(self.report([self.trip(30.0)], "2026-11-06"))
        for want in ("EVIDENCE MODE REPORT", "Since the start (2026-11-02 to 2026-11-06): too few trades to judge "
                     "(1 closed trade, minimum 2); so far it reads ON TRACK",
                     "Real trades since the start: 1 closed, average +GBP 30.00 (+6.00%)",
                     "Virtual signals: 2 logged, 2 resolved, average -0.300R", "Slippage, adverse:",
                     "assumed 0.05% a fill", "strategy +1.50% (on +GBP 2,000.00) vs S&P 500 tracker +0.90%",
                     "agreed on 1 signals (virtual +0.500R), disagreed on 1 (virtual -1.100R)"):
            self.assertIn(want, text)

    def test_untagged_rows_are_tagged_by_the_switch_day(self):
        cfg = dict(self.cfg, evidence=dict(EVIDENCE, universe_from="2026-11-05"))
        for day, sym in (("2026-11-04", "A"), ("2026-11-05", "B")):
            ev.log_signal(self.dir, {"day": day, "symbol": sym})
        ev.log_signal(self.dir, {"day": "2026-11-06", "symbol": "C", "list_from": "allowed_tickers"})
        self.assertEqual(ev.tag_lists(self.dir, cfg), 2)
        self.assertEqual([r["list_from"] for r in ev.load(self.dir, "signals")],
                         ["allowed_tickers", "universe", "allowed_tickers"])     # an existing tag is kept
        self.assertEqual(ev.tag_lists(self.dir, cfg), 0)
        self.assertEqual(ev.universe_from({}), "2026-11-10")

    def test_the_report_shows_before_and_after_the_universe_separately(self):
        self.cfg = dict(self.cfg, evidence=dict(EVIDENCE, universe_from="2026-11-05"))
        ev.log_signal(self.dir, {"day": "2026-11-04", "symbol": "A", "virtual": {"R_net": 0.5, "ret_net": 0.04}})
        ev.log_signal(self.dir, {"day": "2026-11-05", "symbol": "B", "virtual": {"R_net": -1.0, "ret_net": -0.1}})
        ev.log_signal(self.dir, {"day": "2026-11-06", "symbol": "C", "virtual": {"R_net": 0.2, "ret_net": 0.01}})
        ev.log_trade(self.dir, {"day": "2026-11-05", "t212_ticker": "AAPL_US_EQ", "list_from": "allowed_tickers"})
        late = dict(self.trip(-4.0), ticker="AAPL_US_EQ",
                    entry_at=datetime.datetime(2026, 11, 5, 16, 0), exit_at=datetime.datetime(2026, 11, 5, 17, 0))
        later = dict(self.trip(6.0), ticker="MSFT_US_EQ",
                     entry_at=datetime.datetime(2026, 11, 6, 16, 0), exit_at=datetime.datetime(2026, 11, 6, 17, 0))
        r = self.report([self.trip(10.0), late, later], "2026-11-06")
        before, after = r["by_list"]["allowed_tickers"], r["by_list"]["universe"]
        self.assertEqual((before["real"], before["real_total_gbp"]), (2, 6.0))   # the tagged trade beats its day
        self.assertEqual((after["real"], after["real_total_gbp"]), (1, 6.0))
        self.assertEqual((before["signals"], after["signals"]), (1, 2))
        self.assertAlmostEqual(after["virtual_avg_R"], -0.4)
        text = ev.report_text(r)
        self.assertIn("Hand list (allowed_tickers), before 2026-11-05: real 2 closed, average +GBP 3.00, "
                      "total +GBP 6.00; virtual 1 logged, 1 resolved, average +0.500R, 100% won", text)
        self.assertIn("Filtered universe, from 2026-11-05: real 1 closed", text)
        self.assertTrue(text.rstrip().endswith("not financial advice."))


class GuardRecordsWhatItSaw(Base):
    def test_the_end_of_day_close_records_the_price_it_saw_and_the_order(self):
        client = t212.Client("demo", CREDS["T212_API_KEY"], CREDS["T212_API_SECRET"], state_dir=self.dir,
                             log=lambda m: None)
        self.router.add("GET", "/equity/positions", FakeResp(HELD7))
        self.router.add("GET", "/equity/orders", FakeResp([]))
        self.router.add("POST", "/equity/orders/market", FakeResp({"id": 9300}))
        with redirect_stdout(io.StringIO()):
            t212.eod_close(client, self.dir)
        sold = self.read("state/t212-guard.json")["eod"]["sold"]
        self.assertEqual(sold, [{"ticker": "TSLA_US_EQ", "qty": 7, "price_seen": 366.0, "order_id": 9300}])


if __name__ == "__main__":
    unittest.main()
