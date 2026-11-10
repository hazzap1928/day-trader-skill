"""trader-scanner/universe.py: the filtered tradable universe. Every source is a fake: no Wikipedia,
no Yahoo, no Trading 212, no phone. It replaces allowed_tickers only on demo, with the switch on."""

import calendar
import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load
import test_t212_place
from test_t212_place import CREDS, NOW

t212 = test_t212_place.t212
uv = t212.universe_module()
scan = load("trader-scanner", "scan")
risk = load("trader-risk", "check")

DAY = 86400.0


def daily(price=100.0, volume=1_000_000, days=25, spread=0.0, end=NOW):
    """A Yahoo-like daily series: one bar a weekday-ish day before `end`, high/low around the close."""
    t = [end - (days - i) * DAY for i in range(days)]
    half = price * spread / 2.0
    # closes at the high and the low in turn: how a wide spread shows in daily bars
    c = [price + (half if i % 2 else -half) for i in range(days)] if spread else [price] * days
    return {"t": t, "c": c, "o": c, "h": [price + half] * days, "l": [price - half] * days,
            "v": [volume] * days, "meta": {}}


def inst(sym, ticker=None, typ="STOCK", ccy="USD"):
    return {"ticker": ticker or sym.replace(".", "_") + "_US_EQ", "shortName": sym, "type": typ,
            "currencyCode": ccy, "name": sym + " Inc"}


INSTRUMENTS = [inst("AAPL"), inst("MSFT"), inst("META", "FB_US_EQ"), inst("BRK.B"), inst("CHEAP"),
               inst("THIN"), inst("WIDE"), inst("NEWCO"), inst("SPY", typ="ETF"), inst("VOD", "VODl_EQ", ccy="GBX")]

CFG = {"paper": True, "allowed_tickers": ["AAPL", "TSLA"],
       "universe": {"enabled": True, "min_count": 1, "max_drop_pct": 50}}


def fake_daily(table):
    def f(sym):
        v = table[sym]
        if isinstance(v, Exception):
            raise v
        return v
    return f


GOOD = {"AAPL": daily(200, 2_000_000), "MSFT": daily(400, 1_000_000), "META": daily(600, 500_000),
        "BRK.B": daily(450, 400_000), "CHEAP": daily(3.5, 50_000_000), "THIN": daily(50, 100_000),
        "NEWCO": daily(30, 1_000_000, days=8)}
POOL = [("AAPL", "Apple"), ("MSFT", "Microsoft"), ("META", "Meta"), ("BRK.B", "Berkshire"), ("CHEAP", "Cheap"),
        ("THIN", "Thin"), ("NEWCO", "New"), ("NOPE", "Not on T212")]


