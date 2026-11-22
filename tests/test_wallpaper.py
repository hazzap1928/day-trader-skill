"""The desktop wallpaper's status (trader-broker/wallpaper.py, 22 Nov 2026).

Offline: the POST is faked and the background thread runs at once. The key must never be printed or
logged, only the launchd autopilot may send (a terminal or a test never does), and a failed or slow send
never stops anything.
"""

import os
import threading
import time
import unittest

from _load import load, ROOT
import test_autopilot
import test_t212_place
from test_t212_place import FakeResp

wp = load("trader-broker", "wallpaper")
ap = test_autopilot.ap
URL = "https://example.supabase.co/functions/v1/wallpaper-status"
KEY = "test-only-wallpaper-key-0000"


class NowThread(object):
    """Stands in for threading.Thread: runs the send at once, so the tests see it."""
    def __init__(self, target=None, name=None, daemon=None):
        self.target = target

    def start(self):
        self.target()


class Fakes(object):
    def fake_setup(self, mods):
        self.posts, self.reply = [], 200
        self._wp_saved = [(m, m._post, m.threading.Thread) for m in mods]
        for m in mods:
            m._post = self.fake
            m.threading = type("T", (), {"Thread": NowThread, "Lock": threading.Lock})

    def fake_teardown(self):
        for m, post, _ in self._wp_saved:
            m._post = post
            m.threading = threading

    def fake(self, url, key, body, timeout):
        self.posts.append({"url": url, "key": key, "body": body, "timeout": timeout})
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


class Reporter(Fakes, unittest.TestCase):
    def setUp(self):
        self.fake_setup([wp])
        self.dir = os.path.join(os.environ.get("TMPDIR", "/tmp"), "wp-test-%d" % os.getpid())
        os.makedirs(self.dir, exist_ok=True)
        self.path = os.path.join(self.dir, "wallpaper.json")
        if os.path.exists(self.path):
            os.remove(self.path)
        self.log = []

    def tearDown(self):
        self.fake_teardown()

    def test_address_needs_both_and_https(self):
        self.assertEqual(wp.address({}), (None, None))
        self.assertEqual(wp.address({wp.SERVE_URL_ENV: URL}), (None, None))
        self.assertEqual(wp.address({wp.SERVE_URL_ENV: "http://x", wp.SERVE_KEY_ENV: KEY}), (None, None))
        self.assertEqual(wp.address({wp.URL_ENV: URL, wp.KEY_ENV: KEY}), (None, None))   # serve's own names
        self.assertEqual(wp.address({wp.SERVE_URL_ENV: URL, wp.SERVE_KEY_ENV: " %s\n" % KEY}), (URL, KEY))

    def test_no_address_sends_nothing(self):
        r = wp.Reporter(None, None)
        self.assertIsNone(r.send({"value": 1}))
        self.assertIsNone(r.stopped())
        self.assertEqual(self.posts, [])

    def test_the_body_and_the_trend(self):
        r = wp.Reporter(URL, KEY, self.path, log=self.log.append)
        for v in range(35):
            r.send({"value": 1000.0 + v})
        last = self.posts[-1]
        self.assertEqual(last["key"], KEY)
        self.assertEqual(last["body"]["project"], "trading")
        d = last["body"]["data"]
        self.assertEqual((d["status"], d["mode"]), ("running", "Practice"))
        self.assertEqual(len(d["trend"]), 30)
        self.assertEqual(d["trend"][0], 1005.0)
        self.assertEqual(d["trend"][-1], 1034.0)

    def test_account_fields(self):
        f = wp.account_fields({"totalValue": 10124.2, "cash": {"availableToTrade": 1300, "reservedForOrders": 117.39}},
                              [{"quantity": 2}, {"quantity": 0}, {"quantity": 0.5}], 10000.0)
        self.assertEqual(f, {"value": 10124.2, "day_change": 124.2, "day_change_pct": 1.24, "cash_pct": 14,
                             "positions": 2})
        self.assertEqual(wp.account_fields({}, [], None), {})
        self.assertEqual(wp.account_fields({"totalValue": 50}, [], None)["day_change"], 0.0)

    def test_next_items(self):
        self.assertEqual(wp.next_items({}), wp.NEXT)
        self.assertEqual(wp.next_items({"wallpaper": {"next": ["a", {"t": "b", "issue": True}, {"t": ""}, "c", "d"]}}),
                         [{"t": "a"}, {"t": "b", "issue": True}, {"t": "c"}])

    def test_every_fifteen_minutes(self):
        clock = [1000.0]
        r = wp.Reporter(URL, KEY, clock=lambda: clock[0])
        self.assertTrue(r.due())
        r.send({})
        clock[0] += 600
        self.assertFalse(r.due())
        clock[0] += 300
        self.assertTrue(r.due())

    def test_a_failure_never_raises_is_logged_once_and_never_shows_the_key(self):
        r = wp.Reporter(URL, KEY, log=self.log.append)
        self.reply = OSError("refused %s %s" % (URL, KEY))
        self.assertEqual(r.send({}), "started")
        self.assertEqual(r.send({}), "started")
        self.reply = 500
        r.send({})
        self.reply = 200
        r.send({})
        text = "\n".join(self.log)
        self.assertEqual(text.count("send failed"), 1)
        self.assertIn("status sent again", text)
        self.assertNotIn(KEY, text)
        self.assertNotIn("supabase", text)

    def test_stopped_waits_briefly_and_says_stopped(self):
        r = wp.Reporter(URL, KEY)
        self.assertEqual(r.stopped(), "sent")
        self.assertEqual(self.posts[-1]["body"]["data"]["status"], "stopped")
        self.assertLessEqual(self.posts[-1]["timeout"], wp.STOP_TIMEOUT)

    def test_a_slow_send_never_holds_up_the_caller(self):
        self.fake_teardown()                        # the real thread this time, with a slow fake POST
        gate = threading.Event()
        wp._post = lambda url, key, body, timeout: (gate.wait(5), 200)[1]
        try:
            r = wp.Reporter(URL, KEY)
            t0 = time.time()
            self.assertEqual(r.send({}), "started")
            self.assertEqual(r.send({}), "busy")        # one in flight: the next is dropped, not queued
            self.assertLess(time.time() - t0, 1.0)
        finally:
            gate.set()
            self.fake_setup([wp])


