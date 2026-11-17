"""The pre-push secrets check (.githooks/pre-push): the email rules added on 17 Nov 2026 (audit step 5).
Offline: a throwaway git repository in a temporary folder; nothing is pushed anywhere."""

import importlib.machinery
import os
import subprocess
import tempfile
import unittest

from _load import ROOT

hook = importlib.machinery.SourceFileLoader("prepush_hook", os.path.join(ROOT, ".githooks", "pre-push")).load_module()

NOREPLY = "12345+someone@users.noreply.github.com"
PERSONAL = "some.person@" + "mail-provider.test"        # built, so this file has no address in it


class Addresses(unittest.TestCase):
    def test_which_addresses_pass(self):
        for ok in (NOREPLY, "noreply@anthropic.com", "a@example.com", "b@example.org", "webmaster@sec.gov"):
            self.assertTrue(hook.email_ok(ok), ok)
        for bad in (PERSONAL, "noreply.fake@" + "mail-provider.test", "x@" + "notexample.com"):
            self.assertFalse(hook.email_ok(bad), bad)

    def test_code_is_not_mistaken_for_an_address(self):
        for line in ("@contextlib.contextmanager", "    @property", 'pkg = "react@18.3.1"', "x = a@b"):
            self.assertEqual([m.group(0) for m in hook.EMAIL.finditer(line)], [], line)
        self.assertEqual([m.group(0) for m in hook.EMAIL.finditer("contact " + PERSONAL)], [PERSONAL])

    def test_no_test_file_has_an_address_the_check_would_stop(self):
        # a fake address in a test must be built from pieces (as PERSONAL is), or the push is stopped
        found = []
        for name in sorted(os.listdir(os.path.join(ROOT, "tests"))):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(ROOT, "tests", name), encoding="utf-8") as f:
                for n, line in enumerate(f, 1):
                    found += ["%s:%d %s" % (name, n, m.group(0)) for m in hook.EMAIL.finditer(line)
                              if not hook.email_ok(m.group(0))]
        self.assertEqual(found, [])


class Commits(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        self.git("init", "-q")
        os.chdir(self.repo)

    def tearDown(self):
        os.chdir(self.cwd)

    def git(self, *args, email=NOREPLY):
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL=email, GIT_COMMITTER_NAME="t",
                   GIT_COMMITTER_EMAIL=email, GIT_CONFIG_NOSYSTEM="1", HOME=self.repo)
        return subprocess.run(["git"] + list(args), cwd=self.repo, env=env, check=True,
                              capture_output=True, text=True).stdout.strip()

    def commit(self, name, text, email=NOREPLY):
        with open(os.path.join(self.repo, name), "w") as fh:
            fh.write(text)
        self.git("add", name)
        self.git("commit", "-q", "-m", "x", email=email)
        return self.git("rev-parse", "HEAD")

    def test_a_clean_commit_passes(self):
        sha = self.commit("a.py", "print('hello')\n# Co-Authored-By: Claude <noreply@anthropic.com>\n")
        self.assertEqual(hook.check(sha, []), [])

    def test_a_personal_author_email_is_stopped(self):
        sha = self.commit("a.py", "print('hello')\n", email=PERSONAL)
        self.assertIn(("(commit)", "an author or committer email that is not a no-reply address"), hook.check(sha, []))

    def test_a_personal_address_in_a_file_is_stopped_and_not_printed(self):
        sha = self.commit("b.md", "Write to %s\n" % PERSONAL)
        found = hook.check(sha, [])
        self.assertIn(("b.md", "an email address"), found)
        self.assertNotIn(PERSONAL, repr(found))


if __name__ == "__main__":
    unittest.main()
