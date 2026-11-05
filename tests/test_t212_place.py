import base64
import calendar
import email.message
import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
import urllib.parse
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

t212 = load("trader-broker", "t212")
ticket = load("trader-broker", "ticket")
check = load("trader-risk", "check")

KEY = "KEY-place-81-not-real"
SECRET = "SECRET-place-p0w7-not-real"
TOKEN = base64.b64encode(("%s:%s" % (KEY, SECRET)).encode()).decode()
CREDS = {"T212_API_KEY": KEY, "T212_API_SECRET": SECRET}
FORBIDDEN = (KEY, SECRET, TOKEN)
NOW = calendar.timegm((2026, 11, 5, 15, 30, 0))     # 10:30 in New York
TODAY = "2026-11-05"
RATE = 1.25

SUMMARY = {"id": 52354912, "currency": "GBP", "totalValue": 5000.0,
           "cash": {"availableToTrade": 4100.25, "reservedForOrders": 0, "inPies": 0}}
INSTRUMENTS = [{"ticker": "TSLA_US_EQ", "shortName": "TSLA", "name": "Tesla", "currencyCode": "USD", "type": "STOCK"},
               {"ticker": "VODl_EQ", "shortName": "VOD", "name": "Vodafone", "currencyCode": "GBX", "type": "STOCK"}]
HELD7 = [{"instrument": {"ticker": "TSLA_US_EQ"}, "quantity": 7, "quantityAvailableForTrading": 7,
          "currentPrice": 366.0}]
FX = {"chart": {"result": [{"meta": {"regularMarketPrice": RATE, "regularMarketTime": NOW - 60}}]}}


def tsla_ticket(**over):
    with open(os.path.join(FIX, "trader-risk", "orders-tsla.json")) as fh:
        o = json.load(fh)["orders"][0]
    o.update(over)
    return o


def sell_ticket(qty="7"):
    return {"symbol": "TSLA", "side": "sell", "qty": qty, "type": "market", "time_in_force": "day",
            "why": "flat by the close"}


class FakeResp(object):
    def __init__(self, body, status=200, headers=None):
        self.status = status
        self.headers = headers or {}
        self._body = json.dumps(body).encode()

    def read(self):
        return self._body

    def getcode(self):
        return self.status

    def close(self):
        pass


def http_error(code, body=""):
    return urllib.error.HTTPError("u", code, "err", email.message.Message(), io.BytesIO(body.encode()))


