"""No internet: the briefing, guard and autopilot check every 2 minutes for up to 30, then tell the
phone what was missed once it is back. Plus the phone outbox and the keep-awake check.
The network, Trading 212, Telegram and launchd are all fakes."""

import calendar
import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr

from _load import load
import test_t212_place
from test_t212_place import Base, FakeResp, CREDS
import test_autopilot
from test_autopilot import PilotBase, FakePhone, ap
import test_briefing
from test_briefing import BriefBase, br
from test_t212_guard import position, stop_order, entry_record, TICKER

t212 = test_t212_place.t212
net = t212.net_module()


def ny(h, m):
    return float(calendar.timegm((2026, 11, 5, h + 5, m, 0)))    # New York is UTC-5 in November


class BackAt(object):
    """The internet comes back at a set time on the test clock."""

    def __init__(self, test, at):
        self.t, self.at = test, at

    def __call__(self, host, port, timeout):
        if self.t.clock[0] < self.at:
            raise OSError("offline (test)")


class WaitOnline(unittest.TestCase):
    def setUp(self):
        self.clock = [0.0]
        self.sleeps = []

    def sleep(self, s):
        self.sleeps.append(s)
        self.clock[0] += s

    def wait(self, back_at, **kw):
        return net.wait_online(self.sleep, lambda: self.clock[0], probe=lambda: self.clock[0] >= back_at, **kw)

    def test_every_two_minutes_until_it_is_back(self):
        w = self.wait(300)
        self.assertTrue(w["ok"])
        self.assertEqual(self.sleeps, [120, 120, 120])
        self.assertEqual(w["end"] - w["start"], 360)

    def test_it_gives_up_after_thirty_minutes(self):
        w = self.wait(10 ** 9)
        self.assertFalse(w["ok"])
        self.assertTrue(w["gave_up"])
        self.assertEqual(sum(self.sleeps), 30 * 60)
        self.assertTrue(all(s == 120 for s in self.sleeps))

    def test_a_deadline_stops_it_sooner(self):
        w = self.wait(10 ** 9, deadline=500)
        self.assertFalse(w["ok"])
        self.assertFalse(w["gave_up"])
        self.assertLessEqual(self.clock[0], 500)

    def test_the_message_says_what_was_missed(self):
        text = net.back_online("The guard", 0, 600, ["2 guard checks"])
        self.assertIn("(10 min)", text)
        self.assertIn("Missed: 2 guard checks.", text)
        self.assertIn("Nothing was missed.", net.back_online("X", 0, 60, []))


