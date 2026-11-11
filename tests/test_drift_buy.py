"""Post-earnings drift, stage 3: drift BUYING (trader-broker/drift_buy.py), off unless drift.buy_enabled.
A fake Trading 212, fake daily bars and quotes, a fake phone: no network, no real order."""

import calendar
import datetime
import io
import json
import os
import types
import unittest
from contextlib import redirect_stdout

from _load import load
import test_t212_place
from test_t212_place import Base, FakeResp, CREDS, http_error
import test_risk

t212 = test_t212_place.t212
alloc = load("trader-broker", "allocation")
drift = load("trader-broker", "drift")
db = load("trader-broker", "drift_buy")
db.t212, db.allocation, db.drift = t212, alloc, drift
check = test_risk.check
day_risk = load("trader-broker", "day_risk")

D = datetime.date
DAY = D(2026, 11, 5)                                     # Thursday
NY_0933 = datetime.datetime(2026, 11, 5, 9, 33)
UTC_0933 = float(calendar.timegm((2026, 11, 5, 14, 33, 0)))
SELL = "2026-12-03"                                      # the 20th trading day held, Thanksgiving skipped
TICKER = "TSLA_US_EQ"
RATE = 1.25
DRIFT_ON = {"scan_enabled": True, "buy_enabled": True}
ALLOC_ON = {"enabled": True, "max_open_positions": 4, "free_pool_pct": 50, "max_reservations": 2,
            "window_minutes": 90}


def bars(close=366.0, spread=4.0, days=30, end=DAY):
    """Flat daily bars before `end`: high - low = spread, so the ATR is `spread`."""
    out, d = {}, end - datetime.timedelta(days=60)
    while d < end:
        if drift.is_trading_day(d):
            out[d] = (close, close + spread / 2, close - spread / 2, close, 1000.0)
        d += datetime.timedelta(days=1)
    return out


def held(qty, ticker=TICKER, price=366.0):
    return {"instrument": {"ticker": ticker}, "quantity": qty, "quantityAvailableForTrading": qty,
            "currentPrice": price, "averagePricePaid": price}


def record(ticker=TICKER, strategy="drift", date="2026-11-03", status="stop_placed", **kw):
    return dict({"date": date, "index": 1, "fingerprint": "fp-" + ticker + date, "symbol": ticker.split("_")[0],
                 "side": "buy", "qty": "0.6", "kind": "entry", "broker": "trading212", "env": "demo",
                 "t212_ticker": ticker, "stop": 340.0, "entry": 360.0, "max_loss_gbp": 9.0, "cost_gbp": 180.0,
                 "status": status, "strategy": strategy, "sell_date": "2026-12-01"}, **kw)


class DriftBase(Base):
    def setUp(self):
        super().setUp()
        self.clock[0] = UTC_0933
        self.write("cache/t212-instruments-demo.json", {"env": "demo", "fetched_at": UTC_0933,
                                                        "instruments": test_t212_place.INSTRUMENTS})
        self.cfg = {"broker": "trading212", "paper": True, "currency": "GBP", "budget_gbp": 5000.0,
                    "cash_buffer_pct": 10, "max_loss_per_trade_gbp": 50.0, "max_daily_loss_gbp": 150.0,
                    "max_position_pct": 22, "fractional": True, "allowed_tickers": ["TSLA", "AAPL"],
                    "allocation": dict(ALLOC_ON), "drift": dict(DRIFT_ON)}
        self.write("config.json", self.cfg)
        self.write("state/drift/shortlist.json", {"buy_day": DAY.isoformat(), "report_day": "2026-11-03",
                                                  "shortlist": [{"symbol": "TSLA", "surprise_pct": 20.0}]})
        drift.save_signals(self.dir, [{"id": "2026-11-03-TSLA", "symbol": "TSLA", "entry_day": DAY.isoformat(),
                                       "shortlisted": True, "results": {}, "extras": {}}])
        self.client = t212.Client("demo", CREDS["T212_API_KEY"], CREDS["T212_API_SECRET"], state_dir=self.dir,
                                  log=lambda m: None)
        self._daily = drift.daily_bars
        drift.daily_bars = lambda sym, a, b: bars()
        self.pushed_db, self.said = [], []
        self.price = 366.0

    def tearDown(self):
        drift.daily_bars = self._daily
        super().tearDown()

    def account(self, positions=(), pending=(), summary=None, filled=0.76, stop_reply=None, market=None):
        """Fake Trading 212 for one drift buy: the buy step looks, place looks, the buy fills, the stop goes in."""
        r = self.router
        r.add("GET", "/equity/positions", FakeResp(list(positions)), FakeResp(list(positions)),
              FakeResp(list(positions) + [held(filled)]))
        stop_echo = {"id": 5002, "ticker": TICKER, "type": "STOP", "side": "SELL", "quantity": -filled,
                     "stopPrice": 354.0, "status": "NEW"}
        r.add("GET", "/equity/orders", FakeResp(list(pending)), FakeResp(list(pending)),
              FakeResp(list(pending) + [stop_echo]))
        r.add("GET", "/equity/account/summary", FakeResp(summary or test_t212_place.SUMMARY))
        r.add("POST", "/equity/orders/market", market or FakeResp({"id": 5001, "status": "NEW", "side": "BUY"}))
        r.add("POST", "/equity/orders/stop", stop_reply or FakeResp({"id": 5002, "status": "NEW", "side": "SELL"}))

    def buy(self, cfg=None, ny=NY_0933, paused=False, client=None):
        with redirect_stdout(io.StringIO()):
            return db.buy_today(client or self.client, self.dir, cfg or self.cfg, ny, RATE, lambda s: self.price,
                                paused, self.said.append, self.pushed_db.append)

    def records(self):
        return (self.read("state/broker.json") or {}).get("orders", [])

    def signal(self):
        return drift.load_signals(self.dir)[0].get("buy") or {}

    def reservation(self):
        return db.drift_reservation(alloc.load_plan(self.dir, DAY.isoformat()), "TSLA", status=None)