class Router(object):
    """Fake Trading 212: replies per (method, path). A GET's last reply repeats; a POST's never does."""

    def __init__(self):
        self.routes = {}
        self.calls = []

    def add(self, method, path, *replies):
        self.routes.setdefault((method, path), []).extend(replies)

    def posts(self, path=None):
        return [c for c in self.calls if c[0] == "POST" and (path is None or c[1] == path)]

    def __call__(self, req, timeout):
        u = urllib.parse.urlparse(req.full_url)
        path = u.path.split("/api/v0", 1)[1]
        method = req.get_method()
        body = json.loads(req.data.decode()) if req.data else None
        self.calls.append((method, path, body, u.query, req.full_url))
        q = self.routes.get((method, path)) or []
        if not q:
            raise AssertionError("unexpected %s %s" % (method, path))
        r = q.pop(0) if (method == "POST" or len(q) > 1) else q[0]
        if isinstance(r, Exception):
            raise r
        return r


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.clock = [float(NOW)]
        self.sleeps = []
        self.router = Router()
        self.fx_calls = []
        self.reply = "yes"
        self.tty = True
        self._saved = (t212._open, t212._sleep, t212._now, t212._fx_open, t212._is_tty, t212._ask,
                       t212.LIVE_PLACEMENT_ENABLED)

        def fake_sleep(s):
            self.sleeps.append(s)
            self.clock[0] += s

        def fake_fx(req, timeout):
            self.fx_calls.append(req)
            self.assertIsNone(req.get_header("Authorization"))
            return FakeResp(FX)

        t212._open = self.router
        t212._sleep = fake_sleep
        t212._now = lambda: self.clock[0]
        t212._fx_open = fake_fx
        t212._is_tty = lambda: self.tty
        t212._ask = lambda prompt: self.reply
        with open(os.path.join(ROOT, "trader-budget", "config.example.json")) as fh:
            self.cfg = json.load(fh)
        self.write("config.json", self.cfg)
        self.write("cache/t212-instruments-demo.json",
                   {"env": "demo", "fetched_at": NOW, "instruments": INSTRUMENTS})
        self.write("cache/t212-instruments-live.json",
                   {"env": "live", "fetched_at": NOW, "instruments": INSTRUMENTS})
        self.set_tickets([tsla_ticket()])

    def tearDown(self):
        (t212._open, t212._sleep, t212._now, t212._fx_open, t212._is_tty, t212._ask,
         t212.LIVE_PLACEMENT_ENABLED) = self._saved
        shutil.rmtree(self.dir)

    def write(self, rel, data):
        path = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            json.dump(data, fh)

    def read(self, rel, default=None):
        path = os.path.join(self.dir, rel)
        if not os.path.exists(path):
            return default
        with open(path) as fh:
            return json.load(fh)

    def set_tickets(self, orders, verdict="PASS", date=TODAY, kinds=None):
        self.orders = orders
        self.write("orders.json", {"orders": orders})
        results = []
        for i, o in enumerate(orders):
            kind = (kinds or {}).get(i) or ("entry" if o.get("side") == "buy" else "exit")
            results.append({"fingerprint": ticket.fingerprint(o), "verdict": verdict, "kind": kind,
                            "reasons": [] if verdict == "PASS" else ["over your max loss"]})
        self.write("state/risk.json", {"date": date, "results": results})

    def account(self, pending=None, positions=None, filled=None, market=None, stop=None):
        """A clean demo account; the entry fills on the first positions check after it."""
        r = self.router
        r.add("GET", "/equity/account/summary", FakeResp(SUMMARY))
        r.add("GET", "/equity/orders", FakeResp(pending or []))
        r.add("GET", "/equity/positions", FakeResp(positions or []), FakeResp(filled if filled is not None else HELD7))
        r.add("POST", "/equity/orders/market",
              market or FakeResp({"id": 5001, "status": "NEW", "side": "BUY", "ticker": "TSLA_US_EQ", "quantity": 7}))
        r.add("POST", "/equity/orders/stop",
              stop or FakeResp({"id": 5002, "status": "NEW", "side": "SELL", "type": "STOP", "quantity": -7}))

    def place(self, *extra, **kw):
        environ = kw.get("environ", CREDS)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = t212.main(["place"] + list(extra or ["1"]) + ["--state-dir", self.dir], environ=environ)
        return code, out.getvalue(), err.getvalue()

    def broker_log(self):
        return (self.read("state/broker.json") or {}).get("orders", [])

    def order_log(self):
        path = os.path.join(self.dir, "logs", "t212-orders-%s.jsonl" % TODAY)
        if not os.path.exists(path):
            return []
        with open(path) as fh:
            return [json.loads(l) for l in fh if l.strip()]

    def assert_no_secrets(self, *texts):
        for t in texts:
            for s in FORBIDDEN:
                self.assertNotIn(s, t)
        for base, _, files in os.walk(self.dir):
            for f in files:
                with open(os.path.join(base, f), encoding="utf-8") as fh:
                    stored = fh.read()
                for s in FORBIDDEN:
                    self.assertNotIn(s, stored, f)


# ------------------------------------------------------------------ signs

