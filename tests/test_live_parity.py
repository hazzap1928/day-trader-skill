"""Demo behaves exactly like live: stop_mode "market" (the guard sells at market when the
price crosses the stop), the config's live switch, and the allowed_tickers list."""

import argparse
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT
import test_t212_place
from test_t212_place import Base, FakeResp, CREDS, TODAY, http_error, tsla_ticket

t212 = test_t212_place.t212
check = load("trader-risk", "check")
scan = load("trader-scanner", "scan")

TICKER = "TSLA_US_EQ"
LIVE = dict(CREDS, T212_ENV="live")
RECORD = {"date": TODAY, "index": 1, "fingerprint": "fp1", "symbol": "TSLA", "side": "buy", "qty": "7",
          "kind": "entry", "broker": "trading212", "env": "demo", "t212_ticker": TICKER, "stop": 358.32,
          "entry": 364.18, "target": 370.04, "status": "placed", "at": "2026-11-05T10:30:00-05:00"}


def position(price, qty=7):
    return {"instrument": {"ticker": TICKER}, "quantity": qty, "quantityAvailableForTrading": qty,
            "currentPrice": price}


class MarketMode(Base):
    def setUp(self):
        super().setUp()
        self.write("state/broker.json", {"orders": [RECORD]})
        self.config(stop_mode="market")

    def config(self, **kw):
        self.write("config.json", dict(self.cfg, **kw))

    def set_account(self, price, pending=(), sell=None, qty=7):
        self.router.add("GET", "/equity/positions", FakeResp([position(price, qty)]))
        self.router.add("GET", "/equity/orders", FakeResp(list(pending)))
        for r in (sell if isinstance(sell, list) else [sell]):
            self.router.add("POST", "/equity/orders/market", r or FakeResp({"id": 9100, "status": "NEW"}))

    def guard(self, *extra, environ=CREDS):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = t212.main(["guard"] + list(extra) + ["--state-dir", self.dir], environ=environ)
        return code, out.getvalue(), err.getvalue()

    def test_above_the_stop_it_only_watches(self):
        self.set_account(362.0)
        code, text, err = self.guard()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.router.posts(), [])
        self.assertIn("stop guard (market stops)", text)
        self.assertIn("watching: stop 358.32", text)

    def test_at_or_under_the_stop_it_sells_at_market_never_a_stop_order(self):
        self.set_account(358.10)
        code, text, _ = self.guard()
        posts = self.router.posts()
        self.assertEqual([p[1] for p in posts], ["/equity/orders/market"])
        self.assertEqual(posts[0][2], {"ticker": TICKER, "quantity": -7, "extendedHours": False})
        self.assertIn("SOLD AT STOP", text)

    def test_a_pending_sell_at_trading_212_means_no_sale_on_top(self):
        self.set_account(350.0, pending=[{"id": 1, "ticker": TICKER, "type": "STOP", "quantity": -7,
                                          "stopPrice": 358.32}])
        code, text, _ = self.guard()
        self.assertEqual(self.router.posts(), [])
        self.assertIn("exit pending", text)

    def test_the_trail_raises_the_guards_own_stop_then_sells_under_it(self):
        self.config(stop_mode="market", trailing_stop={"enabled": True, "distance_r": 1.0})
        self.set_account(372.0)                  # stop -> 372 - 5.86 = 366.14, no order
        self.guard()
        self.assertEqual(self.router.posts(), [])
        self.assertEqual(self.read("state/t212-guard.json")["trail"][TICKER]["stop"], 366.14)
        self.router.routes.clear()
        self.set_account(366.0)                  # under the trailed stop: sell
        code, text, _ = self.guard()
        self.assertEqual(self.router.posts()[0][2]["quantity"], -7)
        self.assertIn("366.14 stop", text)

    def test_it_never_lowers_its_stop(self):
        self.config(stop_mode="market", trailing_stop={"enabled": True, "distance_r": 1.0})
        self.write("state/t212-guard.json", {"trail": {TICKER: {"fingerprint": "fp1", "high": 372.0,
                                                                "stop": 366.14}}})
        self.set_account(367.0)
        code, text, _ = self.guard()
        self.assertEqual(self.router.posts(), [])
        self.assertIn("stop 366.14", text)

    def test_an_unclear_sale_is_never_sent_again(self):
        self.set_account(358.0, sell=[http_error(503)])
        self.guard()
        self.assertEqual(len(self.router.posts()), 1)
        self.router.add("POST", "/equity/orders/market", FakeResp({"id": 9101}))
        code, text, _ = self.guard()
        self.assertEqual(len(self.router.posts()), 1)
        self.assertIn("unclear", text)

    def test_a_refused_sale_is_tried_again_next_check(self):
        self.set_account(358.0, sell=[http_error(400, "market closed"), FakeResp({"id": 9102})])
        code, text, _ = self.guard()
        self.assertIn("The next check tries again", text)
        self.guard()
        self.assertEqual(len(self.router.posts()), 2)

    def test_half_at_the_target_in_market_mode(self):
        self.config(stop_mode="market", trailing_stop={"enabled": True, "distance_r": 1.0,
                                                       "sell_half_at_target": True})
        self.set_account(371.0)
        code, text, _ = self.guard()
        self.assertEqual(self.router.posts()[0][2]["quantity"], -3)
        self.assertIn("SOLD HALF (3)", text)

    def test_watch_checks_every_minute_in_market_mode(self):
        self.assertIsNone(t212.build_parser().parse_args(["guard", "--watch"]).every)

    def test_place_in_market_mode_sends_the_entry_only_and_says_who_holds_the_stop(self):
        self.write("state/broker.json", {"orders": []})
        self.account()
        code, out, err = self.place()
        self.assertEqual(code, 0, err)
        self.assertEqual([p[1] for p in self.router.posts()], ["/equity/orders/market"])
        self.assertIn("enforced by t212.py guard (stop_mode market)", out)
        rec = [r for r in self.broker_log() if r.get("status") == "placed"][0]
        self.assertEqual(rec["stop"], 358.32)

    def test_a_bad_stop_mode_is_refused(self):
        self.config(stop_mode="sometimes")
        code, _, err = self.guard()
        self.assertEqual(code, 2)
        self.assertIn("stop_mode", err)


