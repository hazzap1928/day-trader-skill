"""Post-earnings drift, stage 2: the SHADOW scan (trader-broker/drift.py). Fake Nasdaq and Yahoo,
a Trading 212 that fails the test if it is touched: no network, no order."""

import calendar
import datetime
import io
import json
import os
import re
import shutil
import tempfile
import unittest
import urllib.parse
from contextlib import redirect_stdout

from _load import load, ROOT
import test_t212_place
from test_t212_place import FakeResp

drift = load("trader-broker", "drift")
t212 = test_t212_place.t212

D = datetime.date
ON = {"scan_enabled": True}
CFG = {"paper": True, "allowed_tickers": ["AAA", "BBB", "CCC", "NEG", "MISS", "SMALL", "TINY", "BLK", "BRK.B"],
       "blocked_tickers": ["BLK"], "drift": ON}


def row(sym, eps, fc, sur, time="time-after-hours"):
    return {"symbol": sym, "name": sym + " Inc", "eps": "$%.2f" % eps if eps >= 0 else "($%.2f)" % -eps,
            "epsForecast": "$%.2f" % fc if fc >= 0 else "($%.2f)" % -fc, "surprise": str(sur), "time": time}


ROWS = [row("AAA", 1.20, 1.00, 20), row("BBB", 0.55, 0.50, 10), row("CCC", 0.11, 0.10, 10, "time-pre-market"),
        row("NEG", -0.10, -0.20, 50), row("MISS", 0.90, 1.00, -10), row("SMALL", 1.099, 1.00, 9.9),
        row("TINY", 0.06, 0.05, 20), row("OUT", 2.0, 1.0, 100), row("BLK", 2.0, 1.0, 100),
        row("BRK/B", 5.0, 4.0, 25)]


def chart(bars):
    """Yahoo's daily chart JSON for {date: (o, h, l, c, v)}: each bar stamped 09:30 New York."""
    days = sorted(bars)
    ts = [calendar.timegm((d.year, d.month, d.day, 14, 30, 0)) for d in days]
    q = {"open": [bars[d][0] for d in days], "high": [bars[d][1] for d in days], "low": [bars[d][2] for d in days],
         "close": [bars[d][3] for d in days], "volume": [bars[d][4] for d in days]}
    return json.dumps({"chart": {"result": [{"timestamp": ts, "indicators": {"quote": [q]}}]}}).encode()


def trading_days(a, b):
    out, d = [], a
    while d <= b:
        if drift.is_trading_day(d):
            out.append(d)
        d += datetime.timedelta(days=1)
    return out


def flat(a, b, price=100.0, vol=1000.0):
    return {d: (price, price + 1, price - 1, price, vol) for d in trading_days(a, b)}


class Web(object):
    """Fake Nasdaq and Yahoo by URL. Anything not set raises, like a failed request."""

    def __init__(self):
        self.nasdaq, self.yahoo, self.urls = {}, {}, []

    def __call__(self, url, browser=False, timeout=30):
        self.urls.append(url)
        m = re.search(r"earnings\?date=(\d{4}-\d{2}-\d{2})", url)
        if m:
            body = self.nasdaq.get(m.group(1))
            if body is None or isinstance(body, Exception):
                raise body or OSError("no fake calendar")
            return json.dumps({"data": {"rows": body}}).encode()
        m = re.search(r"/chart/([^?]+)\?", url)
        if m:
            sym = urllib.parse.unquote(m.group(1))
            if sym not in self.yahoo:
                raise OSError("no fake bars for %s" % sym)
            return chart(self.yahoo[sym])
        raise OSError("no fake for %s" % url)


def boom(*a, **k):
    raise AssertionError("the drift code touched Trading 212")


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.web = Web()
        self._saved = (drift._get, drift._now)
        drift._get = self.web
        drift._now = lambda: calendar.timegm((2026, 11, 10, 20, 35, 0))   # Tue 15:35 New York
        self.t212_inside = drift.mod("trader-broker", "t212")
        self._open = self.t212_inside._open
        self.t212_inside._open = boom                                       # no Trading 212, ever
        self.write("config.json", CFG)

    def tearDown(self):
        drift._get, drift._now = self._saved
        self.t212_inside._open = self._open
        shutil.rmtree(self.dir)

    def write(self, rel, data):
        path = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            json.dump(data, fh)

    def prices(self, **last):
        for sym, px in last.items():
            self.web.yahoo[sym] = flat(D(2026, 10, 26), D(2026, 11, 10), px)

    def standard(self):
        self.web.nasdaq["2026-11-09"] = ROWS
        self.prices(AAA=100.0, BBB=10.0, CCC=50.0, NEG=10.0, **{"BRK-B": 1000.0})


