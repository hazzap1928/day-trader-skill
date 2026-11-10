"""Always on: the phone gets an answer at any hour, the morning briefing gets through, and the open is
confirmed with the guard, the autopilot, positions with their stops and evidence mode. Fakes only."""

import calendar
import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

import test_t212_place
from test_t212_place import FakeResp, NOW, HELD7
from test_autopilot import PilotBase, FakePhone, ap
from test_briefing import br

t212 = test_t212_place.t212


def utc(y, mo, d, h, mi):
    return float(calendar.timegm((y, mo, d, h, mi, 0)))


SATURDAY = utc(2026, 11, 7, 12, 0)
THURSDAY_0800_UK = utc(2026, 11, 5, 8, 0)          # 03:00 New York
STOP = {"id": 7001, "ticker": "TSLA_US_EQ", "type": "STOP", "quantity": -7, "stopPrice": 358.32, "status": "NEW"}


class Base(PilotBase):
    def setUp(self):
        super().setUp()
        self._local = ap._local
        ap._local = lambda ts: datetime.datetime.utcfromtimestamp(ts)      # UK in November: UTC
        self.inbox = []
        clock = self.clock

        def updates(wait=0):
            clock[0] += max(1, wait)
            out, self.inbox[:] = list(self.inbox), []
            return out
        self.phone.updates = updates

    def tearDown(self):
        ap._local = self._local
        super().tearDown()

    def say(self, text):
        self.inbox.append({"kind": "text", "text": text})


class AnyHour(Base):
    def test_status_on_a_saturday(self):
        self.clock[0] = SATURDAY
        p = self.pilot()
        self.say("/status")
        with redirect_stdout(io.StringIO()):
            p.serve(max_loops=2)
        text = self.phone.sent[-1][0]
        self.assertIn("Autopilot (demo", text)
        self.assertIn("Market: closed (weekend); next open 2026-11-09 09:30 New York", text)
        self.assertIn("Guard: running (pid 4242)", text)

    def test_pause_and_resume_before_the_open(self):
        self.clock[0] = THURSDAY_0800_UK
        p = self.pilot()
        self.say("/pause")
        with redirect_stdout(io.StringIO()):
            p.listen(20)
        self.assertTrue(p.state["paused"])
        self.assertIn("Paused", self.phone.sent[-1][0])
        self.say("/resume")
        p.listen(20)
        self.assertFalse(p.state["paused"])

    def test_the_wait_for_the_open_answers_the_phone(self):
        self.clock[0] = utc(2026, 11, 5, 14, 20)          # 09:20 New York
        self.pipe.triggered = ()
        self.say("/status")
        self.router.add("GET", "/equity/positions", FakeResp([]))
        self.router.add("GET", "/equity/orders", FakeResp([]))
        self.router.add("GET", "/equity/account/summary", FakeResp(test_t212_place.SUMMARY))
        self.router.add("GET", "/equity/history/orders", FakeResp({"items": []}))
        p = self.pilot()
        with redirect_stdout(io.StringIO()):
            p.day()
        self.assertTrue(self.phone.sent[0][0].startswith("Autopilot (demo"))       # answered at 09:20
        self.assertTrue(any(t.startswith("MARKET OPEN") for t, _ in self.phone.sent))

    def test_a_new_day_starts_fresh(self):
        p = self.pilot()
        p.state["tickets"].append({"status": "placed"})
        self.clock[0] += 86400
        p.roll_day()
        self.assertEqual(p.state["tickets"], [])
        self.assertEqual(p.state["day"], "2026-11-06")

    def test_the_outbox_goes_out_when_the_phone_answers(self):
        ap.phone_mod.queue(self.dir, "GUARD ALERT: waited")
        p = self.pilot()
        p.flush_phone()
        self.assertIn("DELIVERED LATE: 1 message", self.phone.sent[-1][0])

    def test_not_ready_still_answers_with_the_reason_and_says_so_once_a_day(self):
        self.say("/status")
        with redirect_stderr(io.StringIO()):
            code = ap.serve_unready(self.dir, self.client, self.phone, ["no allowed_tickers"], {}, max_loops=1)
        self.assertEqual(code, 3)
        texts = [t for t, _ in self.phone.sent]
        self.assertTrue(texts[0].startswith("Autopilot NOT RUNNING (demo):"))
        self.assertIn("no allowed_tickers", texts[1])
        with redirect_stderr(io.StringIO()):
            ap.serve_unready(self.dir, self.client, self.phone, ["no allowed_tickers"], {}, max_loops=1)
        self.assertEqual(len(self.phone.sent), 2)            # no second push the same day


