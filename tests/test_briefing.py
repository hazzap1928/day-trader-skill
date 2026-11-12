"""briefing.py: read-only, says "no data" instead of guessing, urgent things first.
A fake Trading 212, fake Yahoo and Nasdaq: no network, no real order."""

import calendar
import datetime
import io
import json
import os
import unittest
import zoneinfo
from contextlib import redirect_stdout, redirect_stderr

from _load import load
import test_t212_place
from test_t212_place import Base, FakeResp, CREDS, FX

t212 = test_t212_place.t212
br = load("trader-broker", "briefing")
br.t212 = t212                                  # the same module the fakes patch
br.tk = t212.ticket_module()
br.phone_mod = t212.phone_module()
br.net = t212.net_module()

LONDON = zoneinfo.ZoneInfo("Europe/London")
MORNING = float(calendar.timegm((2026, 11, 5, 7, 30, 0)))       # Thu 07:30 UK
DHR = {"instrument": {"ticker": "DHR_US_EQ"}, "quantity": 10.51, "averagePricePaid": 229.42, "currentPrice": 230.25,
       "walletImpact": {"unrealizedProfitLoss": 6.03}}
DHR_STOP = {"id": 1, "ticker": "DHR_US_EQ", "type": "STOP", "quantity": -10.51, "stopPrice": 225.9}


def chart(pct, price=7400.0):
    return json.dumps({"chart": {"result": [{"meta": {"regularMarketPrice": price,
                                                      "regularMarketChangePercent": pct}}]}}).encode()


RSS = b"""<rss><channel><item><title>Stocks rise as yields fall</title><link>https://x/1</link>
<pubDate>Thu, 05 Nov 2026 06:00:00 +0000</pubDate></item></channel></rss>"""


def nasdaq(rows):
    return json.dumps({"data": {"asOf": "x", "rows": rows}}).encode()


class Web(object):
    """Fake Yahoo and Nasdaq, by URL. Anything not set raises, like a failed request."""

    def __init__(self):
        self.pages, self.urls = {}, []

    def __call__(self, url, browser=False, timeout=20):
        self.urls.append(url)
        for key, body in self.pages.items():
            if key in url:
                if isinstance(body, Exception):
                    raise body
                return body
        raise OSError("no fake for %s" % url)


class BriefBase(Base):
    def setUp(self):
        super().setUp()
        self.clock[0] = MORNING
        self.web = Web()
        self._br = (br._now, br._fetch, br._local)
        br._now = lambda: self.clock[0]
        br._fetch = self.web
        br._local = lambda u: u.replace(tzinfo=datetime.timezone.utc).astimezone(LONDON)
        self.client = t212.Client("demo", CREDS["T212_API_KEY"], CREDS["T212_API_SECRET"], state_dir=self.dir,
                                  log=lambda m: None)

    def tearDown(self):
        br._now, br._fetch, br._local = self._br
        super().tearDown()

    def all_data(self, earnings_today=(), econ=()):
        w = self.web.pages
        w["ES%3DF"], w["NQ%3DF"], w["YM%3DF"] = chart(0.4), chart(0.6), chart(-0.1)
        w["%5EGSPC"] = RSS
        w["earnings?date=2026-11-05"] = nasdaq(list(earnings_today))
        w["earnings?date=2026-11-06"] = nasdaq([])
        w["economicevents?date=2026-11-05"] = nasdaq(list(econ))

    def trading212(self, positions=(DHR,), orders=(DHR_STOP,)):
        r = self.router
        r.add("GET", "/equity/account/summary", FakeResp(test_t212_place.SUMMARY))
        r.add("GET", "/equity/positions", FakeResp(list(positions)))
        r.add("GET", "/equity/orders", FakeResp(list(orders)))

    def build(self):
        return br.build(self.client, self.dir)


class ReadOnly(BriefBase):
    def test_it_never_places_or_cancels_even_with_no_stop(self):
        self.all_data()
        self.trading212(orders=())
        b = self.build()
        self.assertEqual([c[0] for c in self.router.calls if c[0] != "GET"], [])
        self.assertIn("NO stop", b["todo"][0][1])
        self.assertEqual(b["todo"][0][0], 1)

    def test_the_source_has_no_order_calls(self):
        with open(os.path.join(os.path.dirname(br.__file__), "briefing.py")) as fh:
            src = fh.read()
        for call in ("post_order(", "cancel_order(", "guard_send_stop(", "guard_send_sell(", "cmd_place(",
                     "eod_close(", "run_eod(", "method=\"POST\"", "method=\"DELETE\""):
            self.assertFalse(call in src, "briefing.py must not contain %s" % call)