class Rules(unittest.TestCase):
    S = drift.settings({"drift": ON})
    ALLOWED = {drift.key(x) for x in CFG["allowed_tickers"]} - {"BLK"}

    def ok(self, r, s=None):
        rr = {"symbol": r["symbol"], "eps": drift.num(r["eps"]), "forecast": drift.num(r["epsForecast"]),
              "surprise_pct": drift.num(r["surprise"])}
        return drift.check(rr, s or self.S, self.ALLOWED, {"BLK"})

    def test_a_beat_of_10_percent_on_a_10_cent_forecast_qualifies(self):
        for r in (ROWS[0], ROWS[1], ROWS[2], ROWS[9]):
            self.assertEqual(self.ok(r), (True, ""), r["symbol"])

    def test_what_does_not_qualify(self):
        self.assertIn("not a beat", self.ok(ROWS[4])[1])
        self.assertIn("under 10%", self.ok(ROWS[5])[1])
        self.assertIn("forecast", self.ok(ROWS[6])[1])
        self.assertIn("not in the universe", self.ok(ROWS[7])[1])
        self.assertIn("blocked", self.ok(ROWS[8])[1])
        self.assertFalse(self.ok(row("AAA", 1.0, 1.0, 10))[0])                  # equal is not a beat

    def test_a_negative_forecast_counts_as_tested_unless_switched_off(self):
        self.assertTrue(self.ok(ROWS[3])[0])                                     # -$0.20, either side of zero
        s = drift.settings({"drift": dict(ON, forecast_either_side=False)})
        self.assertIn("forecast", self.ok(ROWS[3], s)[1])
        s = drift.settings({"drift": dict(ON, min_surprise_pct=25)})
        self.assertFalse(self.ok(ROWS[0], s)[0])                                 # every threshold is a setting

    def test_nasdaq_numbers(self):
        self.assertEqual(drift.num("$1.23"), 1.23)
        self.assertEqual(drift.num("($0.05)"), -0.05)
        self.assertEqual(drift.num("12.5"), 12.5)
        self.assertIsNone(drift.num("N/A"))
        self.assertIsNone(drift.num(None))
        self.assertEqual(drift.key("BRK/B"), drift.key("BRK.B"))

    def test_ranking_b_is_the_surprise_relative_to_the_price(self):
        self.assertAlmostEqual(drift.score({"eps": 1.2, "forecast": 1.0}, 100.0), 0.002)
        self.assertAlmostEqual(drift.score({"eps": 0.55, "forecast": 0.5}, 10.0), 0.005)
        self.assertIsNone(drift.score({"eps": 1.2, "forecast": 1.0}, None))

    def test_settings(self):
        s = drift.settings({})
        self.assertFalse(s["scan_enabled"])                                      # no block: off
        self.assertEqual((s["min_surprise_pct"], s["min_forecast"], s["entry_after_days"], s["hold_days"], s["slots"]),
                         (10.0, 0.10, 2, 20, 10))
        self.assertEqual(s["result_days"], [5, 10, 20, 40])
        self.assertEqual((s["fx_fee"], s["slippage"], s["tracker"]), (0.0015, 0.0005, "SPY"))
        for bad in ({"scan_enabled": "yes"}, {"slots": 0}, {"hold_days": 2.5}, {"result_days": []},
                    {"min_surprise_pct": -1}, {"scan_at": "18:00"}, "on"):
            with self.assertRaises(drift.DriftError, msg=bad):
                drift.settings({"drift": bad})

    def test_enabled_needs_the_switch_and_demo(self):
        self.assertTrue(drift.enabled({"paper": True, "drift": ON}))
        self.assertFalse(drift.enabled({"paper": True, "drift": {"scan_enabled": False}}))
        self.assertFalse(drift.enabled({"paper": True}))
        self.assertFalse(drift.enabled({"paper": False, "drift": ON}))
        self.assertFalse(drift.enabled({"paper": True, "drift": "broken"}))


