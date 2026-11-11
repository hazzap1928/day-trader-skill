"""autopilot.py: tickets to the phone, placed only on your Approve, demo only.
A fake pipeline, a fake phone and a fake Trading 212: no network, no real order."""

import datetime
import io
import json
import os
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load
import test_t212_place
from test_t212_place import Base, FakeResp, CREDS, NOW, TODAY, HELD7, tsla_ticket

t212 = test_t212_place.t212
ap = load("trader-broker", "autopilot")
ap.t212 = t212                        # one module instance, so the fakes in Base reach the autopilot
ap.tk = t212.ticket_module()
ap.phone_mod = t212.phone_module()
ap.net = t212.net_module()

GOOD_MC = {"p_target": 0.6, "p_stop": 0.4, "r_at_target": 1.0}
BAD_MC = {"p_target": 0.3, "p_stop": 0.7, "r_at_target": 1.0}


def iso(ts):
    return datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakePhone(object):
    def __init__(self):
        self.sent, self.edits, self.answers, self.inbox = [], [], [], []
        self.state = {"chat_id": 1, "user_id": 1}
        self.next_id = 100

    def scrub(self, t):
        return str(t)

    def save(self):
        pass

    def send(self, text, buttons=None):
        self.next_id += 1
        self.sent.append((text, buttons))
        return self.next_id

    def edit(self, mid, text):
        self.edits.append((mid, text))

    def answer(self, cid, text=""):
        self.answers.append(text)

    def updates(self, wait=0):
        out, self.inbox = self.inbox, []
        return out


class Pipeline(object):
    """Stands in for each step's process; writes the files that step would write."""

    def __init__(self, test, mc=GOOD_MC, quote=364.50, verdict="PASS", triggered=("TSLA",), order=None):
        self.t, self.mc, self.quote, self.verdict, self.triggered = test, mc, quote, verdict, triggered
        self.net_r = 0.12
        self.order = order or tsla_ticket()
        self.calls = []

    def __call__(self, args, timeout=None):
        self.calls.append(list(args))
        s, t = args[0], self.t
        if s.endswith("scan.py"):
            return (0 if self.triggered else 1), "{}", ""
        if s.endswith("entry.py") and args[1] == "watchlist":
            return 0, json.dumps([{"symbol": x, "result": {"status": "triggered"}} for x in self.triggered]), ""
        if s.endswith("entry.py"):
            t.write("entry.json", {"symbol": args[2]})
            return 0, "", ""
        if s.endswith("exit.py"):
            t.write("plan.json", self.order["plan"])
            return 0, "", ""
        if s.endswith("timemachine.py"):
            t.write("timemachine.json", {"mc": dict(self.mc, avg_R=0.3), "verdict": "Leans toward the target: test",
                                         "costs": {"mc_avg_R": 0.3, "mc_avg_R_net": self.net_r, "mc_cost_R": 0.3 - self.net_r,
                                                   "stop_pct": 1.6}})
            return 0, "", ""
        if s.endswith("alerts.py"):
            t.write("prices.json", {"as_of": iso(t.clock[0]), "prices": {"TSLA": self.quote}})
            return 0, "", ""
        if s.endswith("size.py"):
            t.write("orders.json", {"orders": [self.order]})
            return 0, "", ""
        if s.endswith("check.py"):
            o = (t.read("orders.json") or {})["orders"][0]
            t.write("state/risk.json", {"date": TODAY, "results": [
                {"index": 1, "fingerprint": ap.tk.fingerprint(o), "verdict": self.verdict, "kind": "entry",
                 "max_loss": 46.88, "reward_risk": 1.0,
                 "reasons": [] if self.verdict == "PASS" else ["over your max trades"]}]})
            return 0, "", ""
        if s.endswith("t212.py"):
            return 0, "", ""
        raise AssertionError("unexpected step %r" % args)


