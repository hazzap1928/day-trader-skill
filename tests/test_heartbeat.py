"""The heartbeat to an outside check service (trader-broker/heartbeat.py, 21 Nov 2026).

Offline: the ping is faked. The address is private, so it must never be printed or logged, and only the
launchd autopilot (run-autopilot.sh) may ping it, never a test run or a shell.
"""

import io
import os
import unittest
from contextlib import redirect_stdout

from _load import load, ROOT
import test_autopilot

hb = load("trader-broker", "heartbeat")
ap = test_autopilot.ap
URL = "https://hc-ping.com/00000000-test-only-0000"


class Clock(object):
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class Pings(unittest.TestCase):
    def setUp(self):
        self._open = hb._open
        self.pings = []
        self.reply = 200
        hb._open = self.fake
        ap.heartbeat._open = self.fake          # the autopilot loads its own copy

    def tearDown(self):
        hb._open = self._open
        ap.heartbeat._open = self._open

    def fake(self, url, timeout):
        self.pings.append(url)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply

    def test_the_address(self):
        self.assertIsNone(hb.url_from({}))
        self.assertIsNone(hb.url_from({"HEALTHCHECK_URL": "http://insecure.example/x"}))
        self.assertEqual(hb.url_from({"HEALTHCHECK_URL": " %s \n" % URL}), URL)
        self.assertIsNone(hb.url_from({"HEALTHCHECK_URL": URL}, hb.SERVE_ENV))   # serve reads its own name
        self.assertEqual(hb.url_from({hb.SERVE_ENV: URL}, hb.SERVE_ENV), URL)

    def test_no_address_sends_nothing(self):
        h = hb.Heartbeat(None)
        self.assertIsNone(h.beat())
        self.assertEqual(self.pings, [])

    def test_once_every_five_minutes(self):
        c = Clock()
        h = hb.Heartbeat(URL, clock=c)
        self.assertEqual(h.beat(), "sent")
        c.t += 60
        self.assertIsNone(h.beat())
        c.t += 240
        self.assertEqual(h.beat(), "sent")
        self.assertEqual(self.pings, [URL, URL])

    def test_a_failure_never_raises_and_is_tried_again_later(self):
        c = Clock()
        h = hb.Heartbeat(URL, clock=c)
        self.reply = OSError("no route to host %s" % URL)
        self.assertEqual(h.beat(), "failed")
        self.assertIsNone(h.beat())                     # not hammered
        c.t += 300
        self.reply = 500
        self.assertEqual(h.beat(), "failed")
        self.assertEqual(h.fails, 2)
        c.t += 300
        self.reply = 200
        self.assertEqual(h.beat(), "sent")
        self.assertEqual(h.fails, 0)

    def test_by_hand_never_prints_the_address(self):
        out = io.StringIO()
        old = os.environ.get("HEALTHCHECK_URL")
        os.environ["HEALTHCHECK_URL"] = URL
        try:
            with redirect_stdout(out):
                self.assertEqual(hb.main(["ping"]), 0)
        finally:
            if old is None:
                del os.environ["HEALTHCHECK_URL"]
            else:
                os.environ["HEALTHCHECK_URL"] = old
        self.assertEqual(out.getvalue().strip(), "Heartbeat sent.")
        self.assertNotIn("hc-ping", out.getvalue())


class InTheAutopilot(test_autopilot.PilotBase):
    def setUp(self):
        super().setUp()
        self._open = ap.heartbeat._open
        self.pings, self.reply = [], 200

        def fake(url, timeout):
            self.pings.append(url)
            if isinstance(self.reply, Exception):
                raise self.reply
            return self.reply
        ap.heartbeat._open = fake

    def tearDown(self):
        ap.heartbeat._open = self._open
        super().tearDown()

    def test_a_pilot_never_pings_unless_serve_gives_it_the_address(self):
        old = os.environ.get("HEALTHCHECK_URL")
        os.environ["HEALTHCHECK_URL"] = URL         # as in a terminal that has read ~/.zshrc
        try:
            p = self.pilot()
            p.listen(0)
        finally:
            if old is None:
                del os.environ["HEALTHCHECK_URL"]
            else:
                os.environ["HEALTHCHECK_URL"] = old
        self.assertIsNone(p.hb.url)
        self.assertEqual(self.pings, [])

    def test_each_wait_for_the_phone_beats_and_the_log_never_has_the_address(self):
        p = self.pilot()
        p.hb = ap.heartbeat.Heartbeat(URL, clock=lambda: self.clock[0])
        p.listen(0)
        p.listen(0)                                 # under 5 minutes later: no second ping
        self.assertEqual(self.pings, [URL])
        self.clock[0] += 300
        self.reply = OSError("down")
        p.listen(0)
        self.clock[0] += 300
        p.listen(0)                                 # a second failure is not logged again
        self.clock[0] += 300
        self.reply = 200
        p.listen(0)
        log = "\n".join(self.out)
        self.assertEqual(log.count("ping to the outside check failed"), 1)
        self.assertIn("the outside check heard from this Mac again", log)
        self.assertNotIn("hc-ping", log)
        self.assertNotIn(URL, log)


class TheScript(unittest.TestCase):
    def test_only_the_launchd_autopilot_gets_the_address(self):
        with open(os.path.join(ROOT, "trader-broker", "run-autopilot.sh")) as f:
            src = f.read()
        self.assertIn("export[[:space:]]+HEALTHCHECK_URL=", src)
        self.assertIn('export AUTOPILOT_HEARTBEAT_URL="${HEALTHCHECK_URL:-}"; unset HEALTHCHECK_URL', src)
        with open(os.path.join(ROOT, "trader-broker", "autopilot.py")) as f:
            main = f.read()
        self.assertIn("heartbeat.url_from(environ, heartbeat.SERVE_ENV)", main)


if __name__ == "__main__":
    unittest.main()