class TradingDays(unittest.TestCase):
    def test_weekends_and_holidays(self):
        self.assertEqual(drift.add_trading_days(D(2026, 11, 9), 2), D(2026, 11, 11))     # Mon -> Wed
        self.assertEqual(drift.add_trading_days(D(2026, 11, 12), 2), D(2026, 11, 16))    # Thu -> Mon
        self.assertEqual(drift.add_trading_days(D(2026, 11, 24), 2), D(2026, 11, 27))    # over Thanksgiving
        self.assertEqual(drift.add_trading_days(D(2026, 12, 23), 2), D(2026, 12, 28))    # over Christmas
        self.assertEqual(drift.add_trading_days(D(2026, 12, 30), 2), D(2027, 1, 4))      # over New Year
        self.assertEqual(drift.add_trading_days(D(2026, 11, 16), -1), D(2026, 11, 13))   # back over a weekend
        self.assertEqual(drift.add_trading_days(D(2026, 11, 27), -1), D(2026, 11, 25))   # back over Thanksgiving
        with self.assertRaises(drift.DriftError):
            drift.add_trading_days(D(2027, 12, 30), 3)

    def test_the_buy_day_is_day_1_of_20(self):
        s = drift.settings({"drift": ON})
        b = drift.buy_day(D(2026, 11, 9), s)
        self.assertEqual(b, D(2026, 11, 11))
        self.assertEqual(drift.exit_day(b, 20), D(2026, 12, 9))       # 20 trading days held, Thanksgiving skipped
        self.assertEqual(drift.exit_day(b, 40), D(2027, 1, 8))        # Christmas and New Year skipped
        self.assertEqual(drift.exit_day(b, 1), b)
        self.assertEqual(drift.exit_day(b, 20).isoformat(), t212.drift_sell_date(b, 19))   # agrees with stage 1


class Costs(unittest.TestCase):
    def test_after_costs(self):
        self.assertAlmostEqual(drift.net_return(100.0, 110.0, 0.002), 109.78 / 100.2 - 1)
        self.assertAlmostEqual(100 * drift.net_return(100.0, 100.0, 0.002), -0.3992, places=4)   # a flat round trip
        self.assertAlmostEqual(drift.net_return(500.0, 505.0, 0.0005), 504.7475 / 500.25 - 1)

    def test_vs_the_tracker_over_the_same_dates(self):
        s = drift.settings({"drift": ON})
        e, x = D(2026, 11, 11), D(2026, 11, 17)                       # day 1 and day 5
        bars = {e: (100.0, 101, 99, 100, 1), x: (109.0, 111, 108, 110.0, 1)}
        spy = {e: (500.0, 501, 499, 500, 1), x: (504.0, 506, 503, 505.0, 1)}
        r = drift.result_for({"entry_day": e.isoformat()}, 5, bars, spy, s)
        self.assertEqual(r["exit_day"], "2026-11-17")
        self.assertAlmostEqual(r["ret_net_pct"], 100 * (109.78 / 100.2 - 1), places=3)
        self.assertAlmostEqual(r["tracker_ret_pct"], 100 * (504.7475 / 500.25 - 1), places=3)
        self.assertAlmostEqual(r["vs_tracker_pct"], r["ret_net_pct"] - r["tracker_ret_pct"], places=3)
        self.assertIsNone(drift.result_for({"entry_day": e.isoformat()}, 10, bars, spy, s))   # no data: no guess