class PilotBase(Base):
    def setUp(self):
        super().setUp()
        self.cfg = dict(self.cfg, allowed_tickers=["TSLA", "AAPL"], paper=True)
        self.write("config.json", self.cfg)
        self.write("fx.json", {"usd_per_gbp": 1.25, "fetched_at": iso(NOW)})
        self._ap = (ap._now, ap._sleep, ap._run, ap._keep_awake, ap._launchd, ap._spawn)
        # the account snapshot for trader-budget and trader-risk reads Trading 212 three more times per build;
        # these tests queue their fake replies for the placing steps, so it is off here and tested on its own
        # (tests/test_cash_view.py)
        self._wp = ap.Pilot.write_portfolio
        ap.Pilot.write_portfolio = lambda pilot, sym: None
        ap._keep_awake = lambda: (True, "caffeinate -s is running (test)")
        self.jobs = {"com.daytraderskill.t212-guard": {"loaded": True, "running": True, "pid": "4242", "out": ""},
                     "com.daytraderskill.autopilot": {"loaded": True, "running": True, "pid": "4343", "out": ""}}
        ap._launchd = lambda label: self.jobs.get(label, {"loaded": False, "running": False, "pid": None, "out": ""})
        self.spawned = []
        ap._spawn = lambda args, log: self.spawned.append(list(args))
        ap._now = lambda: self.clock[0]
        ap._sleep = lambda s: self.clock.__setitem__(0, self.clock[0] + s)
        self.pipe = Pipeline(self)
        ap._run = self.pipe
        self.phone = FakePhone()
        self.client = t212.Client("demo", CREDS["T212_API_KEY"], CREDS["T212_API_SECRET"], state_dir=self.dir,
                                  log=lambda m: None)
        self.out = []

    def tearDown(self):
        ap._now, ap._sleep, ap._run, ap._keep_awake, ap._launchd, ap._spawn = self._ap
        ap.Pilot.write_portfolio = self._wp
        super().tearDown()

    def pilot(self):
        return ap.Pilot(self.client, self.phone, self.dir, out=self.out.append)

    def ticket(self, p=None):
        """A cycle that sends the TSLA ticket. Returns (pilot, ticket)."""
        p = p or self.pilot()
        self.router.add("GET", "/equity/positions", FakeResp([]))
        line = p.cycle()
        self.assertIn("ticket sent", line)
        self.router.routes.clear()
        return p, p.state["tickets"][-1]

    def tap(self, p, t, which="ap", nonce=None):
        p.tap({"kind": "tap", "id": "cb", "data": "%s:%s:%s" % (which, t["id"], nonce or t["nonce"]),
               "message_id": t["message_id"]})


class Ready(PilotBase):
    def test_ready_needs_demo_an_allow_list_paper_and_a_phone(self):
        self.assertEqual(ap.check_ready(self.cfg, "demo", self.phone), [])
        cases = {"demo account only": (self.cfg, "live", self.phone),
                 "allowed_tickers": (dict(self.cfg, allowed_tickers=[]), "demo", self.phone),
                 "\"paper\": true": (dict(self.cfg, paper=False), "demo", self.phone),
                 "live_enabled": (dict(self.cfg, live_enabled=True), "demo", self.phone),
                 "no phone": (self.cfg, "demo", None)}
        for word, a in cases.items():
            with self.subTest(word):
                self.assertTrue(any(word in x for x in ap.check_ready(*a)), ap.check_ready(*a))

    def test_the_pilot_refuses_a_live_client(self):
        live = t212.Client("live", "k", "s", state_dir=self.dir)
        with self.assertRaises(ap.PilotError):
            ap.Pilot(live, self.phone, self.dir)

    def test_main_refuses_t212_env_live(self):
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            code = ap.main(["--state-dir", self.dir, "status"], environ=dict(CREDS, T212_ENV="live"))
        self.assertEqual(code, 2)
        self.assertEqual(self.router.calls, [])

    def test_a_ticket_must_expire_before_the_next_cycle(self):
        with self.assertRaises(ap.PilotError):
            ap.settings({"autopilot": {"every_minutes": 15, "expire_minutes": 15}})
        self.assertEqual(ap.settings({})["expire_minutes"], 10)

    def test_place_with_a_phone_confirm_is_demo_only(self):
        live = t212.Client("live", "k", "s", state_dir=self.dir)
        live.live_ok = True
        self.router.add("GET", "/equity/account/summary", FakeResp(test_t212_place.SUMMARY))
        self.router.add("GET", "/equity/orders", FakeResp([]))
        self.router.add("GET", "/equity/positions", FakeResp([]))
        args = ap.argparse.Namespace(ticket_id="1", resend=False, fx_rate=1.25, state_dir=self.dir, json=False)
        with redirect_stdout(io.StringIO()):
            with self.assertRaises(t212.T212Error):
                t212.cmd_place(live, args, confirm=lambda i, o, sz: (True, "yes", ""))
        self.assertEqual(self.router.posts(), [])


