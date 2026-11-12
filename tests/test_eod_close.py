"""The 15:50 end-of-day close (demo, exits only) and blocked_tickers, which always win."""

import argparse
import calendar
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load
import test_t212_place
from test_t212_place import Base, FakeResp, CREDS, http_error, tsla_ticket

t212 = test_t212_place.t212
check = load("trader-risk", "check")
scan = load("trader-scanner", "scan")

TICKER = "TSLA_US_EQ"
EOD_ON = {"eod_close": {"enabled": True, "at": "15:50"}}
STOP = {"id": 7001, "ticker": TICKER, "type": "STOP", "quantity": -7, "stopPrice": 358.32, "status": "NEW"}


def held(qty=7, ticker=TICKER):
    return {"instrument": {"ticker": ticker}, "quantity": qty, "quantityAvailableForTrading": 0,
            "currentPrice": 366.0}


def record(ticker=TICKER, **extra):
    """A buy record from t212.py place, as in trader/state/broker.json."""
    return dict({"date": "2026-11-05", "index": 1, "fingerprint": "fp-" + ticker, "symbol": ticker.split("_")[0],
                 "side": "buy", "qty": "7", "kind": "entry", "broker": "trading212", "env": "demo",
                 "t212_ticker": ticker, "stop": 358.32, "entry": 364.18, "target": 370.04,
                 "status": "stop_placed", "at": "2026-11-05T10:30:00-05:00"}, **extra)


def ny(h, m, s=0):
    return float(calendar.timegm((2026, 11, 5, h + 5, m, s)))       # New York is UTC-5 in November


class Close(Base):
    def setUp(self):
        super().setUp()
        self.write("config.json", dict(self.cfg, **EOD_ON))
        self.write("state/broker.json", {"orders": [record(), record("AAPL_US_EQ")]})   # no label: intraday
        self.client = t212.Client("demo", CREDS["T212_API_KEY"], CREDS["T212_API_SECRET"], state_dir=self.dir,
                                  log=lambda m: None)

    def account(self, positions, orders_then=([STOP], []), sell=None, stop=None):
        r = self.router
        r.add("GET", "/equity/positions", FakeResp(positions))
        for o in orders_then:
            r.add("GET", "/equity/orders", FakeResp(list(o)))
        r.add("DELETE", "/equity/orders/7001", FakeResp({}))
        r.add("POST", "/equity/orders/market", sell or FakeResp({"id": 9300, "status": "NEW"}))
        if stop:
            r.add("POST", "/equity/orders/stop", stop)

    def close(self):
        with redirect_stdout(io.StringIO()):
            return t212.run_eod(self.client, self.dir)

    def test_it_cancels_the_stop_then_sells_everything_at_market(self):
        self.account([held(7)])
        res = self.close()
        order = [(c[0], c[1]) for c in self.router.calls if c[0] in ("DELETE", "POST")]
        self.assertEqual(order, [("DELETE", "/equity/orders/7001"), ("POST", "/equity/orders/market")])
        self.assertEqual(self.router.posts()[0][2], {"ticker": TICKER, "quantity": -7, "extendedHours": False})
        self.assertEqual(res["failed"], [])
        self.assertEqual(self.pushed, [])        # the sales go in the evening report, not a message of their own
        self.assertIn("closed: TSLA_US_EQ: SELL 7 at market, order id 9300, cancelled STOP 358.32",
                      t212.render_eod(res))           # the log keeps the detail
        self.assertTrue(t212.eod_done_today(self.dir))

    def test_every_order_it_sends_is_a_sell(self):
        self.account([held(7), held(2.5, "AAPL_US_EQ")], orders_then=([STOP], []))
        self.router.add("POST", "/equity/orders/market", FakeResp({"id": 9301}))
        self.close()
        self.assertTrue(all(p[2]["quantity"] < 0 for p in self.router.posts()))
        self.assertEqual(len(self.router.posts()), 2)

    def test_a_refused_sale_puts_the_stop_back_and_alarms(self):
        self.account([held(7)], sell=http_error(400, "market closed"), stop=FakeResp({"id": 7002}))
        res = self.close()
        self.assertEqual([p[1] for p in self.router.posts()], ["/equity/orders/market", "/equity/orders/stop"])
        self.assertEqual(self.router.posts()[1][2]["stopPrice"], 358.32)
        self.assertIn("REFUSED", res["failed"][0])
        self.assertIn("stop 358.32 put back", res["failed"][0])
        self.assertTrue(any("Couldn't sell everything at the close" in p for p in self.pushed))
        self.assertTrue(any("END-OF-DAY CLOSE FAILED" in n[1] for n in self.notes))     # the Mac too

    def test_an_unclear_sale_is_never_repeated_and_alarms(self):
        self.account([held(7)], sell=http_error(503))
        res = self.close()
        self.assertIn("UNCLEAR", res["failed"][0])
        self.assertEqual(len(self.router.posts()), 1)                # no stop put back on top of it
        self.assertIn(TICKER, self.read("state/t212-guard.json")["unclear"])

    def test_a_stop_that_will_not_cancel_means_no_sale(self):
        self.account([held(7)])
        self.router.routes[("DELETE", "/equity/orders/7001")] = [http_error(400, "cannot cancel")]
        res = self.close()
        self.assertEqual(self.router.posts(), [])
        self.assertIn("NOT CLOSED", res["failed"][0])
        self.assertTrue(any("Couldn't sell everything at the close" in p for p in self.pushed))

    def test_nothing_open_says_so(self):
        self.account([], orders_then=([],))
        res = self.close()
        self.assertTrue(res["nothing"])
        self.assertIn("nothing open", t212.render_eod(res))
        self.assertEqual(self.pushed, [])
        self.assertEqual(self.router.posts(), [])

    def test_demo_only(self):
        live = t212.Client("live", "k", "s", state_dir=self.dir, log=lambda m: None)
        live.live_ok = True
        with self.assertRaises(t212.T212Error):
            t212.eod_close(live, self.dir)
        self.assertEqual(self.router.calls, [])

    def test_settings(self):
        self.assertIsNone(t212.eod_settings({}))
        self.assertIsNone(t212.eod_settings({"eod_close": {"enabled": False}}))
        self.assertEqual(t212.eod_settings({"eod_close": {"enabled": True}})["at"], (15, 50))
        for bad in ("16:30", "noon"):
            with self.assertRaises(t212.T212Error):
                t212.eod_settings({"eod_close": {"enabled": True, "at": bad}})