class InTheAutopilot(Fakes, test_autopilot.PilotBase):
    def setUp(self):
        super().setUp()
        self.fake_setup([ap.wallpaper])

    def tearDown(self):
        self.fake_teardown()
        super().tearDown()

    def wired(self):
        p = self.pilot()
        p.wp = ap.wallpaper.Reporter(URL, KEY, os.path.join(self.dir, "state", "wallpaper.json"),
                                     clock=lambda: self.clock[0], log=p.say)
        return p

    def test_a_pilot_never_sends_unless_serve_gives_it_the_key(self):
        old = {k: os.environ.get(k) for k in (wp.URL_ENV, wp.KEY_ENV)}
        os.environ.update({wp.URL_ENV: URL, wp.KEY_ENV: KEY})       # as if a shell had read .env
        try:
            p = self.pilot()
            p.listen(0)
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.assertFalse(p.wp.on)
        self.assertEqual(self.posts, [])

    def test_the_regular_update_reads_the_account(self):
        self.router.add("GET", "/equity/account/summary", FakeResp(test_t212_place.SUMMARY))
        self.router.add("GET", "/equity/positions", FakeResp(test_t212_place.HELD7))
        p = self.wired()
        p.listen(0)
        p.listen(0)                                  # under 15 minutes later: nothing more
        self.assertEqual(len(self.posts), 1)
        d = self.posts[0]["body"]["data"]
        self.assertEqual((d["value"], d["positions"], d["cash_pct"], d["mode"]), (5000.0, 1, 82, "Practice"))
        self.assertEqual(d["next"], wp.NEXT)
        self.assertNotIn(KEY, "\n".join(self.out))

    def test_trading_212_down_still_sends_the_status(self):
        p = self.wired()                             # no fake replies: the reads fail
        p.listen(0)
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.posts[0]["body"]["data"]["status"], "running")
        self.assertNotIn("value", self.posts[0]["body"]["data"])

    def test_a_fill_sends_at_once_with_the_last_trade(self):
        self.router.add("GET", "/equity/account/summary", FakeResp(test_t212_place.SUMMARY))
        p = self.wired()
        p.wp.tried = self.clock[0]                   # a regular update just went
        p.state["positions"] = {}
        self.router.add("GET", "/equity/positions", FakeResp(test_t212_place.HELD7))
        p.watch_positions()
        self.assertEqual(len(self.posts), 1)
        lt = self.posts[0]["body"]["data"]["last_trade"]
        self.assertRegex(lt, r"^\d\d:\d\d BUY TSLA$")

    def test_the_serve_path_sends_stopped_in_a_finally(self):
        with open(os.path.join(ROOT, "trader-broker", "autopilot.py")) as fh:
            src = fh.read()
        i = src.index('if a.cmd == "serve":')
        block = src[i:i + 500]
        self.assertIn("stop_on_sigterm()", block)
        self.assertIn("finally:", block)
        self.assertIn("pilot.wp.stopped()", block)


if __name__ == "__main__":
    unittest.main()
