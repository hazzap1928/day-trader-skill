"""Kronos opinions and stop shadows on the holdings pages (trader-broker/shadow.py), the news ideas' Kronos
opinion, the autopilot's evening start, and the review checkpoint script. Offline: fake bars, a fake Kronos,
no network, no real state."""

import datetime
import io
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _load import ROOT, load  # noqa: E402

sh = load("trader-broker", "shadow")
cp = load("research", "checkpoint_review")


def bars_fn(prices=None, calls=None):
    """Fake Yahoo daily bars: every weekday from start to end; close = prices.get(day, 100)."""
    def fn(symbol, start, end):
        if calls is not None:
            calls.append((symbol, start, end))
        out, d = {}, start
        while d <= end:
            if d.weekday() < 5:
                c = (prices or {}).get(d.isoformat(), 100.0)
                out[d] = (c, c * 1.01, c * 0.99, c, 1e6)
            d += datetime.timedelta(days=1)
        return out
    return fn


def page(pid, strategy="drift", status="open", buy="2026-11-20", sell="2026-12-18", result=None, **kw):
    p = {"id": pid, "symbol": pid.split("-")[0], "strategy": strategy, "status": status, "buy_date": buy,
         "sell_rule": {"date": sell}, "qty": 2.0, "cost_gbp": 200.0, "stop": 90.0, "result": result,
         "broker_ref": {"ref": pid.split("-")[0] + "_US_EQ"}}
    p.update(kw)
    return p


class Base(unittest.TestCase):
    def setUp(self):
        self.sd = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.sd, "state"))
        self.said = []

    def tearDown(self):
        shutil.rmtree(self.sd)

    def write(self, pages):
        with open(os.path.join(self.sd, "state", "holdings.json"), "w") as fh:
            json.dump({"schema": 1, "since": "2026-11-01", "pages": pages, "reported": {}}, fh)

    def pages(self):
        with open(os.path.join(self.sd, "state", "holdings.json")) as fh:
            return {p["id"]: p for p in json.load(fh)["pages"]}

    def say(self, *a):
        self.said.append(" ".join(map(str, a)))


class KronosOpinion(Base):
    def fake_run(self, seen, score=0.03):
        def run(payload):
            req = json.loads(payload)
            seen.append(req)
            return "noise\n" + json.dumps({"model": "Kronos-small/Tokenizer-base", "load_seconds": 4.0, "results": [
                {"id": it["id"], "score": score, "path_returns": [score] * 5, "paths": 5, "pred_len": len(it["future"]),
                 "as_of": it["bars"][-1][0], "seconds": 14.0} for it in req["items"]]})
        return run

    def test_drift_and_news_buys_get_an_opinion_from_bars_before_the_buy_day(self):
        self.write([page("P-1"), page("NVDA-2", strategy="news", buy="2026-11-23", sell="2026-12-02"),
                    page("DIS-3", strategy="intraday"), page("AAPL-4", status="waiting")])
        seen = []
        n = sh.kronos_pass(self.sd, bars_fn=bars_fn(), run=self.fake_run(seen), say=self.say)
        self.assertEqual(n, 2)
        items = {it["id"]: it for it in seen[0]["items"]}
        self.assertEqual(sorted(items), ["NVDA-2", "P-1"])          # no day trade, no unfilled buy
        self.assertEqual(items["P-1"]["bars"][-1][0], "2026-11-19")  # nothing from the buy day or after
        self.assertEqual(len(items["P-1"]["future"]), 20)            # 20 trading days held, Thanksgiving skipped
        self.assertEqual(items["P-1"]["future"][0], "2026-11-20")
        self.assertEqual(items["P-1"]["future"][-1], "2026-12-18")
        pg = self.pages()
        self.assertEqual(pg["P-1"]["kronos"]["score"], 0.03)
        self.assertEqual(pg["P-1"]["kronos"]["model"], "Kronos-small/Tokenizer-base")
        self.assertNotIn("kronos", pg["DIS-3"])
        self.assertNotIn("kronos", pg["AAPL-4"])
        # nothing else on the page changed
        self.assertEqual(pg["P-1"]["status"], "open")
        self.assertEqual(pg["P-1"]["stop"], 90.0)
        self.assertEqual(pg["P-1"]["sell_rule"], {"date": "2026-12-18"})

    def test_an_opinion_is_written_once(self):
        self.write([page("P-1")])
        seen = []
        sh.kronos_pass(self.sd, bars_fn=bars_fn(), run=self.fake_run(seen), say=self.say)
        self.assertEqual(sh.kronos_pass(self.sd, bars_fn=bars_fn(), run=self.fake_run(seen), say=self.say), 0)
        self.assertEqual(len(seen), 1)

    def test_a_failure_is_tried_three_times_then_left(self):
        self.write([page("P-1")])

        def boom(payload):
            raise RuntimeError("model missing")
        for _ in range(4):
            sh.kronos_pass(self.sd, bars_fn=bars_fn(), run=boom, say=self.say)
        k = self.pages()["P-1"]["kronos"]
        self.assertEqual(k["tries"], 3)
        self.assertIn("model missing", k["error"])

    def test_an_item_left_out_by_the_time_budget_waits_for_next_time(self):
        self.write([page("P-1"), page("Q-2")])

        def run(payload):
            it = json.loads(payload)["items"][0]
            return json.dumps({"model": "m", "results": [{"id": it["id"], "score": -0.01, "pred_len": 20}]})
        self.assertEqual(sh.kronos_pass(self.sd, bars_fn=bars_fn(), run=run, say=self.say), 1)
        self.assertNotIn("kronos", self.pages()["Q-2"])

    def test_without_the_kronos_setup_nothing_happens(self):
        self.write([page("P-1")])
        old = sh.KRONOS_PY
        sh.KRONOS_PY = os.path.join(self.sd, "no-python")
        try:
            self.assertEqual(sh.kronos_pass(self.sd, bars_fn=bars_fn(), say=self.say), 0)
        finally:
            sh.KRONOS_PY = old
        self.assertNotIn("kronos", self.pages()["P-1"])
        self.assertIn("not set up", self.said[0])