class Cycle(PilotBase):
    def test_a_passing_ticket_goes_to_the_phone_with_two_buttons(self):
        p, t = self.ticket()
        text, buttons = self.phone.sent[-1]
        self.assertIn("DEMO TICKET: BUY 8 TSLA", text)
        self.assertIn("Nothing is placed unless you tap Approve", text)
        self.assertEqual([b[0] for b in buttons], ["Approve", "Reject"])
        self.assertEqual(buttons[0][1], "ap:%s:%s" % (t["id"], t["nonce"]))
        self.assertEqual(t["expires"] - t["created"], 600)
        self.assertEqual(self.router.posts(), [])          # sending a ticket places nothing

    def test_the_scanner_is_given_the_allow_list_only(self):
        self.ticket()
        scan = [c for c in self.pipe.calls if c[0].endswith("scan.py")][0]
        self.assertEqual(scan[scan.index("--symbols") + 1], "AAPL,TSLA")

    def test_a_trigger_off_the_list_is_never_built(self):
        self.pipe.triggered = ("NVDA",)
        self.router.add("GET", "/equity/positions", FakeResp([]))
        line = self.pilot().cycle()
        self.assertIn("NVDA not on allowed_tickers", line)
        self.assertEqual(self.phone.sent, [])

    def test_no_ticket_when_the_average_r_after_costs_is_not_positive(self):
        self.pipe.net_r = -0.24
        self.router.add("GET", "/equity/positions", FakeResp([]))
        line = self.pilot().cycle()
        self.assertIn("average R after costs -0.240 (before +0.300", line)
        self.assertEqual(self.phone.sent, [])
        tm_call = [c for c in self.pipe.calls if c[0].endswith("timemachine.py")][0]
        self.assertEqual(tm_call[tm_call.index("--config") + 1], os.path.join(self.dir, "config.json"))

    def test_a_blocked_ticker_is_neither_scanned_nor_built(self):
        self.write("config.json", dict(self.cfg, blocked_tickers=["TSLA"]))
        self.router.add("GET", "/equity/positions", FakeResp([]))
        line = self.pilot().cycle()
        scan = [c for c in self.pipe.calls if c[0].endswith("scan.py")][0]
        self.assertEqual(scan[scan.index("--symbols") + 1], "AAPL")
        self.assertIn("TSLA not on allowed_tickers", line)
        self.assertEqual(self.phone.sent, [])

    def test_the_time_machine_or_risk_can_stop_it(self):
        for mc, verdict, word in ((BAD_MC, "PASS", "Time Machine"), (GOOD_MC, "BLOCK", "BLOCKED")):
            with self.subTest(word):
                self.write("state/autopilot.json", {})
                self.pipe.mc, self.pipe.verdict = mc, verdict
                self.router.add("GET", "/equity/positions", FakeResp([]))
                self.assertIn(word, self.pilot().cycle())
                self.assertEqual(self.phone.sent, [])

    def test_no_new_scan_while_a_ticket_waits_or_when_paused(self):
        p, t = self.ticket()
        n = len(self.pipe.calls)
        self.assertIn("waiting for your answer", p.cycle())
        t["status"] = "rejected"
        p.command("/pause")
        self.assertIn("paused", p.cycle())
        self.assertEqual(len(self.pipe.calls), n)

    def test_a_symbol_is_tried_once_a_day(self):
        p, t = self.ticket()
        t["status"] = "rejected"
        self.router.add("GET", "/equity/positions", FakeResp([]))
        self.assertIn("not tried today", p.cycle())