class Signs(unittest.TestCase):
    def test_buy_is_positive_sell_is_negative(self):
        self.assertEqual(t212.signed_quantity("buy", "8"), 8)
        self.assertEqual(t212.signed_quantity("sell", "8"), -8)
        self.assertEqual(t212.signed_quantity("BUY", 2.5), 2.5)
        self.assertEqual(t212.signed_quantity("Sell", 2.5), -2.5)

    def test_a_negative_or_odd_quantity_never_flips_the_side(self):
        for side in ("buy", "sell"):
            for q in ("-8", -8, 0, "0", "abc", None, True, float("nan"), float("inf")):
                with self.assertRaises(t212.T212Error, msg=(side, q)):
                    t212.signed_quantity(side, q)

    def test_unknown_sides_are_refused(self):
        for side in ("short", "", None, "b", "s", "buy ", "sel"):
            if side == "buy ":
                self.assertEqual(t212.signed_quantity(side, 1), 1)   # whitespace is not a different side
                continue
            with self.assertRaises(t212.T212Error, msg=side):
                t212.signed_quantity(side, 1)

    def test_bodies(self):
        self.assertEqual(t212.market_body("TSLA_US_EQ", "buy", 7),
                         {"ticker": "TSLA_US_EQ", "quantity": 7, "extendedHours": False})
        self.assertEqual(t212.market_body("TSLA_US_EQ", "sell", 7)["quantity"], -7)
        self.assertEqual(t212.stop_body("TSLA_US_EQ", 7, 358.324),
                         {"ticker": "TSLA_US_EQ", "quantity": -7, "stopPrice": 358.32, "timeValidity": "DAY"})

    def test_the_stop_is_always_a_sell(self):
        for q in (1, 7, 0.5):
            self.assertLess(t212.stop_body("X_US_EQ", q, 10)["quantity"], 0)
        with self.assertRaises(t212.T212Error):
            t212.stop_body("X_US_EQ", -7, 10)

    def test_check_sign_catches_a_flipped_body(self):
        t212.check_sign({"quantity": 7}, "buy")
        t212.check_sign({"quantity": -7}, "sell")
        for body, side in (({"quantity": -7}, "buy"), ({"quantity": 7}, "sell"), ({"quantity": 0}, "buy"),
                           ({"quantity": "7"}, "buy"), ({"quantity": True}, "buy"), ({}, "buy"),
                           ({"quantity": 7}, "short")):
            with self.assertRaises(t212.T212Error, msg=(body, side)):
                t212.check_sign(body, side)


class PostOrderGuards(Base):
    def test_a_flipped_body_is_refused_before_sending(self):
        c = t212.Client("demo", KEY, SECRET, state_dir=self.dir)
        for body, side in (({"ticker": "TSLA_US_EQ", "quantity": -7}, "buy"),
                           ({"ticker": "TSLA_US_EQ", "quantity": 7}, "sell")):
            with self.assertRaises(t212.T212Error):
                c.post_order(t212.MARKET, body, side)
        self.assertEqual(self.router.calls, [])

    def test_live_post_is_refused_by_the_client_itself(self):
        c = t212.Client("live", KEY, SECRET, state_dir=self.dir)
        with self.assertRaises(t212.T212Error):
            c.post_order(t212.MARKET, {"ticker": "TSLA_US_EQ", "quantity": 1}, "buy")
        t212.LIVE_PLACEMENT_ENABLED = True
        with self.assertRaises(t212.T212Error):
            c.post_order(t212.STOP, {"ticker": "TSLA_US_EQ", "quantity": -1, "stopPrice": 1}, "sell")
        self.assertEqual(self.router.calls, [])

    def test_only_the_order_endpoints(self):
        c = t212.Client("demo", KEY, SECRET, state_dir=self.dir)
        with self.assertRaises(t212.T212Error):
            c.post_order("/equity/orders/limit", {"quantity": 1}, "buy")
        self.assertEqual(self.router.calls, [])


