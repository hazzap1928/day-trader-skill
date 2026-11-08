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
            t.write("timemachine.json", {"mc": self.mc, "verdict": "Leans toward the target: test"})
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
        self._ap = (ap._now, ap._sleep, ap._run)
        ap._now = lambda: self.clock[0]
        ap._sleep = lambda s: self.clock.__setitem__(0, self.clock[0] + s)
        self.pipe = Pipeline(self)
        ap._run = self.pipe
        self.phone = FakePhone()
        self.client = t212.Client("demo", CREDS["T212_API_KEY"], CREDS["T212_API_SECRET"], state_dir=self.dir,
                                  log=lambda m: None)
        self.out = []

    def tearDown(self):
        ap._now, ap._sleep, ap._run = self._ap
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

    def test_the_daily_summary(self):
        p, t = self.ticket()
        t["status"] = "rejected"
        p.state["start_value"] = 4990.0
        self.router.add("GET", "/equity/account/summary", FakeResp(test_t212_place.SUMMARY))
        self.router.add("GET", "/equity/positions", FakeResp(HELD7))
        p.send_summary()
        text = self.phone.sent[-1][0]
        self.assertIn("DEMO DAILY SUMMARY", text)
        self.assertIn("£5,000.00 (start of day £4,990.00, +£10.00)", text)
        self.assertIn("Tickets: 1 sent, 1 rejected", text)
        self.assertIn("TSLA_US_EQ 7", text)

    def test_status_from_the_phone(self):
        p = self.pilot()
        p.command("/status")
        self.assertIn("Autopilot (demo)", self.phone.sent[-1][0])


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
        p = self.pilot()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(p.day(), "done")
        scans = [c for c in self.pipe.calls if c[0].endswith("scan.py")]
        self.assertEqual(len(scans), 24)              # 9:45, 10:00 ... 15:30
        self.assertIn("Autopilot started (demo)", self.phone.sent[0][0])
        self.assertIn("DEMO DAILY SUMMARY", self.phone.sent[-1][0])
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
