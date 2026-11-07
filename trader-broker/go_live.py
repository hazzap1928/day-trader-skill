#!/usr/bin/env python3
"""Switch day-trader-skill from the Trading 212 demo to LIVE (real money).

Run it yourself, in your own terminal. Claude never runs it.

    python3 trader-broker/go_live.py --check     pre-flight checks only: changes nothing
    python3 trader-broker/go_live.py             checks, then asks for the LIVE key and secret,
                                                 checks them with one read-only call, asks you to
                                                 type GO LIVE, backs up, and switches
    python3 trader-broker/go_live.py --revert    puts the demo keys and the demo config back

What switching changes, and nothing else:
  ~/.zshrc (or T212_ENV_FILE)   the export T212_API_KEY / T212_API_SECRET lines become the live
                                key and secret, and "export T212_ENV=live" is set
  trader/config.json            "paper": false and "live_enabled": true
Both are copied first to trader/backups/go-live-STAMP/ (only you can read it). The scheduled
guard (run-guard.sh) reads the same lines, so it runs on live from its next start.

Every check must pass before it asks for a key. See trader-broker/GO_LIVE.md.
Standard library only.
"""

import argparse
import datetime
import getpass
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PLIST = os.path.expanduser("~/Library/LaunchAgents/com.daytraderskill.t212-guard.plist")
CONFIRM = "GO LIVE"
ENV_LINE = re.compile(r"^[ \t]*export[ \t]+(T212_API_KEY|T212_API_SECRET|T212_ENV)=.*$", re.M)


class GoLiveError(Exception):
    pass


def _load(name, folder="trader-broker"):
    path = os.path.join(ROOT, folder, name + ".py")
    spec = importlib.util.spec_from_file_location("golive_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except ValueError as e:
        raise GoLiveError("%s is not valid JSON: %s" % (path, e))


def write_json(path, doc, mode=None):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    if mode:
        os.chmod(tmp, mode)
    os.replace(tmp, path)


# ---------------------------------------------------------------- the checks

def preflight(cfg, state_dir, env_text, run_tests=None, plist_path=PLIST):
    """(problems, notes). Any problem stops the switch."""
    t212, tk = _load("t212"), _load("ticket")
    problems, notes = [], []
    if not cfg:
        return ["no %s" % os.path.join(state_dir, "config.json")], notes
    try:
        cur = tk.config_currency(cfg)
        notes.append("money limits in %s" % cur)
    except tk.CurrencyError as e:
        problems.append(str(e))
    if cfg.get("live_enabled") is True or cfg.get("paper") is False:
        problems.append("the config already says live (live_enabled true or paper false); use --revert first")
    if cfg.get("paper") is not True:
        problems.append("\"paper\" must be true before switching (it is %r)" % cfg.get("paper"))
    try:
        allow = t212.allowed_tickers(cfg)
        if not allow:
            problems.append("allowed_tickers is missing or empty: live trading needs an allow-list")
        else:
            notes.append("allow-list: %s" % ", ".join(sorted(allow)))
    except t212.T212Error as e:
        problems.append(str(e))
    for keys, what in ((("max_loss_per_trade_gbp", "max_loss_per_trade_usd"), "a max loss per trade"),
                       (("max_daily_loss_gbp", "max_daily_loss_usd"), "a max daily loss")):
        if not any(isinstance(cfg.get(k), (int, float)) and cfg.get(k) > 0 for k in keys):
            problems.append("the config has no %s (%s)" % (what, " or ".join(keys)))
    if cfg.get("require_stop") is False:
        problems.append("\"require_stop\" is false: every live entry needs a stop")
    for k in ("allow_margin", "allow_short", "allow_options"):
        if cfg.get(k):
            problems.append("\"%s\" is on; switch it off for live" % k)
    try:
        notes.append("stops: %s" % t212.stop_mode(cfg))
        tr = t212.trail_settings(cfg)
        notes.append("trailing: %s" % ("off" if not tr else
                                       ("%sR" % tr["distance_r"]) if tr["distance_r"] is not None
                                       else "%s%%" % tr["distance_pct"]))
    except t212.T212Error as e:
        problems.append(str(e))
    broker = read_json(os.path.join(state_dir, "state", "broker.json"), {}) or {}
    demo_trades = [r for r in broker.get("orders", []) if r.get("broker") == "trading212" and r.get("env") == "demo"
                   and r.get("status") in ("placed", "stop_placed")]
    if not demo_trades:
        problems.append("no demo trade placed with t212.py place yet: watch the whole flow work on demo first")
    else:
        notes.append("%d demo order(s) placed with t212.py place" % len(demo_trades))
    if not os.path.exists(os.path.join(state_dir, "state", "t212-guard.json")):
        problems.append("the guard has never run (no trader/state/t212-guard.json): run t212.py guard on demo first")
    guard = read_json(os.path.join(state_dir, "state", "t212-guard.json"), {}) or {}
    if guard.get("unclear"):
        problems.append("the guard has unclear orders for %s: check the app and clear them first"
                        % ", ".join(sorted(guard["unclear"])))
    if not ENV_LINE.search(env_text or ""):
        problems.append("no 'export T212_API_KEY=' line in your shell file to switch")
    if plist_path and not os.path.exists(plist_path):
        problems.append("the scheduled guard is not installed (%s): without it nothing watches your stops"
                        % plist_path)
    if run_tests is not None:
        ok, summary = run_tests()
        (notes if ok else problems).append("test suite: %s" % summary)
    return problems, notes


def run_suite():
    p = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", os.path.join(ROOT, "tests")],
                       cwd=ROOT, capture_output=True, text=True)
    last = [ln for ln in (p.stderr or "").splitlines() if ln.startswith(("Ran ", "OK", "FAILED"))]
    return p.returncode == 0, " / ".join(last[-2:]) or "no output"