class Off(DriftBase):
    def test_buy_enabled_false_changes_nothing(self):
        for cfg in (dict(self.cfg, drift={"scan_enabled": True}), dict(self.cfg, drift={"scan_enabled": True,
                                                                                         "buy_enabled": False})):
            res = self.buy(cfg)
            self.assertEqual(res["status"], "off")
        self.assertEqual(self.router.calls, [])
        self.assertEqual(self.pushed_db, [])
        self.assertIsNone(self.signal().get("status"))
        self.assertFalse(os.path.exists(os.path.join(self.dir, "state", "drift", "buys.jsonl")))
        self.assertFalse(os.path.exists(alloc.path(self.dir)))

    def test_settings_default_off_with_the_agreed_defaults(self):
        s = drift.settings({})
        self.assertFalse(s["buy_enabled"])
        self.assertEqual((s["slots"], s["atr_mult"], s["max_share_pct"], s["max_open_risk_pct"], s["buy_at"],
                          s["min_trades"]), (10, 3.0, 50.0, 4.0, "09:33", 20))
        for bad in ({"buy_at": "09:50"}, {"max_open_risk_pct": 0}, {"buy_enabled": "yes"}, {"atr_mult": 0}):
            with self.assertRaises(drift.DriftError, msg=bad):
                drift.settings({"drift": bad})

    def test_live_refuses_drift_buying(self):
        live = t212.Client("live", "k", "s", state_dir=self.dir, log=lambda m: None)
        live.live_ok = True
        self.assertEqual(self.buy(client=live)["status"], "refused: drift buying is demo only")
        self.assertFalse(db.active(dict(self.cfg, paper=False)))
        self.assertFalse(db.active(dict(self.cfg, live_enabled=True)))
        self.assertEqual(self.buy(cfg=dict(self.cfg, paper=False))["status"], "off")
        self.assertEqual(self.router.calls, [])