class Scan(Base):
    def test_the_shortlist_is_ranked_by_surprise_over_price(self):
        self.standard()
        doc = drift.scan(self.dir, CFG)
        self.assertEqual((doc["report_day"], doc["buy_day"], doc["exit_day"]), ("2026-11-09", "2026-11-11", "2026-12-09"))
        self.assertEqual([x["symbol"] for x in doc["shortlist"]], ["NEG", "BBB", "AAA", "BRK/B", "CCC"])
        self.assertEqual((doc["rows"], doc["qualified"]), (10, 5))
        self.assertTrue(doc["shadow"])
        self.assertIn("Nasdaq", doc["source"])

    def test_every_qualifying_signal_is_logged_with_source_and_timing(self):
        self.standard()
        drift.scan(self.dir, dict(CFG, drift=dict(ON, slots=2)))
        rows = drift.load_signals(self.dir)
        self.assertEqual(len(rows), 5)                                # shortlisted or not
        by = {r["symbol"]: r for r in rows}
        self.assertEqual(sum(1 for r in rows if r["shortlisted"]), 2)
        self.assertIn("no free slot", by["AAA"]["why"])
        self.assertEqual(by["CCC"]["when"], "before the open")
        self.assertEqual(by["AAA"]["when"], "after the close")
        self.assertIn("Nasdaq", by["AAA"]["source"])
        self.assertEqual(by["AAA"]["entry_day"], "2026-11-11")
        self.assertEqual(by["AAA"]["list_from"], "allowed_tickers")

    def test_once_a_day_and_never_twice_in_the_log(self):
        self.standard()
        drift.scan(self.dir, CFG)
        n = len(self.web.urls)
        drift.scan(self.dir, CFG)
        self.assertEqual(len(self.web.urls), n)                       # the day's scan is done: no new fetches
        drift.scan(self.dir, CFG, force=True)
        self.assertEqual(len(drift.load_signals(self.dir)), 5)        # forced again: no duplicates

    def test_a_full_virtual_book_shortlists_nothing(self):
        self.standard()
        held = [{"symbol": "H%d" % i, "report_day": "2026-11-02", "shortlisted": True, "entry_day": "2026-11-04",
                 "exit_day": "2026-12-02"} for i in range(10)]
        drift.save_signals(self.dir, held)
        doc = drift.scan(self.dir, CFG)
        self.assertEqual(doc["shortlist"], [])
        self.assertEqual(doc["in_book"], 10)

    def test_a_held_symbol_is_not_bought_twice(self):
        self.standard()
        drift.save_signals(self.dir, [{"symbol": "BBB", "report_day": "2026-11-02", "shortlisted": True,
                                       "entry_day": "2026-11-04", "exit_day": "2026-12-02"}])
        doc = drift.scan(self.dir, CFG)
        self.assertNotIn("BBB", [x["symbol"] for x in doc["shortlist"]])

    def test_the_calendar_failing_is_written_down_not_raised(self):
        self.web.nasdaq["2026-11-09"] = OSError("timed out")
        doc = drift.scan(self.dir, CFG)
        self.assertIn("did not answer", doc["error"])
        self.assertIn("no drift scan yet", drift.closing_line(self.dir, CFG, D(2026, 11, 10)))

    def test_off_means_nothing_runs(self):
        self.standard()
        for cfg in (dict(CFG, drift={"scan_enabled": False}), {k: v for k, v in CFG.items() if k != "drift"}):
            self.assertIsNone(drift.scan(self.dir, cfg))
            self.assertEqual(drift.resolve(self.dir, cfg), 0)
        self.assertEqual(self.web.urls, [])
        self.assertFalse(os.path.exists(os.path.join(self.dir, "state", "drift")))

    def test_live_is_refused(self):
        self.standard()
        with self.assertRaises(drift.DriftError):
            drift.scan(self.dir, dict(CFG, paper=False))
        self.assertEqual(self.web.urls, [])

    def test_no_scan_on_a_holiday(self):
        self.standard()
        self.assertIsNone(drift.scan(self.dir, CFG, today=D(2026, 11, 26)))