class TmpDir(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self._now = uv._now
        uv._now = lambda: float(NOW)

    def tearDown(self):
        uv._now = self._now
        shutil.rmtree(self.dir)

    def write(self, rel, doc):
        p = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as fh:
            json.dump(doc, fh)

    def built(self, cfg=CFG, table=GOOD, pool=POOL, now=NOW):
        doc, change = uv.build(cfg, INSTRUMENTS, pool, fake_daily(table), now=now, previous=uv.read(self.dir))
        uv.save(self.dir, doc, change)
        return doc, change


class Rules(TmpDir):
    def test_each_filter_and_its_reason(self):
        doc, change = self.built()
        self.assertEqual(sorted(uv.symbols(doc)), ["AAPL", "BRK.B", "META", "MSFT"])
        d = doc["dropped"]
        self.assertIn("not on Trading 212", d["NOPE"])
        self.assertIn("last close $3.50 is under $5", d["CHEAP"])
        self.assertIn("average traded value $5.0m a day (20 sessions) is under $20.0m", d["THIN"])
        self.assertNotIn("max_spread_pct", doc["rules"])   # no spread filter: daily bars cannot measure one
        self.assertIn("only 8 of the last 20 sessions", d["NEWCO"])
        self.assertTrue(change["first"])
        meta = [t for t in doc["tickers"] if t["symbol"] == "META"][0]
        self.assertEqual(meta["t212"], "FB_US_EQ")                     # matched by the symbol Trading 212 shows
        self.assertEqual(doc["tickers"][0]["symbol"], "AAPL")          # most traded first
        self.assertEqual(doc["tickers"][0]["adv_usd"], 400_000_000)

    def test_blocked_always_wins(self):
        doc, _ = self.built(cfg=dict(CFG, blocked_tickers=["msft"]))
        self.assertNotIn("MSFT", uv.symbols(doc))
        self.assertEqual(doc["dropped"]["MSFT"], "on blocked_tickers")

    def test_the_threshold_is_configurable(self):
        cfg = dict(CFG, universe=dict(CFG["universe"], min_avg_traded_usd=4e6, min_price=3))
        doc, _ = self.built(cfg=cfg)
        self.assertIn("THIN", uv.symbols(doc))
        self.assertIn("CHEAP", uv.symbols(doc))

    def test_todays_bar_is_left_out_while_the_market_is_open(self):
        s = daily(100, 1_000_000, days=20, end=NOW - DAY)
        s["t"].append(NOW - 3600)       # 09:30 New York today, in progress
        s["c"].append(1.0)
        s["v"].append(10)
        for k in ("h", "l"):
            s[k].append(1.0)
        f = uv.figures(s, 20, datetime.datetime.utcfromtimestamp(NOW))
        self.assertEqual((f["price"], f["bars"]), (100.0, 20))

    def test_trading_212_index(self):
        idx = uv.t212_index(INSTRUMENTS)
        self.assertEqual(idx["META"]["ticker"], "FB_US_EQ")
        self.assertEqual(idx["BRK.B"]["ticker"], "BRK_B_US_EQ")
        self.assertNotIn("SPY", idx)                                    # ETFs are out
        self.assertNotIn("VOD", idx)                                    # not a US dollar listing
        self.assertEqual(uv.match_t212("BRKB", idx)[0], "BRK.B")         # iShares writes BRKB


class Safety(TmpDir):
    def test_yahoo_failing_keeps_the_old_list(self):
        self.built()
        table = dict(GOOD, AAPL=RuntimeError("HTTP 429"), MSFT=RuntimeError("HTTP 429"))
        with self.assertRaises(uv.UniverseError) as e:
            self.built(table=table)
        self.assertIn("Yahoo failed for 2 of 7", str(e.exception))
        self.assertEqual(len(uv.read(self.dir)["tickers"]), 4)          # untouched

    def test_too_few_or_a_big_drop_keeps_the_old_list(self):
        with self.assertRaises(uv.UniverseError):
            self.built(cfg=dict(CFG, universe=dict(CFG["universe"], min_count=10)))
        self.built()
        thin = dict(GOOD, AAPL=daily(200, 10), MSFT=daily(400, 10), META=daily(600, 10))
        with self.assertRaises(uv.UniverseError) as e:
            self.built(table=thin)
        self.assertIn("shrink from 4 to 1", str(e.exception))

    def test_a_short_starting_pool_is_not_trusted(self):
        with open(os.path.join(self.dir, "sp500.html"), "w") as fh:
            fh.write(sp500_html(["AAPL", "MSFT"]))
        with self.assertRaises(uv.UniverseError) as e:
            uv.load_pool(uv.settings(CFG), offline=self.dir)
        self.assertIn("only 2 stocks", str(e.exception))


def sp500_html(syms):
    rows = "".join('<tr><td><a href="#">%s</a></td><td>%s &amp; Co</td><td>X</td></tr>' % (s, s) for s in syms)
    return ('<html><table class="wikitable" id="constituents"><tr><th>Symbol</th><th>Security</th></tr>%s'
            '</table><table class="wikitable"><tr><td>ZZZ</td><td>changes</td></tr></table></html>' % rows)


class Sources(unittest.TestCase):
    def test_wikipedia_table(self):
        got = uv.parse_sp500(sp500_html(["MMM", "BRK.B", "BF.B"]))
        self.assertEqual(got, [("MMM", "MMM & Co"), ("BRK.B", "BRK.B & Co"), ("BF.B", "BF.B & Co")])
        with self.assertRaises(uv.UniverseError):
            uv.parse_sp500("<html>no table</html>")

    def test_ishares_holdings_csv(self):
        text = ('iShares Russell 1000 ETF\nFund Holdings as of,"Nov 06, 2026"\n\n'
                'Ticker,Name,Sector,Asset Class,Market Value\n'
                'AAPL,APPLE INC,Information Technology,Equity,"1,000"\n'
                'BRKB,BERKSHIRE HATHAWAY INC CLASS B,Financials,Equity,"900"\n'
                'USD,USD CASH,Cash and/or Derivatives,Cash,"10"\n')
        self.assertEqual([s for s, _ in uv.parse_ishares(text)], ["AAPL", "BRKB"])

    def test_settings(self):
        self.assertIsNone(uv.settings({}))
        s = uv.settings({"universe": {}})
        self.assertEqual((s["source"], s["min_avg_traded_usd"], s["min_price"], s["enabled"]),
                         ("sp500", 20e6, 5.0, False))
        for bad in ({"source": "nyse"}, {"source": "russell1000"}, {"enabled": "yes"}, {"refresh_at": "6pm"},
                    {"min_avg_traded_usd": "lots"}):
            with self.assertRaises(uv.UniverseError, msg=bad):
                uv.settings({"universe": bad})


class Changes(TmpDir):
    def test_added_and_removed_with_reasons(self):
        self.built()
        # next week: THIN got busier, MSFT left the index, META got thin
        table = dict(GOOD, THIN=daily(50, 1_000_000), META=daily(600, 1000))
        pool = [p for p in POOL if p[0] != "MSFT"] + [("NEWIN", "New in")]
        table["NEWIN"] = daily(80, 1_000_000)
        instruments = INSTRUMENTS + [inst("NEWIN")]
        doc, change = uv.build(CFG, instruments, pool, fake_daily(table), now=NOW + 7 * DAY,
                               previous=uv.read(self.dir))
        uv.save(self.dir, doc, change)
        added = {r["symbol"]: r["why"] for r in change["added"]}
        removed = {r["symbol"]: r["why"] for r in change["removed"]}
        self.assertTrue(added["NEWIN"].startswith("joined the S&P 500"))
        self.assertTrue(added["THIN"].startswith("was out (average traded value $5.0m"))
        self.assertEqual(removed["MSFT"], "left the S&P 500")
        self.assertIn("average traded value", removed["META"])
        with open(uv.paths(self.dir)["changes"]) as fh:
            self.assertEqual(len(fh.readlines()), 2)
        text = uv.change_text(change, doc)
        self.assertIn("UNIVERSE refreshed (demo): 4 stocks (was 4), 2 in, 2 out.", text)
        self.assertIn("Out: MSFT, left the S&P 500", text)


class Reader(TmpDir):
    def test_active_only_on_demo_with_the_switch_on_and_a_fresh_list(self):
        self.built()
        cfg, st = uv.apply(CFG, self.dir)
        self.assertTrue(st["active"])
        self.assertEqual(sorted(cfg["allowed_tickers"]), ["AAPL", "BRK.B", "META", "MSFT"])
        self.assertEqual(CFG["allowed_tickers"], ["AAPL", "TSLA"])     # the config itself is never changed
        for cfg, why in ((dict(CFG, paper=False), "live account"),
                         (dict(CFG, universe={"enabled": False}), "enabled is false"),
                         ({k: v for k, v in CFG.items() if k != "universe"}, "no universe")):
            out, st = uv.apply(cfg, self.dir)
            self.assertFalse(st["active"])
            self.assertIn(why, st["why"])
            self.assertEqual(out["allowed_tickers"], ["AAPL", "TSLA"])

    def test_missing_or_stale_falls_back_to_allowed_tickers(self):
        out, st = uv.apply(CFG, self.dir)
        self.assertIn("no valid universe file", st["why"])
        self.assertEqual(out["allowed_tickers"], ["AAPL", "TSLA"])
        self.built(now=NOW - 15 * DAY)
        out, st = uv.apply(CFG, self.dir)
        self.assertIn("15 days old", st["why"])
        self.assertEqual(out["allowed_tickers"], ["AAPL", "TSLA"])

    def test_t212_place_risk_and_scanner_all_see_it_and_blocked_still_wins(self):
        self.built()
        cfg = dict(CFG, blocked_tickers=["META"])
        self.write("config.json", cfg)
        self.assertEqual(t212.allowed_tickers(t212.with_universe(cfg, self.dir)), {"AAPL", "BRK.B", "MSFT"})
        self.assertEqual(t212.allowed_tickers(cfg), {"AAPL", "TSLA"})  # without a folder: your own list
        r = risk.with_universe(cfg, os.path.join(self.dir, "config.json"))
        self.assertIn("MSFT", r["allowed_tickers"])

        class A(object):
            symbols, watchlist, universe, limit, json, quiet = "AAPL,MSFT,META,TSLA", None, None, None, True, True
            config = os.path.join(self.dir, "config.json")
        self.assertEqual(scan.read_symbols(A()), ["AAPL", "MSFT"])      # TSLA not in it, META blocked

    def test_the_briefing_line(self):
        self.assertIn("2 tickers from allowed_tickers", uv.briefing_line({"allowed_tickers": ["A", "B"]}, self.dir))
        self.built()
        line = uv.briefing_line(CFG, self.dir)
        self.assertTrue(line.startswith("Universe: 4 stocks (S&P 500, filtered; built "), line)
        off = uv.briefing_line(dict(CFG, universe={"enabled": False}), self.dir)
        self.assertIn("2 tickers from allowed_tickers. A filtered universe of 4 is built but not in use", off)


class Schedule(TmpDir):
    def local(self, *ymdhm):
        return datetime.datetime(*ymdhm)

    def test_sunday_evening_then_not_again_that_week(self):
        cfg = {"universe": {"refresh_day": "sun", "refresh_at": "18:00"}}
        sat = self.local(2026, 11, 14, 12, 0)
        self.assertEqual(uv.last_refresh_moment(uv.settings(cfg), sat), self.local(2026, 11, 8, 18, 0))
        sun = self.local(2026, 11, 15, 18, 5)
        ts = calendar.timegm(sun.timetuple())
        ok, why = uv.due(cfg, self.dir, sun, ts)
        self.assertTrue(ok, why)
        uv.mark_started(self.dir, ts)
        self.assertFalse(uv.due(cfg, self.dir, sun, ts + 60)[0])                 # started a minute ago
        self.assertTrue(uv.due(cfg, self.dir, sun, ts + 7 * 3600)[0])            # failed: retried after 6 h
        self.built(now=ts + 600)
        self.assertEqual(uv.due(cfg, self.dir, sun, ts + 8 * 3600), (False, "built this week"))

    def test_not_in_market_hours_and_not_without_a_universe_section(self):
        mon = self.local(2026, 11, 16, 15, 0)
        ts = calendar.timegm(mon.timetuple())
        self.assertEqual(uv.due({"universe": {}}, self.dir, mon, ts, market_hours=True),
                         (False, "waiting for the market to close"))
        self.assertFalse(uv.due({}, self.dir, mon, ts)[0])


class CommandLine(TmpDir):
    def test_build_offline_writes_the_list_and_the_log(self):
        off = os.path.join(self.dir, "offline")
        os.makedirs(off)
        letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        syms = ["AAPL", "MSFT"] + ["Q" + a + b for a in letters for b in letters][:458]
        with open(os.path.join(off, "sp500.html"), "w") as fh:
            fh.write(sp500_html(syms))
        bundle = {}
        for s in syms:
            d = daily(100, 1_000_000)
            bundle[s] = {"chart": {"result": [{"meta": {"symbol": s}, "timestamp": d["t"], "indicators": {"quote": [
                {"close": d["c"], "open": d["o"], "high": d["h"], "low": d["l"], "volume": d["v"]}]}}]}}
        with open(os.path.join(off, "daily.json"), "w") as fh:
            json.dump(bundle, fh)
        instruments = os.path.join(self.dir, "instruments.json")
        with open(instruments, "w") as fh:
            json.dump([inst(s) for s in syms[:300]], fh)
        self.write("config.json", {"paper": True, "universe": {"enabled": True}})
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = uv.main(["--state-dir", self.dir, "build", "--offline", os.path.join(off, "daily.json"),
                            "--instruments", instruments], environ={})
        self.assertEqual(code, 1, err.getvalue())   # --offline for the pool wants the folder with sp500.html
        with redirect_stdout(out), redirect_stderr(err):
            code = uv.main(["--state-dir", self.dir, "build", "--offline", off, "--instruments", instruments],
                           environ={})
        self.assertEqual(code, 0, err.getvalue())
        self.assertIn("Universe: 300 of 460 S&P 500 stocks pass", out.getvalue())
        self.assertEqual(uv.read(self.dir)["count"], 300)
        with redirect_stdout(io.StringIO()) as o:
            uv.main(["--state-dir", self.dir, "changes"], environ={})
        self.assertIn("300 stocks (first build)", o.getvalue())


if __name__ == "__main__":
    unittest.main()


import test_autopilot
ap = test_autopilot.ap
ap.universe = uv                       # the same module instance the tests patch


class Pilot(test_autopilot.PilotBase):
    def setUp(self):
        super().setUp()
        self._uv_now = uv._now
        uv._now = lambda: self.clock[0]
        self.cfg = dict(self.cfg, universe={"enabled": True, "min_count": 1})
        self.write("config.json", self.cfg)
        doc, change = uv.build(self.cfg, INSTRUMENTS + [inst("TSLA")], POOL + [("TSLA", "Tesla")],
                               fake_daily(dict(GOOD, TSLA=daily(400, 1_000_000))), now=self.clock[0] - DAY)
        uv.save(self.dir, doc, change)

    def tearDown(self):
        uv._now = self._uv_now
        super().tearDown()

    def scanned(self):
        args = [c for c in self.pipe.calls if c[0].endswith("scan.py")][-1]
        return args[args.index("--symbols") + 1].split(",")

    def test_the_cycle_scans_the_universe_minus_what_was_tried_or_held(self):
        p = self.pilot()
        self.assertTrue(p.universe["active"])
        p.state["tried"]["MSFT"] = "Time Machine: no edge"
        self.router.add("GET", "/equity/positions", test_t212_place.FakeResp(
            [{"instrument": {"ticker": "AAPL_US_EQ"}, "quantity": 2}]))
        p.cycle()
        self.assertEqual(self.scanned(), ["BRK.B", "META", "TSLA"])
        cfg = self.read("config.json")
        self.assertEqual(cfg["allowed_tickers"], ["TSLA", "AAPL"])    # your own list stays in the file

    def test_the_weekly_refresh_runs_in_the_background_outside_market_hours(self):
        p = self.pilot()
        p.universe_refresh()                       # Thursday 10:30 New York: built yesterday, nothing to do
        self.assertEqual(self.spawned, [])
        self.clock[0] += 4 * DAY                   # Monday 10:30 New York: due (Sunday passed), but in session
        p.universe_refresh()
        self.assertEqual(self.spawned, [])
        self.clock[0] += 7 * 3600                  # Monday 17:30 New York: after the close
        p.universe_refresh()
        self.assertEqual(len(self.spawned), 1)
        self.assertTrue(self.spawned[0][1].endswith("trader-scanner/universe.py"))
        self.assertIn("--notify", self.spawned[0])
        p.universe_refresh()                       # started: not again
        self.assertEqual(len(self.spawned), 1)

    def test_status_and_open_message_say_how_many(self):
        p = self.pilot()
        self.router.add("GET", "/equity/positions", test_t212_place.FakeResp([]))
        self.router.add("GET", "/equity/orders", test_t212_place.FakeResp([]))
        self.assertIn("Universe: 5 stocks (S&P 500, filtered; built ", p.open_message())
        self.assertIn("5 tickers allowed", p.status_text())

    def test_a_slow_cycle_is_reported_once_a_day(self):
        p = self.pilot()
        p.slow_cycle(5 * 60)
        self.assertEqual(self.phone.sent, [])
        p.slow_cycle(13 * 60)
        p.slow_cycle(14 * 60)
        slow = [t for t, _ in self.phone.sent if t.startswith("SLOW CYCLE")]
        self.assertEqual(len(slow), 1)
        self.assertIn("13m 00s, over 80% of the 15-minute gap. Universe: 5 stocks", slow[0])

    def test_kronos_never_runs_into_the_next_cycle(self):
        p = self.pilot()
        p.cycle_started = self.clock[0] - 14 * 60       # the scan took 14 of the 15 minutes
        p.cycle_signals = [{"id": "x", "entry": 1.0}]
        calls = []
        saved = ap.evidence.update_signal, ap.evidence.kronos_verdict
        ap.evidence.update_signal = lambda sd, i, **k: calls.append(k)
        ap.evidence.kronos_verdict = lambda rec, **kw: self.fail("Kronos ran with no time left")
        try:
            self.clock[0] += 1
            p.kronos_pass()
        finally:
            ap.evidence.update_signal, ap.evidence.kronos_verdict = saved
        self.assertEqual(calls, [{"kronos": {"verdict": "skipped", "why": "time budget"}}])

    def test_kronos_takes_traded_signals_first_and_never_starts_what_cannot_finish(self):
        p = self.pilot()
        p.cycle_started = self.clock[0] - 60
        p.cycle_signals = [{"id": "seen", "entry": 1.0, "decision": "not placed: x"},
                           {"id": "bought", "entry": 1.0, "decision": "traded"}]
        calls, asked = [], []
        saved = ap.evidence.update_signal, ap.evidence.kronos_verdict

        def verdict(rec, timeout=None):
            asked.append((rec["id"], timeout))
            self.clock[0] += 290                        # one verdict takes about 5 minutes
            return {"verdict": "agree"}
        ap.evidence.update_signal = lambda sd, i, **k: calls.append((i, k["kronos"]["verdict"]))
        ap.evidence.kronos_verdict = verdict
        try:
            p.kronos_pass()
        finally:
            ap.evidence.update_signal, ap.evidence.kronos_verdict = saved
        self.assertEqual(asked, [("bought", ap.evidence.KRONOS_TIMEOUT)])
        self.assertEqual(calls, [("bought", "agree"), ("seen", "skipped")])


class CycleClock(Pilot):
    def test_a_long_cycle_leaves_the_rest_for_the_next(self):
        self.pipe.triggered = ("TSLA", "AAPL")
        p = self.pilot()
        self.router.add("GET", "/equity/positions", test_t212_place.FakeResp([]))
        p.cycle_started = self.clock[0] - 12 * 60          # 12 of 15 minutes gone
        line = p.cycle()
        self.assertIn("the rest wait for the next cycle", line)
        self.assertEqual(p.state["tried"], {})               # nothing marked: both come back next cycle


class Grouping(unittest.TestCase):
    def test_reasons_group_without_mangling_words(self):
        import re as _re
        with open(os.path.join(os.path.dirname(uv.__file__), "universe.py")) as fh:
            src = fh.read()
        pat = _re.search(r'key = re\.sub\(r"(.*?)", "N"', src).group(1)
        self.assertEqual(_re.sub(pat, "N", "not on Trading 212 (no US stock with this symbol)"),
                         "not on Trading 212 (no US stock with this symbol)")
        self.assertEqual(_re.sub(pat, "N", "average traded value $14.2m a day (20 sessions) is under $20.0m"),
                         "average traded value N a day (N sessions) is under N")