class OpenMessage(Base):
    def accounts(self, positions, orders=()):
        self.router.add("GET", "/equity/positions", FakeResp(list(positions)))
        self.router.add("GET", "/equity/orders", FakeResp(list(orders)))

    def test_guard_autopilot_positions_and_their_stops(self):
        self.accounts(HELD7, [STOP])
        text = self.pilot().open_message()
        self.assertIn("MARKET OPEN (demo) Thu 05 Nov", text)
        self.assertIn("Guard: running (pid 4242)", text)
        self.assertIn("Autopilot: running, always on (pid 4343)", text)
        self.assertIn("TSLA_US_EQ 7 at $366.00: stop $358.32 (stop order, NEW)", text)

    def test_a_stopped_autopilot_service_is_loud(self):
        self.accounts([])
        self.jobs["com.daytraderskill.autopilot"] = {"loaded": True, "running": False, "pid": None, "out": ""}
        self.assertIn("the always-on service is NOT RUNNING", self.pilot().open_message())

    def test_a_missing_stop_and_a_software_stop_are_called_out(self):
        self.accounts(HELD7)
        self.assertIn("!!! NO STOP !!!", self.pilot().open_message())
        self.write("state/t212-guard.json", {"software": {"TSLA_US_EQ": {"stop": 358.32}}})
        self.assertIn("software stop $358.32", self.pilot().open_message())

    def test_a_stopped_guard_is_loud(self):
        self.accounts([])
        self.jobs["com.daytraderskill.t212-guard"] = {"loaded": True, "running": False, "pid": None, "out": ""}
        self.assertIn("Guard: !!! NOT RUNNING (loaded, stopped)", self.pilot().open_message())

    def test_evidence_mode_and_the_day_of_the_test(self):
        self.write("config.json", dict(self.cfg, autopilot={"auto_buy": True, "evidence_mode": True},
                                       evidence={"start": "2026-11-02"}))
        self.accounts([])
        text = self.pilot().open_message()
        self.assertIn("EVIDENCE MODE is ON: forward test since 2026-11-02, no end date, trading day 4", text)

    def test_the_guards_last_check_comes_from_its_log(self):
        os.makedirs(os.path.join(self.dir, "logs"), exist_ok=True)
        with open(os.path.join(self.dir, "logs", "guard.log"), "w") as fh:
            fh.write("2026-11-05T09:35:31-05:00  stop guard (broker stops) on demo: 0 open positions\n")
        self.accounts([])
        self.assertIn("last check 2026-11-05 09:35 New York", self.pilot().open_message())

    def test_sent_once_at_the_open(self):
        self.clock[0] = utc(2026, 11, 5, 14, 30)
        self.pipe.triggered = ()
        self.accounts([])
        self.router.add("GET", "/equity/account/summary", FakeResp(test_t212_place.SUMMARY))
        self.router.add("GET", "/equity/history/orders", FakeResp({"items": []}))
        p = self.pilot()
        with redirect_stdout(io.StringIO()):
            p.day()
            p.state["summary_sent"] = False
            p.day()
        self.assertEqual(sum(1 for t, _ in self.phone.sent if t.startswith("MARKET OPEN")), 1)


class BriefingCatchUp(Base):
    def setUp(self):
        super().setUp()
        self.clock[0] = THURSDAY_0800_UK

    def test_a_briefing_that_never_started_is_run_once(self):
        p = self.pilot()
        p.catch_up_briefing()
        p.catch_up_briefing()
        self.assertEqual(len(self.spawned), 1)
        self.assertTrue(self.spawned[0][1].endswith("trader-broker/briefing.py"))

    def test_sent_or_still_trying_means_leave_it(self):
        self.write("state/briefing.json", {"started": "2026-11-05", "started_at": self.clock[0] - 600})
        self.pilot().catch_up_briefing()
        self.write("state/briefing.json", {"sent": "2026-11-05"})
        self.pilot().catch_up_briefing()
        self.assertEqual(self.spawned, [])

    def test_a_briefing_stuck_for_40_minutes_is_run_again(self):
        self.write("state/briefing.json", {"started": "2026-11-05", "started_at": self.clock[0] - 41 * 60})
        self.pilot().catch_up_briefing()
        self.assertEqual(len(self.spawned), 1)

    def test_not_before_0745_not_after_the_open_not_at_weekends(self):
        for when in (utc(2026, 11, 5, 7, 40), utc(2026, 11, 5, 14, 31), SATURDAY):
            with self.subTest(when):
                self.clock[0] = when
                self.write("state/autopilot.json", {})
                self.pilot().catch_up_briefing()
        self.assertEqual(self.spawned, [])


class BriefingSend(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.sleeps = []
        self._sleep = br._sleep
        br._sleep = lambda s: self.sleeps.append(s)
        self.phone = FakePhone()

    def tearDown(self):
        br._sleep = self._sleep
        shutil.rmtree(self.dir)

    def test_it_tries_again_every_two_minutes(self):
        fails = [1, 1, 1]
        sent = []

        def send(text, buttons=None):
            if fails:
                fails.pop()
                raise OSError("no route")
            sent.append(text)
        self.phone.send = send
        with redirect_stderr(io.StringIO()):
            self.assertTrue(br.send_with_retry(self.dir, "MORNING BRIEFING", phone=self.phone))
        self.assertEqual(self.sleeps, [120, 120, 120])
        self.assertEqual(sent, ["MORNING BRIEFING"])

    def test_after_thirty_minutes_it_waits_in_the_outbox_once(self):
        def send(text, buttons=None):
            raise OSError("no route")
        self.phone.send = send
        with redirect_stderr(io.StringIO()):
            self.assertFalse(br.send_with_retry(self.dir, "MORNING BRIEFING", phone=self.phone))
        self.assertEqual(len(self.sleeps), br.SEND_TRIES - 1)
        self.assertEqual(sum(self.sleeps), 30 * 60)
        with open(br.phone_mod.outbox_path(self.dir)) as fh:
            self.assertEqual([i["text"] for i in json.load(fh)["items"]], ["MORNING BRIEFING"])


if __name__ == "__main__":
    unittest.main()
