import base64
import email.message
import io
import json
import os
import re
import shutil
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT

t212 = load("trader-broker", "t212")

KEY = "KEY-zq81-not-a-real-key"
SECRET = "SECRET-p0w7-not-a-real-secret"
TOKEN = base64.b64encode(("%s:%s" % (KEY, SECRET)).encode()).decode()
CREDS = {"T212_API_KEY": KEY, "T212_API_SECRET": SECRET}
FORBIDDEN = (KEY, SECRET, TOKEN, "%s:%s" % (KEY, SECRET))

SUMMARY = {"id": 31337, "currency": "GBP", "totalValue": 10250.5,
           "cash": {"availableToTrade": 4100.25, "reservedForOrders": 150.0, "inPies": 0},
           "investments": {"currentValue": 6000.25, "totalCost": 5800.0, "realizedProfitLoss": 12.5,
                           "unrealizedProfitLoss": 200.25}}
POSITIONS = [{"instrument": {"ticker": "TSLA_US_EQ", "name": "Tesla", "isin": "US88160R1014", "currency": "USD"},
              "quantity": 8, "averagePricePaid": 364.2, "currentPrice": 370.04,
              "walletImpact": {"currency": "GBP", "currentValue": 2280.1, "totalCost": 2244.0,
                               "unrealizedProfitLoss": 36.1, "fxImpact": None}}]
ORDERS = [{"id": 99001, "ticker": "TSLA_US_EQ", "side": "SELL", "type": "LIMIT", "quantity": 8,
           "filledQuantity": 0, "limitPrice": 380.0, "status": "NEW", "timeInForce": "DAY"}]
INSTRUMENTS = [
    {"ticker": "TSLA_US_EQ", "shortName": "TSLA", "name": "Tesla", "currencyCode": "USD", "type": "STOCK",
     "isin": "US88160R1014", "extendedHours": True},
    {"ticker": "TL0d_EQ", "shortName": "TSLA", "name": "Tesla (Xetra)", "currencyCode": "EUR", "type": "STOCK",
     "isin": "US88160R1014"},
    {"ticker": "VODl_EQ", "shortName": "VOD", "name": "Vodafone", "currencyCode": "GBX", "type": "STOCK"},
    {"ticker": "BRK_B_US_EQ", "shortName": "BRK.B", "name": "Berkshire B", "currencyCode": "USD", "type": "STOCK"},
    {"ticker": "DUP_US_EQ", "shortName": "DUP", "name": "Dup One", "currencyCode": "USD", "type": "STOCK"},
    {"ticker": "DUPX_US_EQ", "shortName": "DUP", "name": "Dup Two", "currencyCode": "USD", "type": "ETF"},
    {"ticker": "SAPd_EQ", "shortName": "SAP", "name": "SAP (Xetra)", "currencyCode": "EUR", "type": "STOCK"},
    {"ticker": "SAPl_EQ", "shortName": "SAP", "name": "SAP (London)", "currencyCode": "GBX", "type": "STOCK"},
]
OK_HEADERS = {"x-ratelimit-limit": "1", "x-ratelimit-period": "5", "x-ratelimit-remaining": "0",
              "x-ratelimit-reset": "1000005", "x-ratelimit-used": "1"}


class FakeResp(object):
    def __init__(self, body, headers=None, status=200):
        self.status = status
        self.headers = headers or {}
        self._body = json.dumps(body).encode()

    def read(self):
        return self._body

    def getcode(self):
        return self.status

    def close(self):
        pass


