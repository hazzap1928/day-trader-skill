"""go_live.py: pre-flight checks, the switch and the revert, on temporary files only.
Never your real ~/.zshrc or config, never a real key, never a network call."""

import io
import json
import os
import shutil
import stat
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load

gl = load("trader-broker", "go_live")

KEY, SECRET = "LIVE-KEY-test-not-real", "LIVE-SECRET-t'est-not-real"
GOOD = {"currency": "GBP", "budget_gbp": 500, "max_loss_per_trade_gbp": 10, "max_daily_loss_gbp": 25,
        "paper": True, "require_stop": True, "allowed_tickers": ["AAPL", "MSFT"], "stop_mode": "broker",
        "trailing_stop": {"enabled": True, "distance_r": 1.0}, "broker": "trading212"}
ENV = "# my shell\nexport PATH=/usr/bin\nexport T212_API_KEY=demo-key\nexport T212_API_SECRET=demo-secret\nalias ll='ls -l'\n"


class Setup(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.state = os.path.join(self.d, "trader")
        os.makedirs(os.path.join(self.state, "state"))
        self.env = os.path.join(self.d, "zshrc")
        self.plist = os.path.join(self.d, "guard.plist")
        open(self.plist, "w").close()
        self.write_cfg(GOOD)
        with open(self.env, "w") as fh:
            fh.write(ENV)
        self.write("state/broker.json", {"orders": [{"broker": "trading212", "env": "demo", "status": "placed"}]})
        self.write("state/t212-guard.json", {"unclear": {}})
        self.asked = []

    def tearDown(self):
        shutil.rmtree(self.d)

    def write(self, rel, doc):
        with open(os.path.join(self.state, rel), "w") as fh:
            json.dump(doc, fh)

    def write_cfg(self, cfg):
        self.write("config.json", cfg)

    def cfg(self):
        with open(os.path.join(self.state, "config.json")) as fh:
            return json.load(fh)

    def env_text(self):
        with open(self.env) as fh:
            return fh.read()

    def problems(self, cfg=None, env=ENV, tests=None):
        p, _ = gl.preflight(cfg if cfg is not None else self.cfg(), self.state, env, tests, self.plist)
        return p


class Preflight(Setup):
    def test_a_ready_setup_passes(self):
        self.assertEqual(self.problems(tests=lambda: (True, "OK")), [])

    def test_each_missing_safety_stops_it(self):
        cases = {
            "allowed_tickers": dict(GOOD, allowed_tickers=[]),
            "max loss per trade": {k: v for k, v in GOOD.items() if k != "max_loss_per_trade_gbp"},
            "max daily loss": {k: v for k, v in GOOD.items() if k != "max_daily_loss_gbp"},
            "already says live": dict(GOOD, paper=False),
            "require_stop": dict(GOOD, require_stop=False),
            "allow_margin": dict(GOOD, allow_margin=True),
            "stop_mode": dict(GOOD, stop_mode="hope"),
        }
        for word, cfg in cases.items():
            with self.subTest(word):
                self.assertTrue(any(word in p for p in self.problems(cfg)), self.problems(cfg))

    def test_it_must_have_worked_on_demo_first(self):
        self.write("state/broker.json", {"orders": []})
        self.assertTrue(any("no demo trade" in p for p in self.problems()))
        os.remove(os.path.join(self.state, "state", "t212-guard.json"))
        self.assertTrue(any("guard has never run" in p for p in self.problems()))

    def test_unclear_guard_orders_stop_it(self):
        self.write("state/t212-guard.json", {"unclear": {"TSLA_US_EQ": "503"}})
        self.assertTrue(any("unclear" in p for p in self.problems()))

    def test_failing_tests_the_shell_file_and_the_schedule_stop_it(self):
        self.assertTrue(any("test suite" in p for p in self.problems(tests=lambda: (False, "FAILED"))))
        self.assertTrue(any("export T212_API_KEY" in p for p in self.problems(env="# nothing\n")))
        os.remove(self.plist)
        self.assertTrue(any("scheduled guard" in p for p in self.problems()))


class RewriteEnv(unittest.TestCase):
    def test_only_the_t212_lines_change(self):
        out = gl.rewrite_env(ENV, KEY, SECRET)
        self.assertIn("export PATH=/usr/bin\n", out)
        self.assertIn("alias ll='ls -l'\n", out)
        self.assertIn("export T212_API_KEY='%s'" % KEY, out)
        self.assertIn("export T212_ENV='live'", out)
        self.assertNotIn("demo-key", out)
        self.assertEqual(out.count("export T212_API_KEY="), 1)

    def test_a_quote_in_the_secret_survives_the_shell(self):
        out = gl.rewrite_env(ENV, KEY, SECRET)
        self.assertIn("""export T212_API_SECRET='LIVE-SECRET-t'"'"'est-not-real'""", out)

    def test_duplicates_are_commented_out_without_their_values(self):
        out = gl.rewrite_env(ENV + "export T212_API_KEY=old-demo\n", KEY, SECRET)
        self.assertNotIn("old-demo", out)
        self.assertEqual(out.count("export T212_API_KEY="), 1)

    def test_an_empty_or_multi_line_key_is_refused(self):
        for k in ("", "a\nb"):
            with self.assertRaises(gl.GoLiveError):
                gl.rewrite_env(ENV, k, SECRET)


class Switch(Setup):
    def run_switch(self, typed="GO LIVE", verify=None, tests=(True, "OK")):
        out = io.StringIO()
        secrets = iter([KEY, SECRET])

        def ask_secret(prompt):
            self.asked.append(prompt)
            return next(secrets)
        with redirect_stdout(out):
            r = gl.switch(self.state, self.env, ask=lambda p: typed, ask_secret=ask_secret,
                          verify=verify or (lambda k, s, d: {"env": "live", "account_id": 1, "currency": "GBP",
                                                             "total_value": 500}),
                          run_tests=lambda: tests, plist_path=self.plist)
        return r, out.getvalue()

    def test_the_switch_backs_up_then_switches_both_files(self):
        bdir, text = self.run_switch()
        self.assertIn("LIVE. Backups in", text)
        self.assertEqual(self.cfg()["paper"], False)
        self.assertEqual(self.cfg()["live_enabled"], True)
        self.assertIn("export T212_ENV='live'", self.env_text())
        with open(os.path.join(bdir, "shell-env")) as fh:
            self.assertEqual(fh.read(), ENV)
        for f in ("shell-env", "config.json"):
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(bdir, f)).st_mode), 0o600)
        self.assertNotIn(KEY, text)
        self.assertNotIn(SECRET, text)

    def test_anything_but_go_live_changes_nothing(self):
        r, text = self.run_switch(typed="yes")
        self.assertIsNone(r)
        self.assertEqual(self.env_text(), ENV)
        self.assertEqual(self.cfg(), GOOD)
        self.assertFalse(os.path.exists(os.path.join(self.state, "backups")))

    def test_a_failed_check_never_asks_for_a_key(self):
        self.write_cfg(dict(GOOD, allowed_tickers=[]))
        with self.assertRaises(gl.GoLiveError):
            self.run_switch()
        self.assertEqual(self.asked, [])

    def test_a_bad_key_or_a_usd_account_changes_nothing(self):
        def bad(k, s, d):
            raise gl.GoLiveError("the live key did not work")
        with self.assertRaises(gl.GoLiveError):
            self.run_switch(verify=bad)
        with self.assertRaises(gl.GoLiveError):
            self.run_switch(verify=lambda k, s, d: {"env": "live", "currency": "USD"})
        self.assertEqual(self.env_text(), ENV)
        self.assertEqual(self.cfg(), GOOD)

    def test_revert_puts_both_files_back(self):
        self.run_switch()
        with redirect_stdout(io.StringIO()):
            gl.revert(self.state)
        self.assertEqual(self.env_text(), ENV)
        self.assertEqual(self.cfg(), GOOD)
        self.assertFalse(os.path.exists(os.path.join(self.state, "state", "go-live.json")))

    def test_it_will_not_run_without_a_terminal(self):
        err = io.StringIO()
        saved, gl.sys.stdin = gl.sys.stdin, io.StringIO("")        # never a terminal, even when you run the tests
        try:
            with redirect_stderr(err), redirect_stdout(io.StringIO()):
                code = gl.main(["--state-dir", self.state, "--env-file", self.env])
        finally:
            gl.sys.stdin = saved
        self.assertEqual(code, 1)
        self.assertIn("your own terminal", err.getvalue())
        self.assertEqual(self.env_text(), ENV)


if __name__ == "__main__":
    unittest.main()