class Buys(DriftBase):
    def test_a_drift_buy_is_labelled_with_its_sell_date_and_a_real_stop(self):
        self.account()
        res = self.buy()
        self.assertEqual([b["symbol"] for b in res["bought"]], ["TSLA"], res)
        posts = self.router.posts()
        self.assertEqual([p[1] for p in posts], ["/equity/orders/market", "/equity/orders/stop"])
        self.assertEqual(posts[0][2]["quantity"], 0.76)               # one slot: GBP 225 at $366 and 1.25
        self.assertEqual(posts[1][2]["stopPrice"], 354.0)             # 366 - 3 x ATR 4
        self.assertEqual(posts[1][2]["timeValidity"], "GOOD_TILL_CANCEL")
        recs = [r for r in self.records() if r.get("status") == "stop_placed"]
        self.assertEqual((recs[0]["strategy"], recs[0]["sell_date"]), ("drift", SELL))
        self.assertTrue(all(r["strategy"] == "drift" for r in self.records()))
        self.assertEqual(self.signal()["status"], "bought")
        self.assertEqual(self.reservation()["status"], "used")

    def test_the_drift_buy_message(self):
        self.account()
        self.buy()
        text = [p for p in self.pushed_db if p.startswith("DRIFT BUY")][0]
        for part in ("0.76 TSLA", "cost about £", "Stop $354.00", "Sell date Thu 03 Dec", "Max loss at the stop about £"):
            self.assertIn(part, text)

    def test_a_real_stop_whatever_stop_mode_or_fractional_stops_say(self):
        for over in ({"stop_mode": "market"}, {"fractional_stops": "software"}):
            self.router.routes.clear()
            self.router.calls.clear()
            self.write("state/broker.json", {"orders": []})
            self.write("state/allocation.json", {})
            cfg = dict(self.cfg, **over)
            self.write("config.json", cfg)
            self.account()
            self.buy(cfg)
            self.assertEqual([p[1] for p in self.router.posts()], ["/equity/orders/market", "/equity/orders/stop"], over)

    def test_a_refused_stop_becomes_a_software_stop_with_an_alarm(self):
        self.account(stop_reply=http_error(400, "stop refused"))
        res = self.buy()
        self.assertFalse(res["bought"][0]["stop_ok"])
        self.assertIn("NOT IN PLACE", [p for p in self.pushed_db if p.startswith("DRIFT BUY")][0])
        self.assertEqual(self.records()[-1]["status"], "stop_not_placed")
        self.assertTrue(any(p.startswith("GUARD ALERT (DEMO): DRIFT TSLA: bought, but its stop") and
                            "NOT PROTECTED OVERNIGHT" in p for p in self.pushed))

    def test_drift_reserves_its_own_cash_when_the_morning_plan_is_missing(self):
        self.assertFalse(os.path.exists(alloc.path(self.dir)))
        self.account()
        self.buy()
        plan = alloc.load_plan(self.dir, DAY.isoformat())
        self.assertIn("drift buy step", plan["made_by"])
        self.assertEqual(self.reservation()["status"], "used")

    def test_drift_adds_its_reservation_to_a_morning_plan_without_one(self):
        alloc.save_plan(self.dir, alloc.make_plan(self.cfg, DAY.isoformat(), [{"symbol": "AAPL", "reason": "x"}]))
        self.account()
        self.buy()
        plan = alloc.load_plan(self.dir, DAY.isoformat())
        self.assertEqual([r["symbol"] for r in plan["reservations"]], ["AAPL", "TSLA"])
        self.assertEqual(self.reservation()["status"], "used")

    def test_no_reservation_no_buy(self):
        self.router.add("GET", "/equity/positions", FakeResp([]))
        self.router.add("GET", "/equity/orders", FakeResp([]))
        cfg = dict(self.cfg, allocation=dict(ALLOC_ON, enabled=False))
        res = self.buy(cfg)
        self.assertEqual(res["bought"], [])
        self.assertEqual(self.router.posts(), [])
        self.assertIn("no cash reservation", self.signal()["why"])
        self.assertTrue(any("no drift buys today" in p for p in self.pushed_db))
        saved = alloc._write
        alloc._write = lambda p, d: (_ for _ in ()).throw(OSError("disk full"))
        try:
            self.router.routes.clear()
            self.router.add("GET", "/equity/positions", FakeResp([]))
            self.router.add("GET", "/equity/orders", FakeResp([]))
            res = self.buy()
        finally:
            alloc._write = saved
        self.assertEqual(self.router.posts(), [])
        self.assertIn("could not be made", self.signal()["why"])

    def test_the_cash_check_counts_other_reservations(self):
        poor = {"currency": "GBP", "totalValue": 5000.0, "cash": {"availableToTrade": 700.0, "reservedForOrders": 0}}
        self.account(summary=poor)
        res = self.buy()                                               # 700 - 500 buffer = 200 < about 224
        self.assertEqual(res["bought"], [])
        self.assertIn("no free cash", self.signal()["why"])
        self.assertEqual(self.reservation()["status"], "released")
        self.assertEqual(self.router.posts(), [])