class Results(Base):
    """AAA reports Mon 9 Nov after the close; the virtual buy is the open on Wed 11 Nov."""

    def setUp(self):
        super().setUp()
        self.web.nasdaq["2026-11-09"] = [ROWS[0]]
        bars = flat(D(2026, 9, 1), D(2027, 1, 20))
        days = trading_days(D(2026, 11, 11), D(2027, 1, 20))
        bars[D(2026, 11, 6)] = (100.0, 101, 99, 100.0, 1000)
        bars[D(2026, 11, 9)] = (100.0, 105, 99, 104.0, 3000)          # report day: +4%, 3x the volume
        bars[D(2026, 11, 10)] = (104.0, 109, 103, 108.0, 1000)        # reaction day (after the close): +3.85%
        bars[days[0]] = (100.0, 101, 99, 100.0, 1000)                  # day 1: bought at the 100 open
        bars[days[2]] = (101.0, 106, 100, 101.0, 1000)                 # day 3: +6% high
        bars[days[4]] = (109.0, 110.5, 108, 110.0, 1000)               # day 5: sold at the 110 close
        bars[days[5]] = (100.0, 101, 50, 100.0, 1000)                  # day 6: a low far under any 3xATR stop
        bars[days[7]] = (100.0, 116, 99, 100.0, 1000)                  # day 8: +16% high, the best
        self.days = days
        self.web.yahoo["AAA"] = bars
        spy = flat(D(2026, 9, 1), D(2027, 1, 20), 500.0)
        spy[days[0]] = (500.0, 501, 499, 500.0, 1)
        spy[days[4]] = (504.0, 506, 503, 505.0, 1)
        self.web.yahoo["SPY"] = spy
        drift.scan(self.dir, CFG)

    def sig(self):
        return drift.load_signals(self.dir)[0]

    def test_a_close_still_moving_is_never_used(self):
        drift.resolve(self.dir, CFG, today=self.days[4])               # day 5 itself: its close is not final
        self.assertNotIn("5", self.sig()["results"])
        self.assertEqual(self.sig()["entry_open"], 100.0)
        drift.resolve(self.dir, CFG, today=self.days[5])
        r = self.sig()["results"]["5"]
        self.assertEqual(r["exit_day"], self.days[4].isoformat())
        self.assertAlmostEqual(r["ret_net_pct"], 100 * (109.78 / 100.2 - 1), places=3)
        self.assertAlmostEqual(r["vs_tracker_pct"], r["ret_net_pct"] - 100 * (504.7475 / 500.25 - 1), places=3)

    def test_5_10_20_and_40_days_then_done(self):
        drift.resolve(self.dir, CFG, today=D(2027, 1, 20))
        s = self.sig()
        self.assertEqual(sorted(s["results"], key=int), ["5", "10", "20", "40"])
        self.assertEqual(s["results"]["20"]["exit_day"], "2026-12-09")
        self.assertEqual(s["results"]["40"]["exit_day"], "2027-01-08")
        self.assertAlmostEqual(s["results"]["40"]["ret_net_pct"], 100 * drift.net_return(100.0, 100.0, 0.002), places=3)
        self.assertTrue(s["done"])
        n = len(self.web.urls)
        self.assertEqual(drift.resolve(self.dir, CFG, today=D(2027, 1, 21)), 0)   # nothing left to fetch
        self.assertEqual(len(self.web.urls), n)

    def test_the_untested_extras(self):
        drift.resolve(self.dir, CFG, today=D(2027, 1, 20))
        x = self.sig()["extras"]
        self.assertAlmostEqual(x["report_day_change_pct"], 4.0)
        self.assertEqual(x["reaction_day"], "2026-11-10")
        self.assertAlmostEqual(x["reaction_day_change_pct"], 100 * (108 / 104 - 1), places=2)
        self.assertAlmostEqual(x["report_day_volume_x_avg"], 3.0)
        self.assertEqual((x["max_gain_day"], x["max_gain_pct"]), (8, 16.0))
        self.assertEqual(x["first_day_at_gain_pct"], {"5": 3, "10": 5, "15": 8})
        self.assertEqual(x["atr_stop_hit_day"], 6)
        self.assertAlmostEqual(x["atr_stop"], 100.0 - 3 * x["atr"], places=3)
        self.assertIn("never used", x["note"])

    def test_atr(self):
        bars = {D(2026, 11, 2): (10, 11, 9, 10, 0), D(2026, 11, 3): (10, 12, 10, 11, 0), D(2026, 11, 4): (11, 11, 8, 9, 0)}
        self.assertAlmostEqual(drift.atr(bars, D(2026, 11, 5), 2), (2 + 3) / 2.0)
        self.assertIsNone(drift.atr(bars, D(2026, 11, 5), 5))


