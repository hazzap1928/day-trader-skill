"""11 Nov 2026 fixes. No free cash: the autopilot skips a buy before sending it (free cash minus buys
waiting to fill minus the cash buffer, with the FX fee), stops building the rest of that scan, sends no
message per stock and one summary per scan; Trading 212's own insufficient-funds refusal is treated the
same; neither counts toward max_tickets_per_day. A late fill: the entry fills after its stop was due,
and the guard's next pass places the stop and says so; a stop that cannot be placed is alarmed again
at most once an hour. Fakes only: no network, no real order, no phone."""

import io
import json
from contextlib import redirect_stdout

from test_t212_place import FakeResp, http_error
from test_autopilot import PilotBase, ap, iso
from test_evidence import EVIDENCE, ev, t212
from test_t212_guard import Base, position, entry_record, TICKER

LOW = {"id": 1, "currency": "GBP", "totalValue": 5000.0,
       "cash": {"availableToTrade": 300.0, "reservedForOrders": 0, "inPies": 0}}
NO_FUNDS = json.dumps({"type": "/api-errors/insufficient-free-for-stocks-buy", "title": "Error while placing the order",
                       "status": 400, "detail": "Insufficient funds", "traceId": "9fd58af426f042c4"})


class CashPilot(PilotBase):
    def setUp(self):
        super().setUp()
        self.cfg = dict(self.cfg, allowed_tickers=["TSLA", "AAPL", "MSFT"],
                        autopilot={"auto_buy": True, "evidence_mode": True}, evidence=EVIDENCE)
        self.write("config.json", self.cfg)
        self._kv = ev.kronos_verdict
        ev.kronos_verdict = lambda sig, **kw: {"verdict": "agree", "avg_R_net": 0.2}

    def tearDown(self):
        ev.kronos_verdict = self._kv
        super().tearDown()

    def low_cash(self, pending=()):
        r = self.router
        r.add("GET", "/equity/account/summary", FakeResp(LOW))
        r.add("GET", "/equity/orders", FakeResp(list(pending)))
        r.add("GET", "/equity/positions", FakeResp([]))

    def acct(self, **kw):
        """The clean fake account, with the autopilot's own look at positions first."""
        self.router.add("GET", "/equity/positions", FakeResp([]))
        self.account(**kw)

    def texts(self):
        return [m[0] for m in self.phone.sent]

    def built(self):
        return [c for c in self.pipe.calls if c[0].endswith("timemachine.py")]