class SignsEndToEnd(Base):
    def test_a_buy_ticket_with_a_negative_qty_is_refused(self):
        self.set_tickets([tsla_ticket(qty="-8")])
        self.account()
        code, out, err = self.place()
        self.assertEqual(code, 2)
        self.assertEqual(self.router.posts(), [])

    def test_a_sell_ticket_sends_a_negative_quantity_and_no_stop(self):
        self.set_tickets([sell_ticket("7")])
        self.account(positions=HELD7,
                     market=FakeResp({"id": 6001, "status": "NEW", "side": "SELL", "quantity": -7}))
        code, out, err = self.place()
        self.assertEqual(code, 0, err)
        posts = self.router.posts()
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][1], "/equity/orders/market")
        self.assertEqual(posts[0][2]["quantity"], -7)
        self.assertIn("est. proceeds", out)

    def test_a_buy_ticket_sends_positive_then_a_negative_stop(self):
        self.account()
        code, out, err = self.place()
        self.assertEqual(code, 0, err)
        market, stop = self.router.posts()
        self.assertEqual(market[1], "/equity/orders/market")
        self.assertGreater(market[2]["quantity"], 0)
        self.assertEqual(stop[1], "/equity/orders/stop")
        self.assertEqual(stop[2]["quantity"], -market[2]["quantity"])

    def test_a_sell_that_would_go_short_is_refused(self):
        self.set_tickets([sell_ticket("7")], kinds={0: "entry"})
        self.account(positions=HELD7)
        code, _, err = self.place()
        self.assertEqual(code, 2)
        self.assertIn("short", err)
        self.assertEqual(self.router.posts(), [])

    def test_selling_more_than_you_hold_is_refused(self):
        self.set_tickets([sell_ticket("9")])
        self.account(positions=HELD7)
        code, _, err = self.place()
        self.assertEqual(code, 2)
        self.assertIn("not 9", err)
        self.assertEqual(self.router.posts(), [])

    def test_a_reported_side_mismatch_is_shouted(self):
        self.account(market=FakeResp({"id": 1, "status": "NEW", "side": "SELL"}))
        code, out, _ = self.place()
        self.assertIn("reports this order as SELL, not BUY", out)


# ------------------------------------------------------------------ sizing

class Sizing(unittest.TestCase):
    CFG = {"max_loss_per_trade_usd": 50, "max_order_usd": 4000}

    def test_the_fx_fee_can_cut_the_ticket(self):
        # $46.88 at the stop for 8 shares is under $50, but the FX fees on both legs push 8 over
        s = t212.size_entry(364.18, 358.32, 8, RATE, 4100.25, self.CFG)
        self.assertEqual(s["qty"], 7)
        self.assertEqual(s["binding"], "your max loss per trade")
        self.assertAlmostEqual(s["value_gbp"], 2039.41, places=2)
        self.assertAlmostEqual(s["fee_gbp"], 3.06, places=2)
        self.assertAlmostEqual(s["cost_gbp"], 2042.47, places=2)
        self.assertAlmostEqual(s["max_loss_gbp"], 38.89, places=2)
        self.assertLessEqual(s["max_loss_gbp"], s["limit_gbp"])
        self.assertAlmostEqual(s["limit_gbp"], 40.0)

    def test_the_max_loss_is_never_exceeded(self):
        for entry in (5.0, 21.37, 99.99, 364.18, 1234.5):
            for gap in (0.01, 0.3, 0.05 * entry, 0.2 * entry):
                for rate in (1.05, 1.2735, 1.6):
                    for limit in (5, 50, 333):
                        for frac in (False, True):
                            cfg = {"max_loss_per_trade_usd": limit, "fractional": frac}
                            try:
                                s = t212.size_entry(entry, entry - gap, 10 ** 6, rate, 10 ** 9, cfg)
                            except t212.T212Error:
                                continue
                            loss_ps = (gap / rate) + (entry / rate + (entry - gap) / rate) * t212.FX_FEE
                            self.assertLessEqual(s["qty"] * loss_ps, limit / rate + 1e-9,
                                                 (entry, gap, rate, limit, frac))
                            self.assertLessEqual(s["max_loss_gbp"], round(limit / rate, 2) + 0.005)

    def test_never_more_than_the_ticket(self):
        s = t212.size_entry(10.0, 9.9, 3, RATE, 10 ** 6, {"max_loss_per_trade_usd": 1000})
        self.assertEqual(s["qty"], 3)
        self.assertEqual(s["binding"], "the ticket")

    def test_a_gbp_limit_wins_over_the_usd_one(self):
        s = t212.size_entry(364.18, 358.32, 8, RATE, 4100.25,
                            {"max_loss_per_trade_usd": 1000, "max_loss_per_trade_gbp": 20})
        self.assertEqual(s["qty"], 3)
        self.assertIn("max_loss_per_trade_gbp", s["limit_src"])

    def test_cash_and_order_caps(self):
        s = t212.size_entry(364.18, 358.32, 8, RATE, 600, self.CFG)
        self.assertEqual(s["qty"], 2)
        self.assertEqual(s["binding"], "the cash available to trade")
        s = t212.size_entry(364.18, 358.32, 8, RATE, 10 ** 6, {"max_loss_per_trade_usd": 999, "max_order_gbp": 900})
        self.assertEqual(s["qty"], 3)

    def test_refusals(self):
        for args in ((364.18, None, 8, RATE, 4100, self.CFG),        # no stop
                     (364.18, 370.0, 8, RATE, 4100, self.CFG),       # stop above a buy
                     (364.18, 358.32, 8, RATE, 4100, {}),            # no loss limit at all
                     (364.18, 300.0, 8, RATE, 4100, self.CFG),       # not one share fits
                     (364.18, 358.32, 8, RATE, 0, self.CFG)):        # no cash
            with self.assertRaises(t212.T212Error, msg=args):
                t212.size_entry(*args)