class Replay(Base):
    """The Time Machine for holds on each drift and news page (shadow only)."""

    def test_each_holding_gets_one_replay_from_bars_before_its_buy_day(self):
        self.write([page("AAA-1", buy_price=100.0), page("BBB-1", strategy="news", buy_price=100.0),
                    page("CCC-1", strategy="intraday", buy_price=100.0), page("DDD-1", status="waiting", buy_price=100.0)])
        calls = []
        self.assertEqual(sh.replay_pass(self.sd, bars_fn=bars_fn(calls=calls), say=self.say), 4)   # 2 replays + 2 charts
        pages = self.pages()
        r = pages["AAA-1"]["replay"]
        self.assertEqual((r["runs"], r["days"], r["stop"]), (1000, 20, 90.0))     # 20 Nov to 18 Dec, holiday out
        self.assertEqual(set(r["rules"]), {"fixed", "rising", "none"})
        self.assertLess(r["as_of"], "2026-11-20")
        self.assertTrue(all(end < datetime.date(2026, 11, 20) for _, _, end in calls))   # no hindsight
        self.assertNotIn("replay", pages["CCC-1"])
        self.assertNotIn("replay", pages["DDD-1"])
        self.assertEqual(sh.replay_pass(self.sd, bars_fn=bars_fn(), say=self.say), 0)    # once a page

    def test_chart_labels_come_with_the_replay_from_one_fetch(self):
        self.write([page("AAA-1", buy_price=100.0)])
        calls = []
        self.assertEqual(sh.replay_pass(self.sd, bars_fn=bars_fn(calls=calls), say=self.say), 2)
        self.assertEqual(len(calls), 1)
        c = self.pages()["AAA-1"]["chart"]
        self.assertEqual((c["version"], c["falling"]), (1, False))
        self.assertLess(c["as_of"], "2026-11-20")
        rv = load("trader-broker", "review")
        M = load("trader-broker", "messages")
        got = rv._chart({"chart": c})
        self.assertTrue(got["new"])
        self.assertIn("Chart when bought:", M.chart_words(got))
        self.assertIn("⚠️", M.chart_words(dict(got, falling=True, tags=["below_ma50"])))
        self.assertEqual(M.chart_words(dict(got, new=False)), "")
        self.assertIsNone(rv._chart({}))

    def test_a_failure_is_tried_three_times_then_left(self):
        self.write([page("AAA-1")])                                    # no buy price
        for _ in range(4):
            sh.replay_pass(self.sd, bars_fn=bars_fn(), say=self.say)
        self.assertEqual(self.pages()["AAA-1"]["replay"]["tries"], 3)
        self.assertIn("no buy price", self.pages()["AAA-1"]["replay"]["error"])

    def test_it_changes_nothing_but_its_own_field(self):
        self.write([page("AAA-1", buy_price=100.0)])
        before = self.pages()["AAA-1"]
        sh.replay_pass(self.sd, bars_fn=bars_fn(), say=self.say)
        after = dict(self.pages()["AAA-1"])
        after.pop("replay")
        after.pop("chart")
        self.assertEqual(after, before)

    def test_the_review_and_phone_words(self):
        rv = load("trader-broker", "review")
        M = load("trader-broker", "messages")
        import time as _t
        rp = {"rules": {"fixed": {"median_pct": 1.2}, "rising": {"median_pct": 0.4}, "none": {"median_pct": 1.9}},
              "stop_first_pct": 31.4, "runs": 1000, "at": _t.time()}
        got = rv._replay({"replay": rp})
        self.assertTrue(got["new"])
        self.assertEqual(M.replay_words(got), ". Replaying its own past ups and downs: the safety net was hit first 31% of "
                         "the time; typical result after fees +1.2% with the safety net as set, +0.4% if it rose with the "
                         "price, +1.9% with none (for information)")
        old = rv._replay({"replay": dict(rp, at=_t.time() - 3 * 86400)})
        self.assertFalse(old["new"])
        self.assertEqual(M.replay_words(old), "")                      # told once on the phone
        self.assertIsNone(rv._replay({"replay": {"error": "x", "tries": 1}}))