class NoFreeCash(CashPilot):
    def test_several_candidates_no_per_stock_messages_and_exactly_one_summary(self):
        self.pipe.triggered = ("TSLA", "AAPL", "MSFT")
        self.low_cash()
        p = self.pilot()
        line = p.cycle()
        self.assertEqual(self.router.posts(), [])                       # nothing sent to Trading 212
        self.assertEqual(self.texts(), ["3 candidates skipped, no free cash (demo): TSLA, AAPL, MSFT"])
        self.assertIn("3 candidates skipped, no free cash", line)
        self.assertEqual(len(self.built()), 1)                          # the rest were not built
        self.assertNotIn("AAPL", p.state["tried"])                      # so a later scan can try them
        self.assertEqual(p.state["tried"]["TSLA"], "skipped: no free cash")
        self.assertEqual(p.counted(), [])                               # not toward max_tickets_per_day
        self.assertEqual(ev.load(self.dir, "signals")[0]["decision"], "skipped: no free cash")
        self.assertTrue(any("TSLA skipped, no free cash: NOT PLACED: no free cash: free cash GBP 300.00" in o
                            for o in self.out), self.out)

    def test_the_next_scan_builds_nothing_and_does_not_repeat_the_message(self):
        self.pipe.triggered = ("TSLA", "AAPL", "MSFT")
        self.low_cash()
        p = self.pilot()
        p.cycle()
        p.cycle()
        self.assertEqual(len(self.built()), 1)
        self.assertEqual(len(self.texts()), 1)
        self.assertTrue(any("already on your phone today" in o for o in self.out), self.out)

    def test_cash_freed_by_a_sale_lets_the_next_scan_buy(self):
        self.pipe.triggered = ("TSLA", "AAPL")
        self.low_cash()
        p = self.pilot()
        p.cycle()                                                       # TSLA skipped (tried), AAPL not built
        self.router.routes.clear()
        self.router.add("GET", "/equity/orders", FakeResp([]))          # the scan's own cash look
        self.acct()                                                     # 4,100 free again
        pipe = self.pipe                                                # a quote for AAPL too
        ap._run = lambda args, timeout=None: (
            self.write("prices.json", {"as_of": iso(self.clock[0]), "prices": {"TSLA": 364.5, "AAPL": 364.5}})
            or (0, "", "")) if args[0].endswith("alerts.py") else pipe(args, timeout)
        p.cycle()                                                       # AAPL now
        self.assertEqual([x[1] for x in self.router.posts()], ["/equity/orders/market", "/equity/orders/stop"])
        self.assertFalse(p.state["cash_short"])

    def test_a_buy_waiting_to_fill_is_not_free_cash(self):
        self.pipe.triggered = ("TSLA",)
        self.acct(pending=[{"id": 1, "ticker": "CHTR_US_EQ", "type": "MARKET", "side": "BUY", "quantity": 20.65,
                               "filledQuantity": 0, "status": "NEW"}])
        self.write("state/broker.json", {"orders": [{"side": "buy", "t212_ticker": "CHTR_US_EQ", "entry": 133.06,
                                                     "status": "stop_not_placed"}]})
        p = self.pilot()
        p.cycle()                    # 4,100.25 - CHTR 2,201.85 waiting - 500 buffer < TSLA's 2,335
        self.assertEqual(self.router.posts(), [])
        self.assertEqual(self.texts(), ["1 candidate skipped, no free cash (demo): TSLA"])

    def test_the_cost_and_the_free_cash(self):
        fc = t212.free_cash(LOW, [{"ticker": "X_US_EQ", "side": "BUY", "quantity": 10, "filledQuantity": 4,
                                   "limitPrice": 12.5}], 1.25, {"cash_buffer_pct": 10, "budget_gbp": 1000})
        self.assertEqual((fc["cash"], fc["waiting"], fc["buffer"], fc["available"]), (300.0, 60.09, 100.0, 139.91))
        sells = [{"ticker": "X_US_EQ", "type": "STOP", "quantity": -10, "stopPrice": 9}]
        self.assertEqual(t212.free_cash(LOW, sells, 1.25, {})["available"], 300.0)   # a sell holds no cash
        unknown = t212.free_cash(LOW, [{"ticker": "Y_US_EQ", "side": "BUY", "quantity": 1}], 1.25, {})
        self.assertEqual(unknown["unknown"], ["Y_US_EQ"])


class InsufficientFundsFromT212(CashPilot):
    def test_logged_not_messaged_not_retried_and_one_summary(self):
        self.pipe.triggered = ("TSLA", "AAPL")
        self.acct(market=http_error(400, NO_FUNDS))
        p = self.pilot()
        p.cycle()
        self.assertEqual(len(self.router.posts()), 1)                   # the one refused buy, never retried
        self.assertEqual(self.texts(), ["2 candidates skipped, no free cash (demo): TSLA, AAPL"])
        self.assertFalse(any("traceId" in t or "NOT PLACED" in t for t in self.texts()))
        self.assertEqual(p.state["tickets"][0]["status"], "no cash")
        self.assertEqual(p.state["tickets"][0]["note"], "Trading 212 refused it: insufficient funds")
        self.assertEqual(p.counted(), [])