class InTheGuard(Close):
    def watch(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = t212.main(["guard", "--watch", "--every", "5", "--state-dir", self.dir], environ=CREDS)
        return code, out.getvalue()

    def test_the_guard_wakes_at_1550_closes_once_and_keeps_going(self):
        self.clock[0] = ny(15, 47)
        r = self.router
        r.add("GET", "/equity/positions", FakeResp([held(7)]), FakeResp([held(7)]), FakeResp([]))
        r.add("GET", "/equity/orders", FakeResp([STOP]), FakeResp([STOP]), FakeResp([]))
        r.add("DELETE", "/equity/orders/7001", FakeResp({}))
        r.add("POST", "/equity/orders/market", FakeResp({"id": 9300}))
        code, text = self.watch()
        self.assertEqual(code, 0)
        self.assertEqual(len(self.router.posts()), 1)                 # one close, not one a pass
        self.assertIn("END-OF-DAY CLOSE", text)
        self.assertTrue(any(abs(s - 181) < 2 for s in self.sleeps))  # 15:47 -> woke at 15:50, not 15:52

    def test_off_by_default(self):
        self.write("config.json", self.cfg)
        self.clock[0] = ny(15, 51)
        self.router.add("GET", "/equity/positions", FakeResp([]))
        self.router.add("GET", "/equity/orders", FakeResp([]))
        code, text = self.watch()
        self.assertNotIn("END-OF-DAY", text)


class Blocked(Base):
    def test_blocked_beats_the_allow_list_in_place(self):
        self.write("config.json", dict(self.cfg, allowed_tickers=["TSLA"], blocked_tickers=["tsla"]))
        code, _, err = self.place()
        self.assertEqual(code, 2)
        self.assertIn("blocked_tickers", err)
        self.assertEqual(self.router.calls, [])

    def test_blocked_without_an_allow_list_still_blocks(self):
        self.write("config.json", dict(self.cfg, blocked_tickers=["TSLA"]))
        code, _, err = self.place()
        self.assertEqual(code, 2)
        self.assertIn("blocked_tickers", err)

    def test_allowed_tickers_leaves_the_blocked_out(self):
        self.assertEqual(t212.allowed_tickers({"allowed_tickers": ["AAPL", "TSLA"], "blocked_tickers": ["TSLA"]}),
                         {"AAPL"})
        with self.assertRaises(t212.T212Error):
            t212.blocked_tickers({"blocked_tickers": "TSLA"})

    def test_risk_blocks_a_blocked_entry_but_never_an_exit(self):
        cfg = dict(self.cfg, allowed_tickers=["TSLA"], blocked_tickers=["TSLA"])
        res = check.check([tsla_ticket()], cfg, now=check.parse_now("2026-10-19 10:15"))
        self.assertEqual(res["results"][0]["verdict"], "BLOCK")
        self.assertTrue(any("blocked" in r for r in res["results"][0]["reasons"]))
        pf = {"cash": 1000, "positions": [{"ticker": "TSLA", "shares": 7}]}
        sell = {"symbol": "TSLA", "side": "sell", "qty": "7", "type": "market", "time_in_force": "day"}
        res = check.check([sell], cfg, portfolio=pf, prices={"TSLA": 360.0}, now=check.parse_now("2026-10-19 10:15"))
        self.assertEqual(res["results"][0]["verdict"], "PASS")

    def test_the_scanner_never_scans_a_blocked_ticker(self):
        with tempfile.TemporaryDirectory() as d:
            cfgp = os.path.join(d, "config.json")
            a = argparse.Namespace(symbols="TSLA,NVDA,AAPL", watchlist=None, universe=None, limit=None,
                                   config=cfgp, json=True, quiet=True)
            for cfg, want in (({"allowed_tickers": ["TSLA", "AAPL"], "blocked_tickers": ["AAPL"]}, ["TSLA"]),
                              ({"blocked_tickers": ["NVDA"]}, ["TSLA", "AAPL"])):
                with open(cfgp, "w") as fh:
                    json.dump(cfg, fh)
                self.assertEqual(scan.read_symbols(a), want)


if __name__ == "__main__":
    unittest.main()