class Approve(PilotBase):
    def test_approve_checks_again_then_places_on_demo(self):
        p, t = self.ticket()
        self.account()
        self.tap(p, t)
        posts = self.router.posts()
        self.assertEqual([x[1] for x in posts], ["/equity/orders/market", "/equity/orders/stop"])
        self.assertTrue(all(c[4].startswith("https://demo.trading212.com/") for c in self.router.calls))
        self.assertEqual(t["status"], "placed")
        self.assertIn("PLACED on demo", self.phone.edits[-1][1])
        rec = [r for r in self.broker_log() if r.get("status") == "placed"][0]
        self.assertTrue(rec["reply"].startswith("phone approve %s" % t["id"]))
        steps = [c[0].rsplit("/", 1)[1] for c in self.pipe.calls]
        self.assertEqual(steps[-2:], ["alerts.py", "check.py"])     # a fresh quote and risk, after the tap

    def test_reject_places_nothing(self):
        p, t = self.ticket()
        self.tap(p, t, "rj")
        self.assertEqual(t["status"], "rejected")
        self.assertEqual(self.router.posts(), [])

    def test_a_tap_after_ten_minutes_places_nothing(self):
        p, t = self.ticket()
        self.clock[0] += 601
        self.tap(p, t)
        self.assertEqual(t["status"], "expired")
        self.assertEqual(self.router.posts(), [])

    def test_expire_marks_it_and_says_so_on_the_phone(self):
        p, t = self.ticket()
        self.clock[0] += 600
        p.expire()
        self.assertEqual(t["status"], "expired")
        self.assertIn("EXPIRED", self.phone.edits[-1][1])

    def test_a_second_tap_never_places_twice(self):
        p, t = self.ticket()
        self.account()
        self.tap(p, t)
        n = len(self.router.posts())
        self.tap(p, t)
        self.assertEqual(len(self.router.posts()), n)
        self.assertIn("Already placed", self.phone.answers[-1])

    def test_a_forged_tap_does_nothing(self):
        p, t = self.ticket()
        self.tap(p, t, nonce="0" * 16)
        self.assertEqual(t["status"], "pending")
        self.assertEqual(self.router.posts(), [])
        self.assertIn("Unknown ticket", self.phone.answers[-1])

    def test_a_price_that_ran_away_is_not_chased(self):
        p, t = self.ticket()
        self.pipe.quote = 368.00                      # entry 364.18 + 0.5 x 5.86 = 367.11
        self.tap(p, t)
        self.assertEqual(t["status"], "refused")
        self.assertIn("too late to chase", self.phone.edits[-1][1])
        self.assertEqual(self.router.posts(), [])

    def test_risk_blocking_after_the_tap_places_nothing(self):
        p, t = self.ticket()
        self.pipe.verdict = "BLOCK"
        self.tap(p, t)
        self.assertEqual(t["status"], "refused")
        self.assertEqual(self.router.posts(), [])

    def test_a_changed_orders_file_places_nothing(self):
        p, t = self.ticket()
        self.write("orders.json", {"orders": [tsla_ticket(qty="9")]})
        self.tap(p, t)
        self.assertIn("changed", t["note"])
        self.assertEqual(self.router.posts(), [])