class StopShadow(Base):
    stop_result = {"date": "2026-11-24", "price": 90.0, "why": "stop", "pl_gbp": -15.0, "qty": 2.0}

    def test_a_stop_sale_is_followed_to_its_planned_sell_date(self):
        self.write([page("P-1", status="closed", result=dict(self.stop_result))])
        prices = {"2026-12-18": 99.0}
        sh.shadow_pass(self.sd, datetime.date(2026, 11, 25), bars_fn=bars_fn(prices), usd_per_gbp=1.25, say=self.say)
        s = self.pages()["P-1"]["stop_shadow"]
        self.assertEqual(s["status"], "waiting")
        self.assertEqual((s["stop_exit_price"], s["stop_exit_date"], s["planned_sell_date"]),
                         (90.0, "2026-11-24", "2026-12-18"))
        # on the sell date itself the close is not final yet
        sh.shadow_pass(self.sd, datetime.date(2026, 12, 18), bars_fn=bars_fn(prices), usd_per_gbp=1.25, say=self.say)
        self.assertEqual(self.pages()["P-1"]["stop_shadow"]["status"], "waiting")
        sh.shadow_pass(self.sd, datetime.date(2026, 12, 21), bars_fn=bars_fn(prices), usd_per_gbp=1.25, say=self.say)
        s = self.pages()["P-1"]["stop_shadow"]
        self.assertEqual(s["status"], "done")
        self.assertEqual(s["price_on_sell_date"], 99.0)
        self.assertEqual(s["diff_per_share"], 9.0)
        self.assertAlmostEqual(s["diff_pct"], 0.1)
        self.assertEqual(s["diff_gbp"], 14.4)                 # 2 shares x $9 / 1.25
        self.assertEqual(self.pages()["P-1"]["status"], "closed")
        self.assertEqual(self.pages()["P-1"]["result"], self.stop_result)

    def test_a_software_stop_sale_counts_too(self):
        res = dict(self.stop_result, why="sold by the guard")
        self.write([page("P-1", status="closed", result=res)])
        guard = {"sold": [{"at": "2026-11-24T10:15:00-05:00", "ticker": "P_US_EQ", "why": "stop"}]}
        sh.shadow_pass(self.sd, datetime.date(2026, 11, 25), bars_fn=bars_fn(), guard_state=guard, say=self.say)
        self.assertEqual(self.pages()["P-1"]["stop_shadow"]["status"], "waiting")

    def test_other_sales_and_day_trades_get_no_shadow(self):
        self.write([page("P-1", status="closed", result=dict(self.stop_result, why="sold at the close")),
                    page("DIS-2", strategy="intraday", status="closed", result=dict(self.stop_result)),
                    page("Q-3")])
        self.assertEqual(sh.shadow_pass(self.sd, datetime.date(2026, 12, 21), bars_fn=bars_fn(), say=self.say), 0)
        self.assertFalse(any("stop_shadow" in p for p in self.pages().values()))


class NewsIdeaKronos(unittest.TestCase):
    def test_an_idea_gets_the_daily_forecast_to_its_sell_date(self):
        nw = load("trader-broker", "news_ideas")
        seen = []
        fake = types.SimpleNamespace(
            kronos_available=lambda: True, _bars_fn=lambda: bars_fn(),
            kronos_item=lambda p, fn: seen.append(p) or {"id": p["id"]},
            run_kronos=lambda items, budget_s: {"model": "m", "results": [{"id": items[0]["id"], "score": -0.02}]})
        nw._MODS[("trader-broker", "shadow")] = fake
        v = nw.kronos_for({"id": "2026-11-23-VRTX", "symbol": "VRTX", "day": "2026-11-23", "sell_date": "2026-12-03",
                           "price": 400, "stop": 380}, 300)
        self.assertEqual(seen[0]["buy_date"], "2026-11-23")
        self.assertEqual(seen[0]["sell_rule"], {"date": "2026-12-03"})
        self.assertEqual((v["verdict"], v["score"]), ("down", -0.02))