class Prices(Base):
    def test_the_higher_of_plan_and_fresh_quote(self):
        o = tsla_ticket()
        self.write("prices.json", {"prices": {"TSLA": 366.5},
                                   "detail": {"TSLA": {"time": "2026-11-05T15:25:00Z"}}})
        self.assertEqual(t212.ticket_price(o, os.path.join(self.dir, "prices.json")), (366.5, "a fresh quote"))
        self.write("prices.json", {"prices": {"TSLA": 366.5},
                                   "detail": {"TSLA": {"time": "2026-11-05T15:00:00Z"}}})   # 30 min old
        self.assertEqual(t212.ticket_price(o, os.path.join(self.dir, "prices.json"))[1], "the plan's entry")
        self.write("prices.json", {"prices": {"TSLA": 360.0},
                                   "detail": {"TSLA": {"time": "2026-11-05T15:29:00Z"}}})   # lower: plan wins
        self.assertEqual(t212.ticket_price(o, os.path.join(self.dir, "prices.json"))[0], 364.18)

    def test_fx(self):
        rate, when, src = t212.fetch_gbpusd()
        self.assertEqual(rate, RATE)
        self.assertIn("Yahoo", src)
        t212._fx_open = lambda req, timeout: FakeResp(
            {"chart": {"result": [{"meta": {"regularMarketPrice": 1.25, "regularMarketTime": NOW - 5 * 86400}}]}})
        with self.assertRaises(t212.T212Error):
            t212.fetch_gbpusd()
        t212._fx_open = lambda req, timeout: FakeResp(
            {"chart": {"result": [{"meta": {"regularMarketPrice": 125.0, "regularMarketTime": NOW}}]}})
        with self.assertRaises(t212.T212Error):
            t212.fetch_gbpusd()

        def down(req, timeout):
            raise urllib.error.URLError("offline")
        t212._fx_open = down
        with self.assertRaises(t212.T212Error):
            t212.fetch_gbpusd()

    def test_fx_rate_override_skips_yahoo(self):
        self.account()
        code, out, err = self.place("1", "--fx-rate", "1.30")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.fx_calls, [])
        self.assertIn("1.3000", out)


# ------------------------------------------------------------------ the gates