class TicketCap(CashPilot):
    def test_only_buys_count_and_the_cap_holds_inside_a_scan(self):
        self.write("config.json", dict(self.cfg, autopilot={"auto_buy": True, "evidence_mode": True,
                                                            "max_tickets_per_day": 1}))
        self.pipe.triggered = ("TSLA", "AAPL")
        self.acct()
        p = self.pilot()
        p.state["tickets"] = [{"status": "no cash"}, {"status": "refused"}, {"status": "no cash"}]
        line = p.cycle()
        self.assertEqual(len(self.router.posts("/equity/orders/market")), 1)    # TSLA bought
        self.assertIn("ticket limit: 1 today", line)
        self.assertNotIn("AAPL", p.state["tried"])
        self.assertIn("1 tickets today, the most", p.cycle())

    def test_other_not_placed_reasons_still_message(self):
        self.pipe.quote = 350.0                                         # under the 358.32 stop
        self.acct()
        self.pilot().cycle()
        self.assertTrue(self.texts()[0].startswith("NOT PLACED (demo): TSLA passed the checks"), self.texts())


class LateFill(CashPilot):
    def test_an_entry_that_fills_after_the_stop_was_due_gets_its_stop_on_the_next_guard_pass(self):
        self.pipe.triggered = ("TSLA",)
        self.acct(filled=[])                                         # not filled when the stop is due
        p = self.pilot()
        p.cycle()
        self.assertEqual([x[1] for x in self.router.posts()], ["/equity/orders/market"])
        self.assertTrue(any("TSLA BOUGHT BUT STOP NOT PLACED" in t for t in self.texts()))   # still sent
        rec = [r for r in self.read("state/broker.json")["orders"] if r.get("status") == "stop_not_placed"][0]
        qty = float(rec["qty"])
        # it fills; the guard's next pass sees the position and no stop order
        self.router.routes.clear()
        self.router.add("GET", "/equity/positions", FakeResp([position(qty=qty)]))
        self.router.add("GET", "/equity/orders", FakeResp([]))
        self.router.add("POST", "/equity/orders/stop", FakeResp({"id": 9001, "status": "NEW", "type": "STOP"}))
        with redirect_stdout(io.StringIO()):
            res = t212.guard_pass(self.client, self.dir)
            t212.guard_phone(self.client, res)
        stop = self.router.posts("/equity/orders/stop")[-1][2]
        self.assertEqual((stop["ticker"], stop["quantity"], stop["stopPrice"]), (TICKER, -qty, 358.32))
        self.assertTrue(any("stop placed: SELL %s at 358.32" % t212._qty(qty) in m for m in self.pushed), self.pushed)


class RealertHourly(Base):
    def setUp(self):
        super().setUp()
        self.write("state/broker.json", {"orders": [entry_record()]})
        self.router.add("GET", "/equity/positions", FakeResp([position()]))
        self.router.add("GET", "/equity/orders", FakeResp([]))
        self.router.add("POST", "/equity/orders/stop", http_error(400, "bad stop"))

    def alarms(self):
        return [m for m in self.pushed if m.startswith("GUARD ALERT")]

    def run_pass(self, minutes_later=0):
        self.clock[0] += minutes_later * 60
        with redirect_stdout(io.StringIO()):
            t212.guard_pass(self.client, self.dir)

    def test_a_stop_that_cannot_be_placed_is_alarmed_again_at_most_once_an_hour(self):
        self.client = t212.Client("demo", "k", "s", state_dir=self.dir, log=lambda m: None)
        self.run_pass()
        self.assertEqual(len(self.alarms()), 1)                         # refused: software stop and the alarm
        self.run_pass(30)
        self.assertEqual(len(self.alarms()), 1)
        self.run_pass(31)
        self.assertEqual(len(self.alarms()), 2)
        self.assertIn("STOP STILL NOT PLACED", self.alarms()[-1])
        self.run_pass(5)
        self.run_pass(30)
        self.assertEqual(len(self.alarms()), 2)
        self.assertEqual(len(self.router.posts()), 1)                   # the stop itself is not resent