class Phone(Base):
    def test_closing_report_line(self):
        self.standard()
        drift.scan(self.dir, CFG)
        line = drift.closing_line(self.dir, CFG, D(2026, 11, 10))
        self.assertTrue(line.startswith("Drift shortlist for tomorrow (SHADOW: nothing is bought): NEG"))
        self.assertIn("BBB (+10% surprise, reported after the close)", line)
        self.assertIn("virtual sell at the close on Wed 09 Dec", line)

    def test_briefing_line(self):
        self.standard()
        drift.scan(self.dir, CFG)
        line = drift.briefing_line(self.dir, CFG, D(2026, 11, 11))
        self.assertTrue(line.startswith("Drift would buy today (SHADOW: nothing is bought): NEG"))

    def test_missing_old_or_broken_shortlist_says_no_scan_yet(self):
        want = "no drift scan yet"
        self.assertIn(want, drift.closing_line(self.dir, CFG, D(2026, 11, 10)))     # missing
        self.assertIn(want, drift.briefing_line(self.dir, CFG, D(2026, 11, 11)))
        self.write("state/drift/shortlist.json", {"buy_day": "2026-11-05", "shortlist": [{"symbol": "X"}]})
        self.assertIn(want, drift.closing_line(self.dir, CFG, D(2026, 11, 10)))     # out of date
        with open(drift.shortlist_path(self.dir), "w") as fh:
            fh.write("{not json")
        self.assertIn(want, drift.briefing_line(self.dir, CFG, D(2026, 11, 11)))    # broken
        self.assertIn(want, drift.closing_line(self.dir, dict(CFG, drift="broken"), D(2026, 11, 10)))
        self.assertIn(want, drift.closing_line(self.dir, CFG, D(2030, 1, 2)))       # past the calendar

    def test_nothing_qualified(self):
        self.web.nasdaq["2026-11-09"] = [ROWS[4]]
        drift.scan(self.dir, CFG)
        self.assertIn("none. 1 report on Mon 09 Nov, 0 qualified", drift.closing_line(self.dir, CFG, D(2026, 11, 10)))

    def test_off_shows_nothing(self):
        self.assertIsNone(drift.closing_line(self.dir, {"paper": True}, D(2026, 11, 10)))
        self.assertIsNone(drift.briefing_line(self.dir, {"paper": True, "drift": {"scan_enabled": False}},
                                              D(2026, 11, 11)))


class ReportAndBriefing(Base):
    """The closing report and the briefing: a drift line, and never an error from it."""

    def test_report_and_briefing_helpers(self):
        rp = load("trader-broker", "report")
        br = load("trader-broker", "briefing")
        for m, which, day in ((rp, "closing", D(2026, 11, 10)), (br, "briefing", D(2026, 11, 11))):
            self.assertIn("no drift scan yet", m.drift_line(self.dir, day, which))       # nothing scanned yet
            self.write("config.json", dict(CFG, drift="broken"))
            self.assertIn("no drift scan yet", m.drift_line(self.dir, day, which))
            self.write("config.json", {"paper": True})
            self.assertIsNone(m.drift_line(self.dir, day, which))                        # off: no line
            self.write("config.json", CFG)
        self.standard()
        drift.scan(self.dir, CFG)
        self.assertIn("NEG", rp.drift_line(self.dir, D(2026, 11, 10), "closing"))
        self.assertIn("NEG", br.drift_line(self.dir, D(2026, 11, 11), "briefing"))

    def test_the_line_reaches_the_messages(self):
        rp = load("trader-broker", "report")
        r = {"day": D(2026, 11, 10), "today": [], "week": [], "fills_ok": True, "open": [], "alerts": [],
             "unclear": {}, "errors": [], "total": None, "cash": None, "bought_today": [],
             "week_start": D(2026, 11, 9), "drift": "Drift shortlist for tomorrow (SHADOW: nothing is bought): NEG."}
        self.assertIn("Drift shortlist for tomorrow (SHADOW", rp.short_text(r, os.path.join(self.dir, "x.md")))
        self.assertIn("## Post-earnings drift (shadow)", rp.full_text(r))


class NeverAnOrder(Base):
    BANNED = ("post_order", "cancel_order", "cmd_place", "guard_send", "market_body", "stop_body", "eod_close",
              "run_eod", "Client(", "credentials", "/equity/", "MARKET", "T212_API", "LIVE_PLACEMENT")

    def test_the_drift_code_has_no_order_code(self):
        with open(os.path.join(ROOT, "trader-broker", "drift.py"), encoding="utf-8") as fh:
            src = fh.read()
        for word in self.BANNED:
            self.assertNotIn(word, src, word)

    def test_a_whole_scan_and_resolve_never_reach_trading_212(self):
        self.standard()
        self.web.yahoo["SPY"] = flat(D(2026, 9, 1), D(2026, 11, 10), 500.0)
        drift.scan(self.dir, CFG)                                     # boom() would fail the test
        drift.resolve(self.dir, CFG, today=D(2026, 11, 12))
        self.assertTrue(all("nasdaq.com" in u or "finance.yahoo.com" in u for u in self.web.urls))

    def test_it_writes_only_its_own_files(self):
        self.standard()
        before = set(os.listdir(self.dir))
        drift.scan(self.dir, CFG)
        self.assertEqual(set(os.listdir(self.dir)) - before, {"state"})
        self.assertEqual(sorted(os.listdir(os.path.join(self.dir, "state"))), ["drift"])
        self.assertEqual(sorted(os.listdir(os.path.join(self.dir, "state", "drift"))), ["shortlist.json", "signals.jsonl"])