def http_error(url, code, body="", headers=None):
    msg = email.message.Message()
    for k, v in (headers or {}).items():
        msg[k] = v
    return urllib.error.HTTPError(url, code, "err", msg, io.BytesIO(body.encode()))


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.clock = [1000000.0]
        self.sleeps = []
        self.calls = []
        self.replies = []
        self._saved = (t212._open, t212._sleep, t212._now)

        def fake_open(req, timeout):
            self.calls.append(req)
            self.assertEqual(req.get_method(), "GET")
            if not self.replies:
                raise AssertionError("unexpected request to %s" % req.full_url)
            r = self.replies.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        def fake_sleep(s):
            self.sleeps.append(s)
            self.clock[0] += s

        t212._open = fake_open
        t212._sleep = fake_sleep
        t212._now = lambda: self.clock[0]

    def tearDown(self):
        t212._open, t212._sleep, t212._now = self._saved
        shutil.rmtree(self.dir)

    def run_cli(self, argv, environ=None):
        env = dict(CREDS) if environ is None else environ
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = t212.main(list(argv) + ["--state-dir", self.dir], environ=env)
        return code, out.getvalue(), err.getvalue()

    def assert_no_secrets(self, *texts):
        for text in texts:
            for s in FORBIDDEN:
                self.assertNotIn(s, text)
        for base, _, files in os.walk(self.dir):
            for f in files:
                with open(os.path.join(base, f), encoding="utf-8") as fh:
                    stored = fh.read()
                for s in FORBIDDEN:
                    self.assertNotIn(s, stored, f)


class Environment(Base):
    def test_demo_is_the_default(self):
        self.replies = [FakeResp(SUMMARY)]
        code, out, _ = self.run_cli(["status"])
        self.assertEqual(code, 0)
        self.assertEqual(self.calls[0].full_url, "https://demo.trading212.com/api/v0/equity/account/summary")
        self.assertIn("DEMO", out)

    def test_live_needs_both_env_and_flag(self):
        self.replies = [FakeResp(SUMMARY)]
        code, out, _ = self.run_cli(["status", "--live"], dict(CREDS, T212_ENV="live"))
        self.assertEqual(code, 0)
        self.assertEqual(self.calls[0].full_url, "https://live.trading212.com/api/v0/equity/account/summary")
        self.assertIn("LIVE", out)

    def test_options_work_before_or_after_the_command(self):
        live = dict(CREDS, T212_ENV="live")
        for argv in (["--live", "status"], ["status", "--live"], ["--live", "--json", "status"],
                     ["--json", "status", "--live"]):
            self.calls = []
            self.clock[0] += 100
            self.replies = [FakeResp(SUMMARY)]
            code, out, err = self.run_cli(argv, live)
            self.assertEqual(code, 0, (argv, err))
            self.assertTrue(self.calls[0].full_url.startswith("https://live."), argv)
            if "--json" in argv:
                self.assertEqual(json.loads(out)["env"], "live")

    def test_live_before_the_command_without_env_is_still_refused(self):
        code, _, err = self.run_cli(["--live", "status"])
        self.assertEqual(code, 2)
        self.assertEqual(self.calls, [])

    def test_env_live_without_flag_is_refused_before_any_request(self):
        code, _, err = self.run_cli(["status"], dict(CREDS, T212_ENV="live"))
        self.assertEqual(code, 2)
        self.assertEqual(self.calls, [])
        self.assertIn("--live was not passed", err)

    def test_flag_without_env_live_is_refused_before_any_request(self):
        for env in ({}, {"T212_ENV": "demo"}, {"T212_ENV": "LIVEx"}):
            code, _, err = self.run_cli(["cash", "--live"], dict(CREDS, **env))
            self.assertEqual(code, 2, env)
        self.assertEqual(self.calls, [])

    def test_unknown_env_is_refused(self):
        code, _, err = self.run_cli(["status"], dict(CREDS, T212_ENV="prod"))
        self.assertEqual(code, 2)
        self.assertEqual(self.calls, [])

    def test_missing_credentials_send_nothing(self):
        code, _, err = self.run_cli(["status"], {"T212_API_KEY": KEY})
        self.assertEqual(code, 2)
        self.assertIn("T212_API_SECRET", err)
        self.assertNotIn(KEY, err)
        self.assertEqual(self.calls, [])

    def test_basic_auth_get_only(self):
        self.replies = [FakeResp(SUMMARY)]
        self.run_cli(["info"])
        req = self.calls[0]
        self.assertEqual(req.get_method(), "GET")
        self.assertEqual(req.get_header("Authorization"), "Basic " + TOKEN)
        self.assertIsNone(req.data)

    def test_the_only_writes_are_post_order_and_a_demo_cancel(self):
        public = [n for n in dir(t212.Client) if not n.startswith("_")]
        self.assertEqual(public, ["cancel_order", "get", "post_order"])
        self.assertEqual(t212.PLACE_PATHS, ("/equity/orders/market", "/equity/orders/stop"))
        with open(os.path.join(ROOT, "trader-broker", "t212.py"), encoding="utf-8") as fh:
            src = fh.read()
        for verb in ('"PUT"', '"PATCH"', "/orders/limit", "/orders/stop_limit"):
            self.assertNotIn(verb, src)
        self.assertEqual(len(re.findall(r'Request\([^)]*method="POST"', src)), 1)
        self.assertEqual(len(re.findall(r'Request\(.*method="DELETE"', src)), 1)   # cancel_order, demo only
        self.assertEqual(src.count("_open(req, TIMEOUT)"), 3)   # get, post_order, cancel_order

    def test_cancel_is_refused_on_live_before_any_request(self):
        c = t212.Client("live", KEY, SECRET, state_dir=self.dir)
        for bad in (123, "123", None, True):
            with self.assertRaises(t212.T212Error):
                c.cancel_order(bad)
        demo = t212.Client("demo", KEY, SECRET, state_dir=self.dir)
        for bad in ("123", None, True, 0):
            with self.assertRaises(t212.T212Error):
                demo.cancel_order(bad)
        self.assertEqual(self.calls, [])

    def test_get_cannot_reach_the_order_endpoints(self):
        c = t212.Client("demo", KEY, SECRET, state_dir=self.dir)
        for path in t212.PLACE_PATHS:
            with self.assertRaises(t212.T212Error):
                c.get(path)
        self.assertEqual(self.calls, [])

    def test_live_placement_is_switched_off(self):
        self.assertIs(t212.LIVE_PLACEMENT_ENABLED, False)

    def test_redirects_are_never_followed(self):
        h = t212._NoRedirect()
        self.assertIsNone(h.redirect_request(None, None, 302, "Found", {}, "https://elsewhere.example/"))