class Skips(DriftBase):
    def look(self, positions=(), pending=()):
        self.router.add("GET", "/equity/positions", FakeResp(list(positions)))
        self.router.add("GET", "/equity/orders", FakeResp(list(pending)))

    def test_blocked(self):
        self.look()
        self.buy(dict(self.cfg, blocked_tickers=["TSLA"]))
        self.assertEqual(self.signal()["why"], "on blocked_tickers")
        self.assertEqual(self.router.posts(), [])

    def test_held_by_either_strategy_or_waiting(self):
        for positions, pending in (([held(3)], []), ([], [{"id": 1, "ticker": TICKER, "side": "BUY", "quantity": 2}])):
            self.router.routes.clear()
            self.look(positions, pending)
            self.buy()
            self.assertIn("already held or waiting", self.signal()["why"])
        self.assertEqual(self.router.posts(), [])

    def test_already_under_its_stop_at_the_open(self):
        self.look()
        self.price = 353.0                                             # yesterday 366 - 3 x 4 = 354
        self.buy()
        self.assertIn("already at or under its stop at the open", self.signal()["why"])
        self.assertEqual(self.router.posts(), [])

    def test_no_free_slot(self):
        self.write("state/broker.json", {"orders": [record("AAPL_US_EQ")]})
        self.look([held(0.5, "AAPL_US_EQ", 360.0)])
        self.buy(dict(self.cfg, drift=dict(DRIFT_ON, slots=1)))
        self.assertIn("no free drift slot (1 of 1 in use)", self.signal()["why"])

    def test_the_drift_pool_is_full(self):
        self.write("state/broker.json", {"orders": [record("AAPL_US_EQ")]})
        self.look([held(4.0, "AAPL_US_EQ", 360.0)])                   # about GBP 1,150 of a GBP 2,250 pool at 50%
        self.buy(dict(self.cfg, drift=dict(DRIFT_ON, max_share_pct=25)))
        self.assertIn("the drift pool is used", self.signal()["why"])

    def test_the_open_risk_cap(self):
        self.look()
        self.buy(dict(self.cfg, drift=dict(DRIFT_ON, max_open_risk_pct=0.1)))     # GBP 5 cap
        self.assertIn("over the GBP 5.00 cap", self.signal()["why"])
        self.assertEqual(self.router.posts(), [])

    def test_pause_stops_drift_buys_too(self):
        self.buy(paused=True)
        self.assertEqual(self.signal()["why"], "paused from your phone (/pause)")
        self.assertEqual(self.router.calls, [])

    def test_a_late_start_is_missed_never_bought_late(self):
        self.buy(ny=datetime.datetime(2026, 11, 5, 9, 50))
        self.assertIn("missed", self.signal()["why"])
        self.assertTrue(any("MISSED" in p for p in self.pushed_db))
        self.assertEqual(self.router.calls, [])

    def test_no_shortlist_for_today(self):
        self.write("state/drift/shortlist.json", {"buy_day": "2026-11-04", "shortlist": [{"symbol": "TSLA"}]})
        self.assertIn("no shortlist for today", self.buy()["status"])
        self.assertEqual(self.router.calls, [])

    def test_every_skip_is_also_in_buys_jsonl(self):
        self.look()
        self.buy(dict(self.cfg, blocked_tickers=["TSLA"]))
        rows = db.load_jsonl(os.path.join(self.dir, "state", "drift", "buys.jsonl"))
        self.assertEqual([(r["symbol"], r["status"]) for r in rows], [("TSLA", "skipped")])


class PlaceRefusesDrift(DriftBase):
    def place(self, cfg, env="demo", sell_date=SELL):
        self.write("config.json", cfg)
        order = {"symbol": "TSLA", "side": "buy", "qty": "0.5", "type": "market", "time_in_force": "day",
                 "stop_loss_stop_price": 354.0, "plan": {"entry": 366.0, "stop": 354.0}}
        fp = t212.ticket_module().fingerprint(order)
        self.write("state/drift/orders.json", {"orders": [order]})
        self.write("state/drift/risk.json", {"date": DAY.isoformat(), "results": [
            {"index": 1, "fingerprint": fp, "verdict": "PASS", "kind": "entry", "reasons": []}]})
        client = t212.Client(env, "k", "s", state_dir=self.dir, log=lambda m: None)
        client.live_ok = True
        import argparse
        args = argparse.Namespace(ticket_id=fp, resend=False, fx_rate=1.25, state_dir=self.dir, json=False, live=False,
                                  strategy="drift", sell_date=sell_date,
                                  orders_path=os.path.join(self.dir, "state", "drift", "orders.json"),
                                  risk_path=os.path.join(self.dir, "state", "drift", "risk.json"))
        with self.assertRaises(t212.T212Error) as e:
            with redirect_stdout(io.StringIO()):
                t212.cmd_place(client, args, confirm=lambda *a: (True, "yes", ""))
        self.assertEqual(self.router.posts(), [])
        return str(e.exception)

    def test_live_off_and_bad_sell_dates_are_refused_before_anything_is_sent(self):
        self.assertIn("demo only", self.place(dict(self.cfg, paper=False), env="live"))
        self.assertIn("demo only", self.place(dict(self.cfg, paper=False)))
        self.assertIn("buy_enabled is off", self.place(dict(self.cfg, drift={"scan_enabled": True})))
        self.assertIn("sell_date after today", self.place(self.cfg, sell_date=DAY.isoformat()))
        self.assertIn("sell_date after today", self.place(self.cfg, sell_date=None))