class AutoBuy(PilotBase):
    def setUp(self):
        super().setUp()
        self.cfg = dict(self.cfg, autopilot={"auto_buy": True})
        self.write("config.json", self.cfg)

    def cycle(self):
        self.router.add("GET", "/equity/positions", FakeResp([]))
        p = self.pilot()
        self.account()
        return p, p.cycle()

    def test_a_passing_ticket_is_bought_with_no_tap_and_the_phone_is_told(self):
        p, line = self.cycle()
        self.assertIn("TSLA: placed", line)
        self.assertEqual([x[1] for x in self.router.posts()], ["/equity/orders/market", "/equity/orders/stop"])
        self.assertTrue(all(c[4].startswith("https://demo.trading212.com/") for c in self.router.calls))
        text = self.phone.sent[-1][0]
        self.assertRegex(text, r"^BOUGHT \(demo, automatic\): \d+ TSLA\n")
        for want in ("Entry: $", "Cost: about £", "Stop: $358.32",
                     "Target: $370.04", "Max loss at the stop: about £", "Time Machine: average +0.30R a trade before costs (+0.12R after); 60 % of the replays that made "
                     "or lost money made money, 50 % breaks even"):
            self.assertIn(want, text)
        self.assertIsNone(self.phone.sent[-1][1])                  # no buttons
        rec = [r for r in self.broker_log() if r.get("status") == "placed"][0]
        self.assertTrue(rec["reply"].startswith("auto-buy"))

    def test_the_placing_checks_still_run_and_say_why_not(self):
        for setup, why in ((lambda: setattr(self.pipe, "quote", 368.0), "too late to chase"),
                           (lambda: None, "trader-risk no longer passes")):
            with self.subTest(why):
                self.router.routes.clear()
                self.router.calls.clear()
                self.write("state/autopilot.json", {})
                self.pipe.quote = 364.5
                setup()
                if "risk" in why:
                    calls = {"n": 0}
                    real = self.pipe.verdict

                    def flip(args, timeout=None, _p=self.pipe):
                        if args[0].endswith("check.py"):
                            calls["n"] += 1
                            _p.verdict = "PASS" if calls["n"] == 1 else "BLOCK"
                        return Pipeline.__call__(_p, args, timeout)
                    ap._run = flip
                p, line = self.cycle()
                ap._run = self.pipe
                self.pipe.verdict = "PASS"
                self.assertEqual(self.router.posts(), [])
                self.assertIn("NOT PLACED (demo): TSLA passed the checks when built", self.phone.sent[-1][0])
                self.assertIn(why, self.phone.sent[-1][0])

    def test_the_daily_limits_live_in_the_risk_check_it_reruns(self):
        p, _ = self.cycle()
        steps = [c[0].rsplit("/", 1)[1] for c in self.pipe.calls]
        self.assertEqual(steps.count("check.py"), 2)               # when built, and again just before placing

    def test_paused_buys_nothing(self):
        p = self.pilot()
        p.command("/pause")
        self.assertIn("paused", p.cycle())
        self.assertEqual(self.router.posts(), [])

    def test_auto_buy_must_be_true_or_false(self):
        with self.assertRaises(ap.PilotError):
            ap.settings({"autopilot": {"auto_buy": "yes"}})
        self.assertFalse(ap.settings({})["auto_buy"])

    def test_there_is_no_live_automatic_buying(self):
        self.assertTrue(any("no live automatic buying" in x for x in ap.check_ready(self.cfg, "live", self.phone)))
        p = self.pilot()
        p.client = t212.Client("live", "k", "s", state_dir=self.dir, log=lambda m: None)
        with self.assertRaises(ap.PilotError):
            p.auto_buy({"symbol": "TSLA", "fingerprint": "x", "order": tsla_ticket(), "risk": {}, "verdict": ""})
        self.assertEqual(self.router.posts(), [])

    def test_status_says_it_buys_automatically(self):
        p = self.pilot()
        p.command("/status")
        self.assertIn("buys automatically", self.phone.sent[-1][0])