class Secrets(Base):
    def test_credentials_never_in_normal_output(self):
        for cmd, body in (("status", SUMMARY), ("cash", SUMMARY), ("info", SUMMARY),
                          ("positions", POSITIONS), ("orders", ORDERS)):
            for extra in ([], ["--json"]):
                self.clock[0] += 100
                self.replies = [FakeResp(body, OK_HEADERS)]
                code, out, err = self.run_cli([cmd] + extra)
                self.assertEqual(code, 0, (cmd, err))
                self.assert_no_secrets(out, err)

    def test_credentials_scrubbed_from_an_error_body_that_echoes_them(self):
        body = "bad credentials %s / %s / Basic %s" % (KEY, SECRET, TOKEN)
        self.replies = [http_error("u", 401, body)]
        code, out, err = self.run_cli(["status"])
        self.assertEqual(code, 1)
        self.assertIn("401", err)
        self.assertIn("[redacted]", err)
        self.assert_no_secrets(out, err)

    def test_credentials_scrubbed_from_a_network_error(self):
        self.replies = [urllib.error.URLError("proxy said %s" % TOKEN)]
        code, out, err = self.run_cli(["cash"])
        self.assertEqual(code, 1)
        self.assert_no_secrets(out, err)

    def test_credentials_scrubbed_from_an_unexpected_error(self):
        self.replies = [ValueError("boom " + SECRET)]
        code, out, err = self.run_cli(["cash"])
        self.assertEqual(code, 1)
        self.assert_no_secrets(out, err)

    def test_credentials_never_in_state_or_cache_files(self):
        self.replies = [FakeResp(INSTRUMENTS, OK_HEADERS)]
        code, out, err = self.run_cli(["instruments", "--refresh"])
        self.assertEqual(code, 0, err)
        self.assertTrue(os.listdir(os.path.join(self.dir, "cache")))
        self.assert_no_secrets(out, err)