class IntradayUnchanged(unittest.TestCase):
    """Drift never counts toward intraday's trades a day, daily loss or its 4-position limit."""

    def test_trader_risk_does_not_count_drift_entries(self):
        c = test_risk.cfg(max_trades_per_day=1)
        now = test_risk.at("2026-10-19 10:15")
        drift_rec = {"date": "2026-10-19", "status": "placed", "side": "buy", "kind": "entry", "symbol": "AAPL",
                     "strategy": "drift", "max_loss": 40}
        res = check.check([test_risk.tsla()], c, state={"orders": [drift_rec]}, now=now)
        self.assertEqual(res["results"][0]["verdict"], "PASS", res["results"][0]["reasons"])
        intraday = dict(drift_rec, strategy="intraday")
        res = check.check([test_risk.tsla()], c, state={"orders": [intraday]}, now=now)
        self.assertEqual(res["results"][0]["verdict"], "BLOCK")              # the limit still works for intraday
        self.assertTrue(any("trades" in x for x in res["results"][0]["reasons"]))

    def test_day_risk_leaves_drift_out(self):
        st = {"orders": [dict(record(date="2026-11-05", status="placed"), fingerprint="a"),
                         dict(record(date="2026-11-05", status="placed", strategy="intraday"), fingerprint="b")]}
        self.assertEqual([r["fingerprint"] for r in day_risk.entries_today(st, "2026-11-05")], ["b"])

    def test_the_morning_plan_puts_drift_first_and_shrinks_intraday(self):
        cfg = {"paper": True, "budget_gbp": 5000.0, "cash_buffer_pct": 10, "max_position_pct": 22,
               "allocation": dict(ALLOC_ON), "drift": dict(DRIFT_ON)}
        cands = [{"symbol": "AAPL", "reason": "x"}, {"symbol": "MSFT", "reason": "y"}]
        self.assertEqual(len(alloc.make_plan(cfg, "2026-11-05", cands)["reservations"]), 2)
        dres, _ = db.plan_reservations(cfg, ["TSLA", "NVDA"], 1800.0, 8)
        self.assertEqual([r["amount_gbp"] for r in dres], [225.0, 225.0])
        plan = alloc.make_plan(cfg, "2026-11-05", cands, drift=dres, drift_used_gbp=1800.0)
        self.assertEqual([r["symbol"] for r in plan["reservations"]], ["TSLA", "NVDA", "AAPL"])   # room for 1 intraday
        self.assertIsNone(alloc.reservation_for(plan, "TSLA"))                 # intraday never takes drift's
        self.assertEqual(alloc.reserved_for_others.__name__, "reserved_for_others")

    def test_plan_reservations_respects_slots_and_pool(self):
        cfg = {"paper": True, "budget_gbp": 5000.0, "cash_buffer_pct": 10, "drift": dict(DRIFT_ON)}
        res, skipped = db.plan_reservations(cfg, ["A", "B", "C"], 0.0, 9)
        self.assertEqual([r["symbol"] for r in res], ["A"])
        self.assertIn("no free drift slot", dict(skipped)["B"])
        res, skipped = db.plan_reservations(cfg, ["A"], 2100.0, 0)
        self.assertEqual(res, [])
        self.assertIn("drift pool is used", dict(skipped)["A"])

    def test_slot_sizes(self):
        for slots, each in ((5, 450.0), (8, 281.25), (10, 225.0)):
            f = db.figures({"budget_gbp": 5000.0, "cash_buffer_pct": 10, "drift": dict(DRIFT_ON, slots=slots)})
            self.assertEqual((f["pool_gbp"], f["slot_gbp"]), (2250.0, each))
        self.assertEqual(db.figures({"budget_gbp": 5000.0, "cash_buffer_pct": 10, "drift": DRIFT_ON})["risk_cap_gbp"],
                         200.0)