class LiveSwitch(Base):
    def test_the_guard_runs_on_live_only_when_the_config_switches_it_on(self):
        self.write("state/broker.json", {"orders": [dict(RECORD, env="live")]})
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = t212.main(["guard", "--live", "--state-dir", self.dir], environ=LIVE)
        self.assertEqual(code, 2)
        self.assertEqual(self.router.calls, [])
        self.write("config.json", dict(self.cfg, live_enabled=True, paper=False))
        self.router.add("GET", "/equity/positions", FakeResp([position(366.0)]))
        self.router.add("GET", "/equity/orders", FakeResp([]))
        self.router.add("POST", "/equity/orders/stop", FakeResp({"id": 1}))
        with redirect_stdout(out), redirect_stderr(err):
            code = t212.main(["guard", "--live", "--state-dir", self.dir], environ=LIVE)
        self.assertEqual(code, 0, err.getvalue())
        self.assertTrue(all(c[4].startswith("https://live.trading212.com/") for c in self.router.calls))

    def test_live_switch_needs_both_settings(self):
        self.assertFalse(t212.live_switch({}))
        self.assertFalse(t212.live_switch({"live_enabled": True}))
        self.assertFalse(t212.live_switch({"live_enabled": True, "paper": True}))
        self.assertFalse(t212.live_switch({"live_enabled": "yes", "paper": False}))
        self.assertTrue(t212.live_switch({"live_enabled": True, "paper": False}))

    def test_the_code_switch_is_still_off(self):
        self.assertFalse(t212.LIVE_PLACEMENT_ENABLED)

    def test_run_guard_passes_live_only_with_t212_env_live(self):
        with open(os.path.join(ROOT, "trader-broker", "run-guard.sh"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn('if [[ "${T212_ENV:-}" == "live" ]]; then', src)
        self.assertIn("unset T212_ENV", src)
        self.assertIn("T212_(API_KEY|API_SECRET|ENV)=", src)


class AllowList(Base):
    def test_place_refuses_a_buy_off_the_list(self):
        self.write("config.json", dict(self.cfg, allowed_tickers=["AAPL", "MSFT"]))
        code, _, err = self.place()
        self.assertEqual(code, 2)
        self.assertIn("not on your allowed_tickers", err)
        self.assertEqual(self.router.calls, [])

    def test_risk_blocks_an_entry_off_the_list_but_never_an_exit(self):
        cfg = dict(self.cfg, allowed_tickers=["AAPL"])
        res = check.check([tsla_ticket()], cfg, now=check.parse_now("2026-10-19 10:15"))
        self.assertTrue(any("allowed_tickers" in r for r in res["results"][0]["reasons"]))
        pf = {"cash": 1000, "positions": [{"ticker": "TSLA", "shares": 7}]}
        sell = {"symbol": "TSLA", "side": "sell", "qty": "7", "type": "market", "time_in_force": "day"}
        res = check.check([sell], cfg, portfolio=pf, prices={"TSLA": 360.0},
                          now=check.parse_now("2026-10-19 10:15"))
        self.assertEqual(res["results"][0]["verdict"], "PASS")

    def test_the_scanner_only_picks_from_the_list(self):
        with tempfile.TemporaryDirectory() as d:
            cfgp = os.path.join(d, "config.json")
            with open(cfgp, "w") as fh:
                json.dump({"allowed_tickers": ["aapl", "TSLA"]}, fh)
            a = argparse.Namespace(symbols="TSLA,NVDA,AAPL,AMD", watchlist=None, universe=None, limit=None,
                                   config=cfgp, json=True, quiet=True)
            self.assertEqual(scan.read_symbols(a), ["TSLA", "AAPL"])
            a.symbols = "NVDA,AMD"
            with self.assertRaises(scan.ScanError):
                scan.read_symbols(a)
            a.config = os.path.join(d, "none.json")
            self.assertEqual(scan.read_symbols(a), ["NVDA", "AMD"])     # no list: no limit


if __name__ == "__main__":
    unittest.main()