class Retries(Base):
    def test_429_backs_off_until_reset_then_succeeds(self):
        limited = {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(self.clock[0]) + 4)}
        self.replies = [http_error("u", 429, "", limited), FakeResp(SUMMARY)]
        code, out, err = self.run_cli(["cash"])
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.calls), 2)
        self.assertIn("429", err)
        self.assertTrue(any(3.5 <= s <= 6 for s in self.sleeps), self.sleeps)

    def test_429_gives_up_after_a_few_retries(self):
        self.replies = [http_error("u", 429) for _ in range(t212.MAX_RETRIES + 1)]
        code, _, err = self.run_cli(["positions"])
        self.assertEqual(code, 1)
        self.assertEqual(len(self.calls), t212.MAX_RETRIES + 1)
        self.assertIn("429", err)

    def test_other_errors_are_never_retried(self):
        for status in (400, 401, 403, 404, 408, 500, 502, 503):
            self.calls = []
            self.clock[0] += 100
            self.replies = [http_error("u", status), FakeResp(SUMMARY)]
            code, _, err = self.run_cli(["status"])
            self.assertEqual(code, 1, status)
            self.assertEqual(len(self.calls), 1, status)
            self.assertIn(str(status), err)

    def test_network_errors_are_never_retried(self):
        self.replies = [urllib.error.URLError("down"), FakeResp(SUMMARY)]
        code, _, _ = self.run_cli(["status"])
        self.assertEqual(code, 1)
        self.assertEqual(len(self.calls), 1)

    def test_403_names_the_missing_scope(self):
        self.replies = [http_error("u", 403)]
        _, _, err = self.run_cli(["orders"])
        self.assertIn("orders:read", err)


class RateLimit(Base):
    def test_documented_spacing_between_calls(self):
        self.replies = [FakeResp(SUMMARY), FakeResp(SUMMARY)]
        self.run_cli(["cash"])
        self.assertEqual(self.sleeps, [])
        self.run_cli(["info"])
        self.assertEqual(len(self.sleeps), 1)
        self.assertAlmostEqual(self.sleeps[0], 5.0)

    def test_spacing_is_per_endpoint(self):
        self.replies = [FakeResp(SUMMARY), FakeResp(POSITIONS)]
        self.run_cli(["cash"])
        self.run_cli(["positions"])
        self.assertEqual(self.sleeps, [])

    def test_remaining_zero_waits_for_reset(self):
        headers = {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(self.clock[0]) + 20)}
        self.replies = [FakeResp(POSITIONS, headers), FakeResp(POSITIONS)]
        self.run_cli(["positions"])
        self.run_cli(["positions"])
        self.assertEqual(len(self.sleeps), 1)
        self.assertAlmostEqual(self.sleeps[0], 20.0)

    def test_headers_are_reported_in_status(self):
        self.replies = [FakeResp(SUMMARY, OK_HEADERS)]
        code, out, _ = self.run_cli(["status", "--json"])
        data = json.loads(out)
        self.assertEqual(data["rate_limit"]["limit"], "1")
        self.assertEqual(data["rate_limit"]["period"], "5")
        self.assertEqual(data["account_id"], 31337)

    def test_every_endpoint_has_its_documented_limit(self):
        self.assertEqual(t212.MIN_INTERVAL, {"/equity/account/summary": 5.0, "/equity/positions": 1.0,
                                             "/equity/orders": 5.0, "/equity/metadata/instruments": 50.0,
                                             "/equity/orders/market": 60.0 / 50, "/equity/orders/stop": 2.0,
                                             "/equity/orders/{id}": 60.0 / 50})


class Resolve(Base):
    def r(self, sym):
        return t212.resolve(sym, INSTRUMENTS)["ticker"]

    def test_prefers_the_us_listing(self):
        self.assertEqual(self.r("TSLA"), "TSLA_US_EQ")
        self.assertEqual(self.r("tsla"), "TSLA_US_EQ")

    def test_class_shares(self):
        self.assertEqual(self.r("BRK.B"), "BRK_B_US_EQ")
        self.assertEqual(self.r("BRK-B"), "BRK_B_US_EQ")

    def test_a_t212_ticker_passes_through(self):
        self.assertEqual(self.r("TL0d_EQ"), "TL0d_EQ")

    def test_single_non_us_listing(self):
        self.assertEqual(self.r("VOD"), "VODl_EQ")

    def test_ambiguous_lists_candidates(self):
        for sym, want in (("DUP", ("DUP_US_EQ", "DUPX_US_EQ")), ("SAP", ("SAPd_EQ", "SAPl_EQ"))):
            with self.assertRaises(t212.T212Error) as cm:
                t212.resolve(sym, INSTRUMENTS)
            for t in want:
                self.assertIn(t, str(cm.exception))

    def test_unknown(self):
        with self.assertRaises(t212.T212Error) as cm:
            t212.resolve("ZZZZ", INSTRUMENTS)
        self.assertIn("--refresh", str(cm.exception))