import test_autopilot                                   # noqa: E402  (its fakes: phone, pipeline, Trading 212)


class FakeDrift(object):
    def __init__(self, clock, fail=False, on=True):
        self.clock, self.fail, self.on, self.scans, self.resolves = clock, fail, on, [], []

    def enabled(self, cfg):
        return self.on

    def scan(self, sd, cfg):
        self.scans.append(self.clock[0])
        if self.fail:
            raise RuntimeError("Nasdaq fell over")
        return {"rows": 3, "qualified": 1, "shortlist": [{"symbol": "AAA"}]}

    def resolve(self, sd, cfg):
        self.resolves.append(self.clock[0])
        return 0


class InTheAutopilot(test_autopilot.PilotBase):
    def setUp(self):
        super().setUp()
        self.ap = test_autopilot.ap
        self._mods = dict(self.ap._MODS)

    def tearDown(self):
        self.ap._MODS.clear()
        self.ap._MODS.update(self._mods)
        super().tearDown()

    def fake(self, **kw):
        f = FakeDrift(self.clock, **kw)
        self.ap._MODS["drift"] = f
        return f

    def test_once_a_day_and_a_failure_never_stops_it(self):
        f = self.fake(fail=True)
        p = self.pilot()
        p.drift_scan()
        p.drift_scan()
        self.assertEqual(len(f.scans), 1)
        self.assertTrue(any("drift scan failed (shadow, nothing affected)" in x for x in self.out))
        self.assertEqual(self.router.posts(), [])

    def test_off_means_no_scan(self):
        f = self.fake(on=False)
        self.pilot().drift_scan()
        self.assertEqual(f.scans, [])

    def test_the_day_scans_at_1535_before_the_closing_report_then_fills_results(self):
        f = self.fake()
        self.clock[0] = test_autopilot.NOW - 90 * 60                 # 9:00 New York
        clock = self.clock

        def updates(wait=0):
            clock[0] += wait
            return []
        self.phone.updates = updates
        self.pipe.triggered = ()
        self.router.add("GET", "/equity/positions", FakeResp([]))
        self.router.add("GET", "/equity/account/summary", FakeResp(test_t212_place.SUMMARY))
        self.router.add("GET", "/equity/history/orders", FakeResp({"items": [], "nextPagePath": None}))
        self.router.add("GET", "/equity/orders", FakeResp([]))
        p = self.pilot()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(p.day(), "done")
        at_1535 = test_autopilot.NOW + (5 * 60 + 5) * 60              # NOW is 10:30 New York
        self.assertEqual(len(f.scans), 1)
        self.assertGreaterEqual(f.scans[0], at_1535)
        self.assertLess(f.scans[0], at_1535 + 120)
        self.assertEqual(len(f.resolves), 1)
        self.assertGreater(f.resolves[0], f.scans[0])
        self.assertIn("CLOSING REPORT (demo)", self.phone.sent[-1][0])
        self.assertEqual(self.router.posts(), [])

    def test_a_late_start_scans_just_before_the_report(self):
        f = self.fake()
        self.clock[0] = test_autopilot.NOW + (5 * 60 + 30) * 60      # 16:00 New York: straight to the report
        self.router.add("GET", "/equity/positions", FakeResp([]))
        self.router.add("GET", "/equity/account/summary", FakeResp(test_t212_place.SUMMARY))
        self.router.add("GET", "/equity/history/orders", FakeResp({"items": [], "nextPagePath": None}))
        with redirect_stdout(io.StringIO()):
            self.pilot().day()
        self.assertEqual(len(f.scans), 1)
        self.assertEqual(len(f.resolves), 1)


if __name__ == "__main__":
    unittest.main()