# ---------------------------------------------------------------- the switch

def _sq(v):
    """A value as a single-quoted shell word."""
    return "'" + str(v).replace("'", "'\"'\"'") + "'"


def rewrite_env(text, key, secret, env="live"):
    """The shell file with the T212 export lines replaced: key, secret and T212_ENV."""
    if not key or not secret or any(c in key + secret for c in "\n\r"):
        raise GoLiveError("the key and the secret must be one line each, not empty")
    want = {"T212_API_KEY": key, "T212_API_SECRET": secret, "T212_ENV": env}
    seen = set()

    def sub(m):
        name = m.group(1)
        if name in seen:
            return "# (replaced by go_live.py) " + m.group(0).strip().split("=")[0]
        seen.add(name)
        return "export %s=%s" % (name, _sq(want[name]))
    out = ENV_LINE.sub(sub, text)
    missing = [n for n in ("T212_API_KEY", "T212_API_SECRET", "T212_ENV") if n not in seen]
    if missing:
        if not out.endswith("\n"):
            out += "\n"
        out += "".join("export %s=%s\n" % (n, _sq(want[n])) for n in missing)
    return out


def live_config(cfg):
    return dict(cfg, paper=False, live_enabled=True)


def verify_live_keys(key, secret, state_dir):
    """One read-only call (t212.py status --live) with the new key. Returns the account summary."""
    env = dict(os.environ, T212_ENV="live", T212_API_KEY=key, T212_API_SECRET=secret)
    p = subprocess.run([sys.executable, os.path.join(HERE, "t212.py"), "status", "--live", "--json",
                        "--state-dir", state_dir], env=env, capture_output=True, text=True)
    if p.returncode != 0:
        raise GoLiveError("the live key did not work: %s" % (p.stderr.strip() or "no answer"))
    data = json.loads(p.stdout)
    if data.get("env") != "live":
        raise GoLiveError("that call did not reach the live account")
    return data


def backup(paths, state_dir, stamp):
    d = os.path.join(state_dir, "backups", "go-live-" + stamp)
    os.makedirs(d, mode=0o700, exist_ok=False)
    saved = {}
    for name, p in paths.items():
        dest = os.path.join(d, name)
        shutil.copy2(p, dest)
        os.chmod(dest, 0o600)
        saved[name] = {"from": p, "copy": dest}
    return d, saved