class Gates(Base):
    def test_a_good_run(self):
        self.account()
        code, out, err = self.place()
        self.assertEqual(code, 0, err)
        for want in ("PLACE TICKET 1 of 1", "DEMO", "BUY 7 TSLA_US_EQ", "GBP 2,042.47", "FX fee", "358.32",
                     "target:      370.04", "cut from the ticket's 8", "max loss:    GBP 38.89",
                     "PLACED", "STOP PLACED"):
            self.assertIn(want, out)
        self.assertEqual(len(self.router.posts()), 2)

    def test_not_passed_by_risk(self):
        self.set_tickets([tsla_ticket()], verdict="BLOCK")
        code, _, err = self.place()
        self.assertEqual(code, 2)
        self.assertIn("BLOCKED", err)
        self.assertEqual(self.router.calls, [])

    def test_a_risk_check_from_another_day(self):
        self.set_tickets([tsla_ticket()], date="2026-11-04")
        code, _, err = self.place()
        self.assertEqual(code, 2)
        self.assertEqual(self.router.calls, [])

    def test_a_changed_ticket_is_not_the_one_that_passed(self):
        self.set_tickets([tsla_ticket()])
        self.write("orders.json", {"orders": [tsla_ticket(qty="9")]})
        code, _, err = self.place()
        self.assertEqual(code, 2)
        self.assertIn("not in the last risk check", err)
        self.assertEqual(self.router.calls, [])

    def test_ticket_by_fingerprint(self):
        self.account()
        fp = ticket.fingerprint(tsla_ticket())
        code, _, err = self.place(fp[:8])
        self.assertEqual(code, 0, err)
        code, _, err = self.place("7")
        self.assertEqual(code, 2)

    def test_anything_but_a_yes_sends_nothing(self):
        self.set_tickets([tsla_ticket(), dict(sell_ticket(), symbol="AAPL")])   # so "yes AAPL" names another ticket
        for reply in ("", "sure", "ok", "yeah", "yes to all", "yes AAPL", "yes 9", "yes but wait", "no"):
            self.router = Router()
            t212._open = self.router
            self.account()
            self.reply = reply
            code, out, _ = self.place()
            self.assertEqual(code, 1, reply)
            self.assertEqual(self.router.posts(), [], reply)
            self.assertIn("Not placed", out)
        self.assertEqual([r for r in self.broker_log() if r.get("status") != "declined"], [])

    def test_a_yes_naming_this_order_counts(self):
        self.set_tickets([tsla_ticket(), dict(sell_ticket(), symbol="AAPL")])
        self.account()
        self.reply = "yes 7 TSLA"
        code, _, err = self.place()
        self.assertEqual(code, 0, err)

    def test_no_terminal_no_order(self):
        self.account()
        self.tty = False
        code, out, err = self.place()
        self.assertEqual(code, 2)
        self.assertIn("own terminal", err)
        self.assertEqual(self.router.posts(), [])

    def test_there_is_no_reply_flag(self):
        with self.assertRaises(SystemExit):
            with redirect_stderr(io.StringIO()):
                t212.build_parser().parse_args(["place", "1", "--reply", "yes"])

    def test_already_placed_is_refused(self):
        self.account()
        self.assertEqual(self.place()[0], 0)
        self.account()
        code, _, err = self.place()
        self.assertEqual(code, 2)
        self.assertIn("done for today", err)
        self.assertEqual(len(self.router.posts()), 2)   # only the first run's entry and stop

    def test_limit_and_dollar_tickets_are_refused(self):
        for o in (tsla_ticket(type="limit", limit_price="364"),):
            self.set_tickets([o])
            code, _, err = self.place()
            self.assertEqual(code, 2)
        self.assertEqual(self.router.posts(), [])

    def test_non_usd_instruments_are_refused(self):
        self.set_tickets([dict(tsla_ticket(), symbol="VOD")])
        code, _, err = self.place()
        self.assertEqual(code, 2)
        self.assertIn("GBX", err)