class AutopilotStart(unittest.TestCase):
    def test_started_only_from_the_run_script_and_never_waited_for(self):
        ap = load("trader-broker", "autopilot")
        sd = tempfile.mkdtemp()
        calls = []
        old = ap._spawn
        ap._spawn = lambda cmd, out: calls.append(cmd)
        pilot = types.SimpleNamespace(sd=sd, said=[], client=types.SimpleNamespace(_secrets=[]))
        pilot.say = pilot.said.append
        try:
            os.environ.pop("AUTOPILOT_SHADOW", None)
            ap.Pilot.shadow_start(pilot)
            self.assertEqual(calls, [])
            os.environ["AUTOPILOT_SHADOW"] = "1"
            ap.Pilot.shadow_start(pilot)
            self.assertEqual(len(calls), 1)
            self.assertTrue(calls[0][1].endswith("shadow.py") and calls[0][2] == "run")
        finally:
            os.environ.pop("AUTOPILOT_SHADOW", None)
            ap._spawn = old
            shutil.rmtree(sd)

    def test_the_run_script_turns_it_on(self):
        with open(os.path.join(ROOT, "trader-broker", "run-autopilot.sh")) as fh:
            self.assertIn("export AUTOPILOT_SHADOW=1", fh.read())

    def test_shadow_code_never_touches_orders(self):
        with open(os.path.join(ROOT, "trader-broker", "shadow.py")) as fh:
            src = fh.read()
        for word in ("cmd_place", "guard_send", "client.", "\"t212\")", "POST", "cancel"):
            self.assertNotIn(word, src.split('"""', 2)[2], word)


class Checkpoint(unittest.TestCase):
    def closed(self, pid, strategy, pl, score=None, buy="2026-11-02", sold="2026-11-30", shadow=None):
        p = page(pid, strategy=strategy, status="closed", buy=buy,
                 result={"date": sold, "price": 100, "why": "sold at the close", "pl_gbp": pl})
        if score is not None:
            p["kronos"] = {"score": score}
        if shadow:
            p["stop_shadow"] = shadow
        return p

    def test_no_trades_says_so(self):
        out = cp.report([])
        self.assertIn("NOT ENOUGH TRADES YET", out)
        self.assertIn("mid to late January", out)
        self.assertIn("Nothing to compare yet", out)

    def test_a_few_trades_are_compared_but_not_judged(self):
        spy = bars_fn({"2026-11-30": 102.0})("SPY", datetime.date(2026, 10, 25), datetime.date(2026, 12, 5))
        pages = [self.closed("A-1", "news", 10.0, 0.05), self.closed("B-2", "news", -4.0, -0.03),
                 self.closed("C-3", "drift", 2.0), page("D-4", strategy="intraday", status="closed",
                                                         result={"pl_gbp": 50.0, "date": "2026-11-03"})]
        out = cp.report(pages, spy)
        self.assertIn("3 finished trades (1 earnings, 2 news)", out)
        self.assertIn("News ideas: 2 finished, 1 made money. Total +GBP 6.00, average +1.5% a trade", out)
        self.assertIn("the S&P 500 averaged +1.9%", out)
        self.assertIn("Too few trades to judge", out)
        self.assertIn("rated highest averaged +5.0%", out)
        self.assertIn("Too few to decide", out)

    def test_at_the_checkpoint_kronos_is_judged(self):
        good = [self.closed("H%d-%d" % (i, i), "news", 10.0, 0.05 + i / 1000) for i in range(10)]
        bad = [self.closed("L%d-%d" % (i, i), "news", -2.0, -0.05 - i / 1000) for i in range(10)]
        self.assertIn("did clearly better", cp.report(good + bad))
        flat = [self.closed("F%d-%d" % (i, i), "news", 1.0, i / 100) for i in range(20)]
        self.assertIn("remove Kronos", cp.report(flat))

    def test_stop_recommendation(self):
        shadow = {"status": "done", "diff_pct": 0.06, "diff_gbp": 5.0}
        pages = [self.closed("S%d-%d" % (i, i), "news", -5.0, shadow=dict(shadow)) for i in range(20)]
        out = cp.report(pages)
        self.assertIn("would have been worth +6.0% on average", out)
        self.assertIn("Consider widening them", out)
        few = cp.report(pages[:3])
        self.assertIn("Too few to judge", few)

    def test_the_script_runs_offline_on_an_empty_record(self):
        sd = tempfile.mkdtemp()
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                cp.main(["--state-dir", sd, "--no-spy"])
            self.assertIn("REVIEW CHECKPOINT: 0 finished trades", buf.getvalue())
        finally:
            shutil.rmtree(sd)


if __name__ == "__main__":
    unittest.main()