def switch(state_dir, env_file, ask=input, ask_secret=getpass.getpass, verify=verify_live_keys,
           run_tests=run_suite, plist_path=PLIST, now=None):
    cfg_path = os.path.join(state_dir, "config.json")
    cfg = read_json(cfg_path)
    with open(env_file, encoding="utf-8") as fh:
        env_text = fh.read()
    problems, notes = preflight(cfg, state_dir, env_text, run_tests, plist_path)
    for n in notes:
        print("  ok   " + n)
    if problems:
        for p in problems:
            print("  STOP " + p)
        raise GoLiveError("%d check(s) failed; nothing was changed" % len(problems))
    key = ask_secret("Trading 212 LIVE API key (hidden): ").strip()
    secret = ask_secret("Trading 212 LIVE API secret (hidden): ").strip()
    acct = verify(key, secret, state_dir)
    print("Live account %s, %s, total value %s." % (acct.get("account_id"), acct.get("currency"),
                                                     acct.get("total_value")))
    if cfg.get("currency") == "GBP" and str(acct.get("currency", "")).upper() != "GBP":
        raise GoLiveError("your limits are in GBP but that live account is in %s; nothing was changed"
                          % acct.get("currency"))
    print("This switches %s and %s to the LIVE account. Real money from the next order." % (env_file, cfg_path))
    if (ask('Type "%s" to switch (anything else cancels): ' % CONFIRM) or "").strip() != CONFIRM:
        print("Cancelled. Nothing was changed.")
        return None
    stamp = (now or datetime.datetime.now()).strftime("%Y%m%d-%H%M%S")
    bdir, saved = backup({"shell-env": env_file, "config.json": cfg_path}, state_dir, stamp)
    new_env = rewrite_env(env_text, key, secret)
    tmp = env_file + ".golive.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(new_env)
    os.chmod(tmp, os.stat(env_file).st_mode & 0o777)
    os.replace(tmp, env_file)
    write_json(cfg_path, live_config(cfg))
    write_json(os.path.join(state_dir, "state", "go-live.json"),
               {"switched_at": stamp, "backup": bdir, "files": saved, "account_id": acct.get("account_id")}, 0o600)
    print("LIVE. Backups in %s. Open a new terminal (or: source %s) before running anything." % (bdir, env_file))
    print("To go back to demo: python3 trader-broker/go_live.py --revert")
    return bdir


def revert(state_dir):
    rec = read_json(os.path.join(state_dir, "state", "go-live.json"))
    if not rec:
        raise GoLiveError("no trader/state/go-live.json: nothing to revert")
    for name, f in rec["files"].items():
        if not os.path.exists(f["copy"]):
            raise GoLiveError("the backup %s is missing; nothing was changed" % f["copy"])
    for name, f in rec["files"].items():
        shutil.copy2(f["copy"], f["from"])
        print("restored %s from %s" % (f["from"], f["copy"]))
    os.replace(os.path.join(state_dir, "state", "go-live.json"),
               os.path.join(state_dir, "state", "go-live-reverted-%s.json" % rec["switched_at"]))
    print("DEMO again. Open a new terminal (or source your shell file) before running anything.")
    return True


def main(argv=None):
    ap = argparse.ArgumentParser(description="Switch to the Trading 212 LIVE account (you run this, never Claude).")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--check", action="store_true", help="pre-flight checks only; change nothing")
    g.add_argument("--revert", action="store_true", help="put the demo keys and config back")
    ap.add_argument("--state-dir", default=os.path.join(ROOT, "trader"))
    ap.add_argument("--env-file", default=os.environ.get("T212_ENV_FILE", os.path.expanduser("~/.zshrc")))
    a = ap.parse_args(argv)
    try:
        if a.revert:
            revert(a.state_dir)
            return 0
        if a.check:
            with open(a.env_file, encoding="utf-8") as fh:
                env_text = fh.read()
            problems, notes = preflight(read_json(os.path.join(a.state_dir, "config.json")), a.state_dir,
                                        env_text, run_suite)
            for n in notes:
                print("  ok   " + n)
            for p in problems:
                print("  STOP " + p)
            print("%s. Nothing was changed." % ("Ready to switch" if not problems else
                                                "%d check(s) to fix first" % len(problems)))
            return 0 if not problems else 1
        if not sys.stdin.isatty():
            raise GoLiveError("run it in your own terminal: it asks for the key and for you to type GO LIVE")
        return 0 if switch(a.state_dir, a.env_file) else 1
    except GoLiveError as e:
        print("go_live: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
