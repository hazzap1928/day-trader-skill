"""t212.py: the rate-limit note is shared by the guard and the autopilot. Writing it must never
stop either of them (17 Nov 2026: a shared temporary file name stopped the guard). Offline."""

import os
import shutil
import tempfile
import threading
import unittest

from _load import load

t212 = load("trader-broker", "t212")


class RateLimitNote(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_writers_at_the_same_moment_never_fail_and_leave_no_temporary_files(self):
        path = os.path.join(self.dir, "state", "t212-ratelimit.json")
        errors = []

        def write(n):
            try:
                for i in range(200):
                    t212._write_json(path, {"writer": n, "i": i})
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=write, args=(n,)) for n in range(4)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        self.assertEqual(errors, [])
        self.assertEqual(sorted(os.listdir(os.path.dirname(path))), ["t212-ratelimit.json"])

    def test_a_failed_save_of_the_note_is_logged_and_never_raised(self):
        logged = []
        c = t212.Client("demo", "k", "s", state_dir=self.dir, log=logged.append)
        real = t212._write_json

        def boom(path, data):
            raise FileNotFoundError("gone")

        t212._write_json = boom
        try:
            c._remember("/equity/orders", {"x-ratelimit-remaining": "5"})
        finally:
            t212._write_json = real
        self.assertTrue(any("could not save the rate-limit note" in m for m in logged))


if __name__ == "__main__":
    unittest.main()