class Outbox(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.phone_mod = t212.phone_module()

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_a_failed_message_goes_out_with_the_next_one(self):
        p = FakePhone()
        sent = []
        p.send = lambda text, buttons=None: (_ for _ in ()).throw(OSError("offline"))
        with redirect_stderr(io.StringIO()):
            self.assertFalse(self.phone_mod.push(self.dir, "GUARD ALERT: x", phone=p))
        p.send = lambda text, buttons=None: sent.append(text)
        self.assertTrue(self.phone_mod.push(self.dir, "BACK ONLINE", phone=p))
        self.assertTrue(sent[0].startswith("DELIVERED LATE: 1 message"))
        self.assertIn("GUARD ALERT: x", sent[0])
        self.assertEqual(sent[1], "BACK ONLINE")
        self.assertEqual(self.phone_mod.flush_outbox(p, self.dir), 0)

    def test_the_outbox_keeps_the_newest_fifty(self):
        for i in range(55):
            self.phone_mod.queue(self.dir, "m%d" % i)
        with open(self.phone_mod.outbox_path(self.dir)) as fh:
            doc = json.load(fh)
        self.assertEqual(len(doc["items"]), 50)
        self.assertEqual(doc["items"][-1]["text"], "m54")


class Guard(Base):
    def setUp(self):
        super().setUp()
        self.write("state/broker.json", {"orders": [entry_record(status="stop_placed")]})

    def watch(self, until):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = t212.main(["guard", "--watch", "--every", "5", "--until", until, "--state-dir", self.dir],
                             environ=CREDS)
        return code, out.getvalue()

    def drop_then(self, back_at):
        self.net._connect = BackAt(self, back_at)
        r = self.router
        r.add("GET", "/equity/positions", urllib.error.URLError("no route to host"), FakeResp([position()]))
        r.add("GET", "/equity/orders", FakeResp([stop_order()]))

    def test_a_short_drop_is_retried_and_reported_once_back(self):
        self.clock[0] = ny(10, 0)
        self.drop_then(ny(10, 6))
        code, text = self.watch("10:20")
        self.assertEqual(code, 0)
        self.assertIn(120, self.sleeps)
        back = [p for p in self.pushed if p.startswith("BACK ONLINE")]
        self.assertEqual(len(back), 1)
        self.assertIn("The guard (demo) had no internet", back[0])
        self.assertIn("Missed: 1 guard check", back[0])
        self.assertEqual(self.router.posts(), [])
        with open(os.path.join(self.dir, "logs", "outages.jsonl")) as fh:
            self.assertEqual(json.loads(fh.readline())["what"], "guard")

    def test_after_thirty_minutes_it_alarms_and_keeps_trying(self):
        self.clock[0] = ny(10, 0)
        self.drop_then(ny(10, 40))
        code, text = self.watch("11:00")
        self.assertTrue(any("GUARD OFFLINE for 30 minutes" in n[1] for n in self.notes))   # the Mac, offline too
        self.assertTrue(any(p.startswith("BACK ONLINE") for p in self.pushed))
        self.assertIn("It is checking your positions again now.", [p for p in self.pushed if p.startswith("BACK")][0])

    def test_a_missed_end_of_day_close_is_named_and_then_run(self):
        self.write("config.json", dict(self.cfg, eod_close={"enabled": True, "at": "15:50"}))
        self.clock[0] = ny(15, 45)
        self.net._connect = BackAt(self, ny(15, 53))
        r = self.router
        r.add("GET", "/equity/positions", urllib.error.URLError("down"), FakeResp([]))
        r.add("GET", "/equity/orders", FakeResp([]))
        code, text = self.watch("16:00")
        back = [p for p in self.pushed if p.startswith("BACK ONLINE")][0]
        self.assertIn("the 15:50 end-of-day close, which runs now that it is back", back)
        self.assertIn("END-OF-DAY CLOSE", text)

    def test_trading_212_down_but_the_internet_up_is_a_normal_failure(self):
        self.clock[0] = ny(10, 0)
        r = self.router
        r.add("GET", "/equity/positions", urllib.error.URLError("t212 down"), FakeResp([position()]))
        r.add("GET", "/equity/orders", FakeResp([stop_order()]))
        code, text = self.watch("10:12")
        self.assertIn("guard pass failed (1 in a row)", text)
        self.assertFalse(any(p.startswith("BACK ONLINE") for p in self.pushed))


class Autopilot(PilotBase):
    def setUp(self):
        super().setUp()
        self.clock[0] = ny(10, 0)

    def go_offline(self, back_after_min):
        self.net._connect = BackAt(self, self.clock[0] + back_after_min * 60)
        p = self.pilot()
        now = ap._ny()
        nc = p.offline(now, ap.datetime.timedelta(minutes=15), now + ap.datetime.timedelta(hours=5),
                       now + ap.datetime.timedelta(hours=6))
        return p, now, nc

    def test_back_within_thirty_minutes_says_which_cycles_were_missed(self):
        p, start, nc = self.go_offline(20)
        self.assertEqual(nc, start + ap.datetime.timedelta(minutes=30))
        back = self.phone.sent[-1][0]
        self.assertIn("BACK ONLINE: The autopilot (demo) had no internet", back)
        self.assertIn("2 scan cycles (10:00, 10:15 New York)", back)
        self.assertFalse(p.state.get("paused"))
        self.assertEqual(self.router.posts(), [])

    def test_thirty_minutes_or_more_pauses_buying_for_the_day(self):
        p, start, nc = self.go_offline(44)
        texts = [s[0] for s in self.phone.sent]
        self.assertTrue(any(t.startswith("AUTOPILOT OFFLINE for 30 minutes: buying is PAUSED") for t in texts))
        self.assertIn("Buying is PAUSED for the rest of today", texts[-1])
        self.assertTrue(p.state["paused"])
        self.assertIn("paused", p.cycle())

    def test_a_failed_phone_poll_with_no_internet_marks_it_offline(self):
        p = self.pilot()
        self.net._connect = BackAt(self, 10 ** 12)

        def down(wait=0):
            raise ap.phone_mod.PhoneError("Telegram getUpdates failed: offline")
        self.phone.updates = down
        p.listen(20)
        self.assertTrue(p.net_down)

    def test_the_keep_awake_job_is_part_of_the_readiness_checks(self):
        ap._keep_awake = lambda: (False, "the com.daytraderskill.caffeinate launchd job is not loaded")
        problems = ap.check_ready(self.cfg, "demo", self.phone)
        self.assertTrue(any("the Mac may sleep" in x and "launchctl bootstrap" in x for x in problems), problems)


class KeepAwakeCheck(unittest.TestCase):
    """The real _keep_awake and _on_mains, against faked launchctl and pmset output."""

    def setUp(self):
        self.mod = load("trader-broker", "autopilot")       # a fresh copy whose checks are not patched
        self.real = self.mod.subprocess.run

    def tearDown(self):
        self.mod.subprocess.run = self.real

    def fake(self, code, out):
        class Done(object):
            returncode, stdout = code, out
        self.mod.subprocess.run = lambda *a, **k: Done()

    def test_running(self):
        self.fake(0, "\tstate = running\n\tpid = 47023\n\tprogram = /usr/bin/caffeinate\n\t\t-s\n")
        self.assertEqual(self.mod._keep_awake(), (True, "caffeinate -s is running (pid 47023)"))

    def test_not_loaded_or_not_running(self):
        self.fake(113, "")
        self.assertFalse(self.mod._keep_awake()[0])
        self.fake(0, "\tstate = not running\n\tprogram = /usr/bin/caffeinate\n\t\t-s\n")
        self.assertIn("not running", self.mod._keep_awake()[1])

    def test_battery_is_a_warning(self):
        self.fake(0, "Now drawing from 'Battery Power'\n")
        self.assertFalse(self.mod._on_mains()[0])
        self.fake(0, "Now drawing from 'AC Power'\n")
        self.assertTrue(self.mod._on_mains()[0])


class Briefing(BriefBase):
    def run_main(self):
        sent = []
        saved = br.phone_mod.push
        br.phone_mod.push = lambda sd, text, **kw: sent.append(text) or True
        self._sleep = br._sleep
        br._sleep = lambda s: self.clock.__setitem__(0, self.clock[0] + s)
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = br.main(["--state-dir", self.dir], environ=CREDS)
        finally:
            br.phone_mod.push = saved
            br._sleep = self._sleep
        return code, sent

    def test_late_briefing_says_so_first(self):
        self.net._connect = BackAt(self, self.clock[0] + 5 * 60)
        self.all_data()
        self.trading212()
        code, sent = self.run_main()
        self.assertEqual(code, 0)
        self.assertTrue(sent[0].startswith("BACK ONLINE: The morning briefing had no internet from"))
        self.assertIn("the briefing ran 6 minutes late", sent[0])
        self.assertTrue(sent[1].startswith("MORNING BRIEFING"))

    def test_no_internet_for_thirty_minutes_gives_up_and_leaves_a_note(self):
        self.net._connect = BackAt(self, 10 ** 12)
        code, sent = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(sent, [])
        self.assertEqual(self.web.urls, [])                 # nothing fetched, nothing guessed
        with open(br.phone_mod.outbox_path(self.dir)) as fh:
            self.assertIn("MORNING BRIEFING MISSED", json.load(fh)["items"][0]["text"])


if __name__ == "__main__":
    unittest.main()