class IntradayCannotBuyWhatDriftHolds(DriftBase):
    def test_place_refuses_a_buy_of_a_stock_already_held(self):
        r = self.router
        r.add("GET", "/equity/account/summary", FakeResp(test_t212_place.SUMMARY))
        r.add("GET", "/equity/orders", FakeResp([]))
        r.add("GET", "/equity/positions", FakeResp([held(0.6)]))
        self.write("state/broker.json", {"orders": [record()]})
        code, out, err = self.place()                                 # the intraday TSLA ticket
        self.assertEqual(code, 2)
        self.assertIn("already hold", err)
        self.assertEqual(self.router.posts(), [])


class Holdings(unittest.TestCase):
    def test_day_x_of_20_gain_stop_and_sell_date(self):
        st = {"orders": [record()]}
        pos = [dict(held(0.6, price=370.0), averagePricePaid=360.0, walletImpact={"unrealizedProfitLoss": 4.5})]
        stop = {"id": 1, "ticker": TICKER, "type": "STOP", "quantity": -0.6, "stopPrice": 340.0, "status": "NEW"}
        rows = db.holdings(st, pos, [stop], "demo", {}, RATE, D(2026, 11, 5), {"drift": DRIFT_ON})
        self.assertEqual((rows[0]["day_n"], rows[0]["protected"], rows[0]["gain_gbp"]), (3, True, 4.5))
        line = db.holding_line(rows[0])
        self.assertEqual(line, "TSLA 0.6: day 3 of 20, +£4.50, stop $340.00 (stop order), sells Tue 01 Dec")
        self.assertEqual(db.unprotected(rows), [])

    def test_no_stop_order_is_not_protected_overnight(self):
        rows = db.holdings({"orders": [record()]}, [held(0.6)], [], "demo", {"software": {TICKER: {"stop": 340.0}}},
                           RATE, D(2026, 11, 5), {"drift": DRIFT_ON})
        self.assertIn("NOT PROTECTED OVERNIGHT", db.holding_line(rows[0]))
        self.assertEqual(len(db.unprotected(rows)), 1)

    def test_intraday_positions_are_not_drift(self):
        rows = db.holdings({"orders": [record(strategy="intraday")]}, [held(0.6)], [], "demo", {}, RATE, DAY,
                           {"drift": DRIFT_ON})
        self.assertEqual(rows, [])
        self.assertEqual(db.drift_tickers({"orders": [record(), record("AAPL_US_EQ", strategy="intraday")]}, "demo"),
                         {TICKER})


class Sells(DriftBase):
    def fills(self, sell_at, sell_px=380.0, frm="API"):
        buy_at = datetime.datetime(2026, 11, 3, 14, 33)
        return [{"ticker": TICKER, "side": "BUY", "qty": 0.6, "price": 360.0, "gross_gbp": 172.8, "fees_gbp": 0.26,
                 "at": buy_at, "type": "MARKET", "from": "API", "order_id": 1},
                {"ticker": TICKER, "side": "SELL", "qty": 0.6, "price": sell_px,
                 "gross_gbp": round(0.6 * sell_px / RATE, 2), "fees_gbp": 0.27,
                 "at": sell_at, "type": "MARKET", "from": frm, "order_id": 2}]

    def report_mod(self, fills):
        rp = load("trader-broker", "report")
        return types.SimpleNamespace(history=lambda c, s: fills, round_trips=rp.round_trips, alerts=rp.alerts)

    def spy(self):
        return {D(2026, 11, 3): (500.0, 501, 499, 500.0, 1), D(2026, 12, 1): (504.0, 506, 503, 505.0, 1),
                D(2026, 11, 20): (490.0, 491, 489, 490.0, 1)}

    def test_sold_on_its_sell_date_gives_result_days_and_spy(self):
        self.write("state/broker.json", {"orders": [record()]})
        self.write("state/t212-guard.json", {"eod": {"date": "2026-12-01", "sold": [{"ticker": TICKER}]}})
        self.router.add("GET", "/equity/positions", FakeResp([]))
        out = db.closed_since(self.client, self.dir, self.cfg, D(2026, 12, 1),
                              self.report_mod(self.fills(datetime.datetime(2026, 12, 1, 20, 50))), RATE, spy=self.spy())
        row, text = out[0]
        self.assertEqual((row["reason"], row["days"]), ("sell date", 20))
        self.assertAlmostEqual(row["pl_gbp"], 182.4 - 0.27 - 172.8 - 0.26, places=2)
        self.assertAlmostEqual(row["spy_pct"], 100 * drift.net_return(500.0, 505.0, 0.0005), places=2)
        self.assertTrue(text.startswith("DRIFT SELL (demo): TSLA, sell date. +£9.07 after costs"))
        self.assertIn("SPY +0.90% over the same days", text)
        self.assertIn("Held 20 trading days", text)
        again = db.closed_since(self.client, self.dir, self.cfg, D(2026, 12, 1),
                                self.report_mod(self.fills(datetime.datetime(2026, 12, 1, 20, 50))), RATE, spy=self.spy())
        self.assertEqual(again, [])                                   # reported once

    def test_sold_by_its_stop(self):
        self.write("state/broker.json", {"orders": [record()]})
        self.router.add("GET", "/equity/positions", FakeResp([]))
        out = db.closed_since(self.client, self.dir, self.cfg, D(2026, 11, 20),
                              self.report_mod(self.fills(datetime.datetime(2026, 11, 20, 16, 0), 339.0)), RATE,
                              spy=self.spy())
        self.assertEqual(out[0][0]["reason"], "stop")
        self.assertIn("DRIFT SELL (demo): TSLA, stop. -£", out[0][1])

    def test_still_held_is_not_reported(self):
        self.write("state/broker.json", {"orders": [record()]})
        self.router.add("GET", "/equity/positions", FakeResp([held(0.6)]))
        self.assertEqual(db.closed_since(self.client, self.dir, self.cfg, DAY, self.report_mod([]), RATE), [])