class NoGuessing(BriefBase):
    def test_everything_that_fails_says_no_data(self):
        self.trading212()
        self.fx_calls.clear()
        t212._fx_open = lambda req, timeout: (_ for _ in ()).throw(OSError("down"))
        b = self.build()
        text = br.full_text(b)
        self.assertEqual(b["mood"], "no data")
        self.assertIn("S&P 500 (ES=F): no data", text)
        self.assertIn("GBP/USD: no data", text)
        self.assertIn("Headlines (Yahoo's S&P 500 feed", text)
        self.assertIn("  - no data", text)
        self.assertIn("Today (Thu 05 Nov): no data", text)
        self.assertIn("after fees: no data", text)

    def test_no_trading_212_keys_still_makes_a_briefing(self):
        self.all_data()
        b = br.build(br._NoAccount("T212_API_KEY not set"), self.dir)
        self.assertIn("Total value: no data", br.full_text(b))
        self.assertEqual(b["todo"][0][0], 1)
        self.assertIn("did not answer", b["todo"][0][1])


class Contents(BriefBase):
    def test_account_and_yesterdays_result(self):
        self.all_data()
        self.trading212()
        self.write("state/account-days.json", {"days": {"2026-11-04": {"start": 4990.0, "close": 5003.32}}})
        b = self.build()
        text = br.full_text(b)
        self.assertIn("Total value: £5,000.00", text)
        self.assertIn("Cash available: £4,100.25", text)
        self.assertIn("Result on Wed 04 Nov, after fees: +£13.32", text)

    def test_positions_with_their_stops(self):
        self.all_data()
        self.trading212()
        text = br.full_text(self.build())
        self.assertIn("DHR: 10.51 @ $229.42, now $230.25, P/L £6.03; stop: $225.90 (covers 10.51)", text)

    def test_guard_alerts_since_the_last_close_only(self):
        self.all_data()
        self.trading212()
        os.makedirs(os.path.join(self.dir, "logs"), exist_ok=True)
        with open(os.path.join(self.dir, "logs", "guard-alerts.jsonl"), "w") as fh:
            fh.write(json.dumps({"at": "2026-11-04T20:00:00Z", "text": "old, before Wed's close"}) + "\n")
            fh.write(json.dumps({"at": "2026-11-04T21:30:00Z", "text": "END-OF-DAY CLOSE FAILED: DHR"}) + "\n")
        b = self.build()
        self.assertEqual([a["text"] for a in b["positions"]["alerts"]], ["END-OF-DAY CLOSE FAILED: DHR"])
        self.assertIn("1 guard alert since the last close", b["todo"][0][1])

    def test_unclear_guard_orders_are_urgent(self):
        self.all_data()
        self.trading212()
        self.write("state/t212-guard.json", {"unclear": {"DHR_US_EQ": "stop SELL 10.51: 503"}})
        self.assertIn("UNCLEAR", self.build()["todo"][0][1])

    def test_futures_fx_news_and_us_releases_in_uk_time(self):
        self.all_data(econ=[{"gmt": "08:30", "country": "United States", "eventName": "CPI m/m",
                             "consensus": "0.3%", "previous": "0.4%"},
                            {"gmt": "10:00", "country": "Japan", "eventName": "Not ours"}])
        self.trading212()
        b = self.build()
        text = br.full_text(b)
        self.assertEqual(b["mood"], "up")
        self.assertIn("Nasdaq 100 (NQ=F): 7,400.00, +0.60%", text)
        self.assertIn("GBP/USD: 1.2500", text)
        self.assertIn("Stocks rise as yields fall", text)
        self.assertIn("13:30 CPI m/m (BIG), consensus 0.3%, previous 0.4%", text)   # 08:30 New York
        self.assertNotIn("Not ours", text)
        self.assertTrue(any("CPI m/m at 13:30 UK" in t for _, t in b["todo"]))

    def test_earnings_for_holdings_beat_earnings_for_flags(self):
        self.write("state/autopilot.json", {"tried": {"AAPL": "ticket sent"}})
        self.all_data(earnings_today=[{"symbol": "AAPL", "name": "Apple", "time": "time-after-hours"},
                                      {"symbol": "DHR", "name": "Danaher", "time": "time-pre-market"},
                                      {"symbol": "ZZZ", "name": "Not yours", "time": "time-pre-market"}])
        self.trading212()
        b = self.build()
        self.assertEqual(b["todo"][0][0], 1)
        self.assertIn("You hold DHR and it reports earnings today before the open", b["todo"][0][1])
        self.assertTrue(any("AAPL today" in t for _, t in b["todo"]))
        self.assertNotIn("ZZZ", br.full_text(b))

    def test_the_scanners_flags_are_capped_and_on_the_allow_list(self):
        hits = [{"symbol": "S%d" % i} for i in range(40)] + [{"symbol": "AAPL"}]
        self.write("watchlist.json", {"as_of": "2026-11-04 12:00 ET", "hits": hits})
        self.assertEqual(len(br.flagged(self.dir, {})), br.MAX_FLAGGED)
        self.assertEqual(br.flagged(self.dir, {"allowed_tickers": ["AAPL"]}), {"AAPL"})
        self.write("watchlist.json", {"as_of": "2026-10-01 12:00 ET", "hits": hits})
        self.assertEqual(br.flagged(self.dir, {}), set())               # too old to count