class Watching(PilotBase):
    def test_fills_and_exits_go_to_the_phone(self):
        p = self.pilot()
        self.router.add("GET", "/equity/positions", FakeResp([]))
        p.watch_positions()
        self.assertEqual(self.phone.sent, [])          # the first look is the baseline
        self.router.routes.clear()
        self.router.add("GET", "/equity/positions", FakeResp(HELD7))
        p.watch_positions()
        self.assertIn("FILLED (demo): bought 7 TSLA_US_EQ", self.phone.sent[-1][0])
        self.router.routes.clear()
        self.router.add("GET", "/equity/positions", FakeResp([]))
        p.watch_positions()
        self.assertIn("EXIT (demo): sold 7 TSLA_US_EQ", self.phone.sent[-1][0])

    def test_the_summary_records_the_day_for_the_briefing(self):
        p = self.pilot()
        p.state["start_value"] = 4990.0
        self.router.add("GET", "/equity/account/summary", FakeResp(test_t212_place.SUMMARY))
        self.router.add("GET", "/equity/positions", FakeResp([]))
        self.router.add("GET", "/equity/history/orders", FakeResp({"items": []}))
        p.send_summary()
        self.assertEqual(self.read("state/account-days.json")["days"][TODAY], {"start": 4990.0, "close": 5000.0})

    def test_status_from_the_phone(self):
        p = self.pilot()
        p.command("/status")
        self.assertIn("Autopilot (demo, asks for Approve)", self.phone.sent[-1][0])


class Day(PilotBase):
    def test_a_quiet_day_cycles_every_15_minutes_then_sends_the_summary(self):
        self.clock[0] = NOW - 90 * 60                 # 9:00 New York
        clock = self.clock

        def updates(wait=0):
            clock[0] += wait                          # a phone poll takes its wait
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
        scans = [c for c in self.pipe.calls if c[0].endswith("scan.py")]
        self.assertEqual(len(scans), 24)              # 9:45, 10:00 ... 15:30
        self.assertIn("MARKET OPEN (demo) Thu 05 Nov 09:30 New York", self.phone.sent[0][0])
        self.assertIn("CLOSING REPORT (demo) Thu 05 Nov", self.phone.sent[-1][0])
        self.assertLessEqual(self.clock[0], NOW + (5 * 60 + 26) * 60)   # by about 15:56 New York
        self.assertEqual(self.router.posts(), [])
        self.assertTrue(p.state["summary_sent"])

    def test_a_weekend_does_nothing(self):
        self.clock[0] = NOW + 2 * 86400               # Saturday
        with redirect_stdout(io.StringIO()):
            self.assertEqual(self.pilot().day(), "closed")
        self.assertEqual(self.pipe.calls, [])


class GuardToPhone(PilotBase):
    def test_guard_alerts_go_to_the_phone(self):
        t212.guard_alarm(self.client, "key rejected")
        self.assertIn("GUARD ALERT (DEMO): key rejected", self.pushed[-1])

    def test_guard_news_is_orders_sent_not_quiet_checks(self):
        res = {"positions": [{"ticker": "A", "status": "covered", "held": 1},
                             {"ticker": "B", "status": "stop placed", "placed_qty": 5, "stop": 10.0, "held": 5},
                             {"ticker": "C", "status": "covered", "trail": "RAISED the stop 1.00 -> 2.00 (high 3.00)"},
                             {"ticker": "D", "status": "SOLD AT STOP", "trail": "market SELL 2 at the 9.00 stop"}],
               "warnings": ["w1"]}
        news = t212.guard_news(res)
        self.assertEqual(len(news), 3)
        self.assertIn("B stop placed: SELL 5 at 10.00", news[0])
        self.assertIn("RAISED", news[1])
        self.assertIn("SOLD AT STOP", news[2])


if __name__ == "__main__":
    unittest.main()
