"""Files more than one program writes (audit step 3, 17 Nov 2026): the holdings record and the phone outbox.
Offline: temporary folders only, no network, no real phone."""

import fcntl
import json
import os
import subprocess
import sys
import tempfile
import unittest

from _load import load

holdings = load("trader-broker", "holdings")
phone = load("trader-broker", "phone")

TRY_LOCK = ("import fcntl, sys\n"
            "fh = open(sys.argv[1], 'a')\n"
            "try:\n"
            "    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
            "    print('got it')\n"
            "except OSError:\n"
            "    print('busy')\n")


def other_program_can_lock(lock_path):
    out = subprocess.run([sys.executable, "-c", TRY_LOCK, lock_path], capture_output=True, text=True, timeout=30)
    return out.stdout.strip() == "got it"


class HoldingsLock(unittest.TestCase):
    def setUp(self):
        self.sd = tempfile.mkdtemp()

    def test_another_program_waits_while_the_record_is_held(self):
        lock = holdings.path(self.sd) + ".lock"
        with holdings.locked(self.sd):
            self.assertFalse(other_program_can_lock(lock))
        self.assertTrue(other_program_can_lock(lock))

    def test_the_lock_can_be_taken_again_inside_one_thread(self):
        with holdings.locked(self.sd):
            with holdings.locked(self.sd):           # re-entrant: no deadlock
                holdings.save(self.sd, {"schema": 1, "pages": []})
            self.assertFalse(other_program_can_lock(holdings.path(self.sd) + ".lock"))
        self.assertEqual(holdings.load(self.sd).get("pages"), [])

    def test_save_leaves_no_temporary_file(self):
        holdings.save(self.sd, {"schema": 1, "pages": []})
        left = [f for f in os.listdir(os.path.dirname(holdings.path(self.sd))) if f.endswith(".tmp")]
        self.assertEqual(left, [])


class FakePhone(object):
    def __init__(self, during_send=None):
        self.sent, self.during_send = [], during_send

    def send(self, text, **kw):
        self.sent.append(text)
        if self.during_send:
            self.during_send()


class Outbox(unittest.TestCase):
    def setUp(self):
        self.sd = tempfile.mkdtemp()

    def items(self):
        with open(phone.outbox_path(self.sd)) as fh:
            return [i["text"] for i in json.load(fh).get("items") or []]

    def test_a_message_queued_while_the_outbox_is_sent_is_kept(self):
        phone.queue(self.sd, "first")
        phone.queue(self.sd, "second")
        p = FakePhone(during_send=lambda: phone.queue(self.sd, "queued meanwhile by another program"))
        self.assertEqual(phone.flush_outbox(p, self.sd), 2)
        self.assertEqual(len(p.sent), 1)
        self.assertEqual(self.items(), ["queued meanwhile by another program"])

    def test_a_failed_send_keeps_everything(self):
        phone.queue(self.sd, "first")

        class Down(FakePhone):
            def send(self, text, **kw):
                raise phone.PhoneError("offline (test)")
        with self.assertRaises(phone.PhoneError):
            phone.flush_outbox(Down(), self.sd)
        self.assertEqual(self.items(), ["first"])

    def test_queue_holds_the_lock(self):
        lock = phone.outbox_path(self.sd) + ".lock"
        with phone._outbox_lock(self.sd):
            self.assertFalse(other_program_can_lock(lock))
        self.assertTrue(other_program_can_lock(lock))


if __name__ == "__main__":
    unittest.main()