class Live(Base):
    LIVE = dict(CREDS, T212_ENV="live")

    def test_live_is_refused_before_any_request(self):
        code, _, err = self.place("1", "--live", environ=self.LIVE)
        self.assertEqual(code, 2)
        self.assertIn("demo only", err)
        self.assertIn("set the stop yourself", err)
        self.assertEqual(self.router.calls, [])
        self.assertEqual(self.fx_calls, [])

    def test_a_mismatch_is_still_refused(self):
        for argv, env in ((["1"], self.LIVE), (["1", "--live"], CREDS)):
            code, _, _ = self.place(*argv, environ=env)
            self.assertEqual(code, 2)
        self.assertEqual(self.router.calls, [])

    def test_when_switched_on_live_places_the_entry_only_and_warns(self):
        t212.LIVE_PLACEMENT_ENABLED = True
        self.account()
        code, out, err = self.place("1", "--live", environ=self.LIVE)
        self.assertEqual(code, 0, err)
        posts = self.router.posts()
        self.assertEqual([p[1] for p in posts], ["/equity/orders/market"])
        self.assertTrue(posts[0][4].startswith("https://live.trading212.com/"))
        self.assertIn("NO STOP WILL BE PLACED", out)
        self.assertIn("NO STOP WAS PLACED", out)
        self.assertIn("SELL 7 TSLA_US_EQ at 358.32", out)


# ------------------------------------------------------------------ never retried

class NoRetry(Base):
    def test_a_429_is_not_retried(self):
        self.account(market=http_error(429))
        code, _, err = self.place()
        self.assertEqual(code, 1)
        self.assertEqual(len(self.router.posts()), 1)
        self.assertIn("NOT PLACED", err)
        self.assertEqual(self.broker_log()[-1]["status"], "failed")

    def test_a_500_or_no_answer_is_unclear_and_not_retried(self):
        for failure in (http_error(500), http_error(408), urllib.error.URLError("timed out"), OSError("reset")):
            self.tearDown()
            self.setUp()
            self.account(market=failure)
            code, _, err = self.place()
            self.assertEqual(code, 1, failure)
            self.assertEqual(len(self.router.posts()), 1, failure)
            self.assertIn("UNCLEAR", err)
            self.assertEqual(self.broker_log()[-1]["status"], "unknown")

    def test_a_resend_needs_the_flag(self):
        self.account(market=http_error(500))
        self.place()
        self.account()
        code, _, err = self.place()
        self.assertEqual(code, 2)
        self.assertIn("--resend", err)
        self.assertEqual(len(self.router.posts()), 1)

    def test_a_resend_checks_for_a_duplicate_first(self):
        self.account(market=http_error(500))
        self.place()
        # the first one did reach Trading 212: it is pending now
        self.router = Router()
        t212._open = self.router
        self.account(pending=[{"id": 5001, "ticker": "TSLA_US_EQ", "side": "BUY", "type": "MARKET",
                               "quantity": 7, "status": "NEW"}])
        code, _, err = self.place("1", "--resend")
        self.assertEqual(code, 2)
        self.assertIn("pending order", err)
        self.assertEqual(self.router.posts(), [])
        # or it filled: the position is there
        self.router = Router()
        t212._open = self.router
        self.account(positions=HELD7)
        code, _, err = self.place("1", "--resend")
        self.assertEqual(code, 2)
        self.assertIn("already hold", err)
        self.assertEqual(self.router.posts(), [])

    def test_a_resend_with_a_clean_account_sends_once(self):
        self.account(market=http_error(400, '{"code":"InsufficientResources"}'))
        self.place()
        self.router = Router()
        t212._open = self.router
        self.account()
        code, _, err = self.place("1", "--resend")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.router.posts("/equity/orders/market")), 1)

    def test_a_failed_stop_is_not_retried_and_shouts(self):
        self.account(stop=http_error(400, "bad stop"))
        code, out, err = self.place()
        self.assertEqual(code, 3)
        self.assertEqual(len(self.router.posts("/equity/orders/stop")), 1)
        self.assertIn("ENTRY PLACED, STOP NOT PLACED", err)
        self.assertIn("SELL 7 TSLA_US_EQ at 358.32", err)

    def test_no_stop_until_the_entry_fills(self):
        self.account(filled=[])
        code, out, err = self.place()
        self.assertEqual(code, 3)
        self.assertEqual(self.router.posts("/equity/orders/stop"), [])
        self.assertIn("has not filled yet", err)
        self.assertEqual(self.broker_log()[-1]["status"], "stop_not_placed")