class EndOfDay(DriftBase):
    """Stage 1 sells drift on its sell date; an overdue one also raises an alert. buy_enabled false still sells."""

    def close(self, sell_date):
        self.write("config.json", dict(self.cfg, eod_close={"enabled": True, "at": "15:50"},
                                       drift={"scan_enabled": True, "buy_enabled": False}))
        self.write("state/broker.json", {"orders": [record(sell_date=sell_date)]})
        stop = {"id": 7001, "ticker": TICKER, "type": "STOP", "quantity": -0.6, "stopPrice": 340.0, "status": "NEW"}
        r = self.router
        r.add("GET", "/equity/positions", FakeResp([held(0.6)]))
        r.add("GET", "/equity/orders", FakeResp([stop]), FakeResp([]))
        r.add("DELETE", "/equity/orders/7001", FakeResp({}))
        r.add("POST", "/equity/orders/market", FakeResp({"id": 9300}))
        self.clock[0] = float(calendar.timegm((2026, 11, 5, 20, 50, 0)))
        with redirect_stdout(io.StringIO()):
            return t212.run_eod(self.client, self.dir)

    def test_sold_on_the_sell_date_with_buy_enabled_off(self):
        res = self.close("2026-11-05")
        self.assertEqual(len(self.router.posts()), 1)
        self.assertEqual(res["overdue"], [])

    def test_overdue_is_sold_and_alerted(self):
        res = self.close("2026-11-03")
        self.assertEqual(len(self.router.posts()), 1)
        self.assertIn("sell date was 2026-11-03", res["overdue"][0])
        self.assertTrue(any("DRIFT SELL OVERDUE" in p for p in self.pushed))

    def test_kept_before_the_sell_date(self):
        res = self.close("2026-11-06")
        self.assertEqual(self.router.posts(), [])
        self.assertEqual(len(res["kept"]), 1)


class Friday(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir)

    def rows(self, n, ret, vs):
        os.makedirs(os.path.join(self.dir, "state", "drift"), exist_ok=True)
        with open(db.trades_path(self.dir), "w") as fh:
            for i in range(n):
                fh.write(json.dumps({"fingerprint": str(i), "ret_pct": ret, "vs_spy_pct": vs}) + "\n")

    def test_too_few_then_on_track_then_not(self):
        cfg = {"paper": True, "drift": dict(DRIFT_ON, min_trades=3)}
        self.rows(2, 1.0, 0.5)
        text = "\n".join(db.friday_lines(self.dir, cfg))
        self.assertIn("Real drift trades (buying is ON): 2 closed", text)
        self.assertIn("too few trades to judge (2 of 3; reads ON TRACK so far)", text)
        self.assertIn("Shadow signals", text)
        self.rows(3, 1.0, 0.5)
        self.assertIn("ON TRACK.", "\n".join(db.friday_lines(self.dir, cfg)))
        self.rows(3, 1.0, -0.5)                                       # made money but lost to SPY
        self.assertIn("NOT ON TRACK.", "\n".join(db.friday_lines(self.dir, cfg)))

    def test_off_shows_nothing(self):
        self.assertIsNone(db.friday_lines(self.dir, {"paper": True}))