class Cache(Base):
    def test_ticker_uses_the_cache_for_24_hours(self):
        self.replies = [FakeResp(INSTRUMENTS)]
        code, out, err = self.run_cli(["ticker", "TSLA"])
        self.assertEqual(code, 0, err)
        self.assertIn("TSLA -> TSLA_US_EQ", out)
        self.clock[0] += 23 * 3600
        code, out, _ = self.run_cli(["ticker", "TSLA", "--json"])
        self.assertEqual(json.loads(out)["ticker"], "TSLA_US_EQ")
        self.assertEqual(len(self.calls), 1)

    def test_stale_cache_is_refetched(self):
        self.replies = [FakeResp(INSTRUMENTS), FakeResp(INSTRUMENTS)]
        self.run_cli(["ticker", "TSLA"])
        self.clock[0] += 24 * 3600 + 1
        self.run_cli(["ticker", "TSLA"])
        self.assertEqual(len(self.calls), 2)

    def test_refresh_forces_a_download(self):
        self.replies = [FakeResp(INSTRUMENTS), FakeResp(INSTRUMENTS)]
        self.run_cli(["instruments"])
        code, out, _ = self.run_cli(["instruments", "--refresh", "--json"])
        self.assertEqual(len(self.calls), 2)
        data = json.loads(out)
        self.assertTrue(data["fetched_now"])
        self.assertEqual(data["count"], len(INSTRUMENTS))
        self.assertEqual(self.calls[1].full_url,
                         "https://demo.trading212.com/api/v0/equity/metadata/instruments")

    def test_cache_is_kept_per_environment(self):
        self.replies = [FakeResp(INSTRUMENTS)]
        self.run_cli(["instruments"])
        self.assertTrue(os.path.exists(os.path.join(self.dir, "cache", "t212-instruments-demo.json")))
        self.assertFalse(os.path.exists(os.path.join(self.dir, "cache", "t212-instruments-live.json")))


class Output(Base):
    def test_cash(self):
        self.replies = [FakeResp(SUMMARY)]
        _, out, _ = self.run_cli(["cash"])
        self.assertIn("4,100.25 GBP", out)
        self.assertIn("150.00 GBP", out)

    def test_positions_table_and_json(self):
        self.replies = [FakeResp(POSITIONS), FakeResp(POSITIONS)]
        _, out, _ = self.run_cli(["positions"])
        self.assertIn("TSLA_US_EQ", out)
        self.assertIn("364.20", out)
        self.clock[0] += 2
        _, out, _ = self.run_cli(["positions", "--json"])
        self.assertEqual(json.loads(out), POSITIONS)

    def test_orders_table(self):
        self.replies = [FakeResp(ORDERS)]
        _, out, _ = self.run_cli(["orders"])
        self.assertIn("99001", out)
        self.assertNotIn("99,001", out)
        self.assertIn("LIMIT", out)

    def test_account_id_is_shown_as_given(self):
        self.replies = [FakeResp(SUMMARY), FakeResp(SUMMARY)]
        _, out, _ = self.run_cli(["status"])
        self.assertIn("account 31337 (GBP)", out)
        self.clock[0] += 10
        _, out, _ = self.run_cli(["info"])
        self.assertIn("account id:      31337\n", out)

    def test_empty_lists(self):
        self.replies = [FakeResp([]), FakeResp([])]
        _, out, _ = self.run_cli(["positions"])
        self.assertIn("no open positions", out)
        _, out, _ = self.run_cli(["orders"])
        self.assertIn("no pending orders", out)


class Repo(unittest.TestCase):
    def test_gitignore_covers_trader_state(self):
        with open(os.path.join(ROOT, ".gitignore")) as fh:
            lines = [l.strip() for l in fh]
        self.assertTrue({"trader/", "/trader/"} & set(lines))


if __name__ == "__main__":
    unittest.main()