class Duplicates(Base):
    def test_a_pending_order_for_the_ticker_blocks(self):
        self.account(pending=[{"ticker": "TSLA_US_EQ", "side": "BUY", "type": "LIMIT", "quantity": 3,
                               "status": "NEW"}])
        code, _, err = self.place()
        self.assertEqual(code, 2)
        self.assertEqual(self.router.posts(), [])
        self.assertIn("refused", [e["event"] for e in self.order_log()])

    def test_an_existing_position_blocks_an_entry(self):
        self.account(positions=HELD7)
        code, _, err = self.place()
        self.assertEqual(code, 2)
        self.assertEqual(self.router.posts(), [])

    def test_other_tickers_do_not(self):
        self.account(pending=[{"ticker": "AAPL_US_EQ", "side": "BUY", "quantity": 1}],
                     positions=[{"instrument": {"ticker": "AAPL_US_EQ"}, "quantity": 3}])
        code, _, err = self.place()
        self.assertEqual(code, 0, err)

    def test_the_positions_check_asks_for_this_ticker(self):
        self.account()
        self.place()
        q = [c[3] for c in self.router.calls if c[1] == "/equity/positions"]
        self.assertTrue(q and all(x == "ticker=TSLA_US_EQ" for x in q), q)


# ------------------------------------------------------------------ records

class Records(Base):
    def test_every_order_and_answer_is_logged_without_credentials(self):
        self.account()
        code, out, err = self.place()
        self.assertEqual(code, 0, err)
        events = [(e["event"], e.get("path")) for e in self.order_log()]
        self.assertEqual(events, [("request", "/equity/orders/market"), ("response", "/equity/orders/market"),
                                  ("request", "/equity/orders/stop"), ("response", "/equity/orders/stop")])
        log = self.order_log()
        self.assertEqual(log[0]["body"], {"ticker": "TSLA_US_EQ", "quantity": 7, "extendedHours": False})
        self.assertEqual(log[1]["body"]["id"], 5001)
        self.assert_no_secrets(out, err)

    def test_a_failure_is_logged_and_scrubbed(self):
        self.account(market=http_error(401, "bad key %s %s" % (KEY, TOKEN)))
        code, out, err = self.place()
        self.assertEqual(code, 1)
        self.assertEqual([e["event"] for e in self.order_log()], ["request", "response"])
        self.assertEqual(self.order_log()[1]["outcome"], "rejected")
        self.assert_no_secrets(out, err)

    def test_trader_risk_counts_it(self):
        self.account()
        self.place()
        state = self.read("state/broker.json")
        placed = check.placed_today(state, TODAY)
        self.assertEqual(len(placed), 1)
        self.assertEqual(placed[0]["kind"], "entry")
        self.assertEqual(placed[0]["broker"], "trading212")
        self.assertLessEqual(placed[0]["max_loss"], 50)
        self.assertEqual(placed[0]["broker_order_id"], 5001)
        self.assertEqual(ticket.latest(state, ticket.fingerprint(tsla_ticket()), TODAY)["status"], "stop_placed")

    def test_sending_is_recorded_before_the_post(self):
        seen = []

        def spy(req, timeout):
            if req.get_method() == "POST" and not seen:
                seen.append([r["status"] for r in self.broker_log()])
            return self.router(req, timeout)
        t212._open = spy
        self.account()
        self.place()
        self.assertEqual(seen[0], ["sending"])


if __name__ == "__main__":
    unittest.main()