import test_autopilot                                   # noqa: E402


class FakeDriftBuy(object):
    """Stands in for drift_buy in the autopilot: records when the buy step ran."""

    def __init__(self, clock, on=True):
        self.clock, self.on, self.calls = clock, on, []

    def active(self, cfg):
        return self.on

    def settings(self, cfg):
        return {"buy_at": "09:33"}

    def buy_today(self, client, sd, cfg, ny, rate, quote, paused, say, push):
        self.calls.append((self.clock[0], paused))
        return {"status": "done", "bought": [], "skipped": []}

    def drift_tickers(self, state, env):
        return set()

    def load_jsonl(self, path):
        return []

    def trades_path(self, sd):
        return os.path.join(sd, "x")


class AutopilotDay(test_autopilot.PilotBase):
    def setUp(self):
        super().setUp()
        self.ap = test_autopilot.ap
        self._mods = dict(self.ap._MODS)

    def tearDown(self):
        self.ap._MODS.clear()
        self.ap._MODS.update(self._mods)
        super().tearDown()

    def run_day(self, on=True):
        f = FakeDriftBuy(self.clock, on)
        self.ap._MODS["drift_buy"] = f
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
            p.day()
        return f

    def test_drift_buys_once_before_the_first_intraday_scan(self):
        f = self.run_day()
        self.assertEqual(len(f.calls), 1)
        at_0933 = test_autopilot.NOW - 57 * 60
        self.assertGreaterEqual(f.calls[0][0], at_0933)
        self.assertLess(f.calls[0][0], test_autopilot.NOW - 45 * 60)  # before 09:45
        self.assertEqual(self.router.posts(), [])

    def test_off_never_runs_the_buy_step(self):
        f = self.run_day(on=False)
        self.assertEqual(f.calls, [])

    def test_positions_full_leaves_drift_out(self):
        cfg = dict(self.cfg, allocation=dict(ALLOC_ON, max_open_positions=1))
        self.write("config.json", cfg)
        self.write("state/broker.json", {"orders": [record("AAPL_US_EQ")]})
        self.router.add("GET", "/equity/positions", FakeResp([held(0.5, "AAPL_US_EQ")]))
        self.router.add("GET", "/equity/orders", FakeResp([]))
        p = self.pilot()
        self.assertIsNone(p.positions_full("MSFT"))                    # the drift AAPL does not count
        self.write("state/broker.json", {"orders": [record("AAPL_US_EQ", strategy="intraday")]})
        self.assertIn("max_open_positions", p.positions_full("MSFT"))   # an intraday AAPL does




class BriefingOvernight(unittest.TestCase):
    """The briefing's "held overnight" warning is for intraday positions; drift is expected overnight."""

    def b(self, drift_rows, rows):
        return {"account": {}, "positions": {"rows": rows, "alerts": [], "unclear": {}, "error": None},
                "earnings": {}, "eod": {"date": "2026-11-04"}, "prev": D(2026, 11, 4), "session": DAY,
                "econ": [], "mood": "up", "fx": {"rate": 1.25}, "drift_rows": drift_rows}

    def row(self, sym, drift_flag):
        return {"symbol": sym, "ticker": sym + "_US_EQ", "qty": 0.6, "covered": 0.6, "fully_covered": True,
                "drift": drift_flag}

    def test_drift_is_not_flagged_but_an_unprotected_one_is(self):
        br = load("trader-broker", "briefing")
        protected = {"symbol": "TSLA", "waiting": False, "protected": True, "stop": 340.0, "stop_how": "stop order"}
        items = br.todo(self.b([protected], [self.row("TSLA", True)]))
        self.assertFalse(any("overnight" in t for _, t in items), items)
        items = br.todo(self.b([protected], [self.row("TSLA", True), self.row("BBY", False)]))
        self.assertTrue(any(t.startswith("Still held overnight: BBY,") for _, t in items))
        bare = dict(protected, protected=False, stop_how="NO STOP ORDER: NOT PROTECTED OVERNIGHT")
        items = br.todo(self.b([bare], [dict(self.row("TSLA", True), covered=0, fully_covered=False)]))
        self.assertEqual(items[0][0], 1)
        self.assertIn("DRIFT TSLA is NOT PROTECTED OVERNIGHT", items[0][1])
        self.assertEqual(sum(1 for _, t in items if "TSLA" in t), 1)       # said once, not twice


if __name__ == "__main__":
    unittest.main()