class Message(BriefBase):
    def run_main(self, *extra, clock=None):
        if clock:
            self.clock[0] = clock
        sent = []
        saved = br.send_with_retry
        br.send_with_retry = lambda sd, text, environ=None: sent.append(text) or True
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = br.main(["--state-dir", self.dir] + list(extra), environ=CREDS)
        finally:
            br.send_with_retry = saved
        return code, sent, out.getvalue()

    def test_short_message_has_three_points_and_the_full_one_is_saved(self):
        self.all_data()
        self.trading212(orders=())
        code, sent, _ = self.run_main()
        self.assertEqual(code, 0)
        lines = sent[0].splitlines()
        self.assertEqual(lines[0], "☀️ <b>Good morning: Thursday 5 Nov</b>")
        self.assertEqual(lines[1], "• Account value: <b>£5,000.00</b>")
        self.assertTrue(lines[2].startswith("• 📈 The US market looks set to open higher (+"), lines[2])
        self.assertTrue(any(l.startswith("Full briefing: ") for l in lines))
        self.assertEqual(lines[-1], "<i>Practice account</i>")
        path = os.path.join(self.dir, "briefings", "2026-11-05.md")
        with open(path) as fh:
            self.assertIn("## 5. To do, most urgent first", fh.read())

    def test_weekends_and_us_holidays_are_skipped(self):
        for when in (calendar.timegm((2026, 11, 7, 7, 30, 0)),        # Saturday
                     calendar.timegm((2026, 11, 26, 7, 30, 0))):      # Thanksgiving
            with self.subTest(when):
                code, sent, out = self.run_main(clock=float(when))
                self.assertEqual((code, sent), (0, []))
                self.assertIn("no briefing today", out)
                self.assertEqual(self.router.calls, [])

    def test_force_runs_on_a_weekend_about_the_next_session(self):
        self.clock[0] = float(calendar.timegm((2026, 11, 7, 12, 0, 0)))
        self.web.pages["earnings?date=2026-11-09"] = nasdaq([])
        self.trading212()
        b = self.build()
        self.assertEqual(b["session"].isoformat(), "2026-11-09")
        self.assertEqual(b["prev"].isoformat(), "2026-11-06")


class Feeds(BriefBase):
    def test_guard_alarms_are_logged_for_the_briefing_without_secrets(self):
        t212.guard_alarm(self.client, "key %s rejected" % CREDS["T212_API_KEY"])
        with open(os.path.join(self.dir, "logs", "guard-alerts.jsonl")) as fh:
            rec = json.loads(fh.readline())
        self.assertIn("rejected", rec["text"])
        self.assertNotIn(CREDS["T212_API_KEY"], rec["text"])


if __name__ == "__main__":
    unittest.main()
