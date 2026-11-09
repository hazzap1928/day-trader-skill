#!/usr/bin/env python3
"""Autopilot (DEMO ONLY): builds tickets every 15 minutes, asks your phone, places on your tap.

    python3 trader-broker/autopilot.py run       # the trading day (launchd starts it on weekdays)
    python3 trader-broker/autopilot.py cycle     # one scan-to-ticket cycle now, and wait for your tap
    python3 trader-broker/autopilot.py summary   # send today's summary to your phone now
    python3 trader-broker/autopilot.py status    # what it has done today

Each cycle, during the regular session and inside your entry window, runs the same
steps you would: scanner (orb, allowed_tickers only) -> entry -> exit plan -> Time
Machine -> trader-budget -> trader-risk. A ticket that passes all of them goes to your
phone with Approve and Reject buttons. Nothing is placed without your Approve, and a
ticket expires after 10 minutes.

Your Approve does not skip a check: it fetches a fresh quote, refuses if the price
has run more than max_chase_r past the entry, runs trader-risk again, and then
calls the same t212.py place code your typed yes uses, on the DEMO account only.
There is no live path here: T212_ENV=live makes it refuse to start.

It also sends fills and exits (from the positions it watches each minute) and a
daily summary after the close. Guard alerts come from t212.py guard itself.

With "auto_buy": true (demo only) there is no Approve step: a ticket that passes every
check is placed at once, after the same checks an Approve runs, and the phone is told
what was bought (or why a passing ticket was not placed). Live has no automatic buying.
After the 15:50 end-of-day close it sends the closing report (trader-broker/report.py).

Config (trader/config.json), all optional except allowed_tickers:
    "allowed_tickers": ["AAPL", ...]      required: the autopilot never scans anything else
    "autopilot": {"every_minutes": 15, "expire_minutes": 10, "max_chase_r": 0.5,
                  "min_edge": 0.0, "max_tickets_per_day": 10, "auto_buy": false}

From your phone: /pause stops new tickets, /resume starts them again, /status.
Education and tooling, not financial advice.
"""

import argparse
import datetime
import hmac
import importlib.util
import io
import json
import os
import re
import secrets as _rand
import subprocess
import sys
import time
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
AUTOPILOT_ENV = "demo"           # the one account this ever places on
DEFAULTS = {"every_minutes": 15, "expire_minutes": 10, "max_chase_r": 0.5, "min_edge": 0.0,
            "max_tickets_per_day": 10, "min_net_r": 0.0}
FLAGS = {"auto_buy": False,      # true: a ticket that passes every check is placed without a tap (demo only)
         "evidence_mode": False}  # true: the demo forward test (trader-broker/evidence.py); live refuses it
POSITIONS_EVERY = 60             # seconds between fill and exit checks
POLL_WAIT = 20                   # seconds a phone poll waits for a tap
BRIEFING_CATCHUP = (7, 45)       # UK time on the Mac: from then until the open a missed briefing is run again
REPORT_AFTER = 5                 # minutes after the end-of-day close (15:50 New York) the closing report goes
EOD_DEFAULT = "15:50"
STEP_TIMEOUT = 900


class PilotError(Exception):
    pass


def _load(folder, name):
    path = os.path.join(ROOT, folder, name + ".py")
    spec = importlib.util.spec_from_file_location("autopilot_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


t212 = _load("trader-broker", "t212")
phone_mod = t212.phone_module()
net = t212.net_module()
evidence = _load("trader-broker", "evidence")
KRONOS_BUDGET = 6 * 60          # seconds of Kronos shadow verdicts a cycle at most (it never blocks a trade)
tk = t212.ticket_module()
_MODS = {}


def mod(folder, name):
    if name not in _MODS:
        _MODS[name] = _load(folder, name)
    return _MODS[name]


# ---------------------------------------------------------------- seams the tests replace

def _now():
    return time.time()


def _sleep(s):
    time.sleep(s)


def _local(ts):
    """The Mac's own clock (UK time). Tests replace this."""
    return datetime.datetime.fromtimestamp(ts)


def _spawn(args, log_path):
    """Start a process in the background, its output appended to a log. Tests replace this."""
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, "a") as fh:
        subprocess.Popen(list(args), cwd=ROOT, stdout=fh, stderr=fh, start_new_session=True)


def _run(args, timeout=STEP_TIMEOUT):
    """One pipeline step as its own process, from the repo root. Returns (code, stdout, stderr)."""
    try:
        p = subprocess.run([sys.executable] + list(args), cwd=ROOT, capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timed out after %ds" % timeout


def _utc():
    return datetime.datetime.fromtimestamp(_now(), datetime.timezone.utc).replace(tzinfo=None, microsecond=0)


def _ny():
    return tk.to_eastern(_utc())


def _hm(text, default):
    m = re.match(r"^(\d{1,2}):(\d{2})$", str(text or default).strip())
    if not m:
        m = re.match(r"^(\d{1,2}):(\d{2})$", default)
    return int(m.group(1)), int(m.group(2))


def _uk(ts):
    """A time as your Mac shows it (you set the Mac to UK time)."""
    return datetime.datetime.fromtimestamp(ts).strftime("%H:%M")


def _gbp(x):
    return "£{:,.2f}".format(x) if x is not None else "?"


# ---------------------------------------------------------------- settings

def settings(cfg):
    s = dict(DEFAULTS)
    s.update({k: v for k, v in (cfg.get("autopilot") or {}).items() if k in DEFAULTS})
    for k, default in FLAGS.items():
        v = (cfg.get("autopilot") or {}).get(k, default)
        if not isinstance(v, bool):
            raise PilotError("autopilot.%s must be true or false" % k)
        s[k] = v
    for k in DEFAULTS:
        if k in FLAGS:
            continue
        try:
            s[k] = float(s[k])
        except (TypeError, ValueError):
            raise PilotError("autopilot.%s must be a number" % k)
    if not 5 <= s["every_minutes"] <= 120:
        raise PilotError("autopilot.every_minutes must be 5 to 120")
    if not 1 <= s["expire_minutes"] <= 30:
        raise PilotError("autopilot.expire_minutes must be 1 to 30")
    if s["expire_minutes"] >= s["every_minutes"]:
        raise PilotError("autopilot.expire_minutes must be shorter than every_minutes, so a ticket is answered "
                         "or gone before the next cycle")
    if not 0 <= s["max_chase_r"] <= 2:
        raise PilotError("autopilot.max_chase_r must be 0 to 2")
    if not -1 <= s["min_net_r"] <= 2:
        raise PilotError("autopilot.min_net_r must be -1 to 2 (R after costs)")
    return s


KEEP_AWAKE_LABEL = "com.daytraderskill.caffeinate"
GUARD_LABEL = "com.daytraderskill.t212-guard"
AUTOPILOT_LABEL = "com.daytraderskill.autopilot"


def _launchd(label):
    """{"loaded", "running", "pid", "out"} for one of this user's launchd jobs."""
    try:
        p = subprocess.run(["/bin/launchctl", "print", "gui/%d/%s" % (os.getuid(), label)],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as e:
        return {"loaded": False, "running": False, "pid": None, "out": "", "error": str(e)}
    if p.returncode != 0:
        return {"loaded": False, "running": False, "pid": None, "out": p.stdout}
    pid = re.search(r"^\s*pid = (\d+)", p.stdout, re.M)
    return {"loaded": True, "running": bool(re.search(r"^\s*state = running", p.stdout, re.M)),
            "pid": pid.group(1) if pid else None, "out": p.stdout}


def _keep_awake():
    """(running, detail): is the launchd job that runs `caffeinate -s` up?"""
    j = _launchd(KEEP_AWAKE_LABEL)
    if j.get("error"):
        return False, "could not ask launchd (%s)" % j["error"]
    if not j["loaded"]:
        return False, "the %s launchd job is not loaded" % KEEP_AWAKE_LABEL
    if j["running"] and "caffeinate" in j["out"] and "-s" in j["out"]:
        return True, "caffeinate -s is running (pid %s)" % (j["pid"] or "?")
    return False, "the %s job is loaded but not running" % KEEP_AWAKE_LABEL


def _guard_state(state_dir):
    """(running, line) about the stop guard: its launchd job and its last check in trader/logs/guard.log."""
    j = _launchd(GUARD_LABEL)
    last = None
    try:
        with open(os.path.join(state_dir, "logs", "guard.log"), encoding="utf-8") as fh:
            for line in fh:
                m = re.match(r"^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}):\d{2}[-+]\d{2}:\d{2}  stop guard", line)
                if m:
                    last = "%s %s New York" % m.groups()
    except OSError:
        pass
    if j["running"]:
        return True, "running (pid %s)%s" % (j["pid"] or "?", "; last check %s" % last if last else "; no check yet today")
    return False, "!!! NOT RUNNING (%s): stops are not being checked !!!" % ("not loaded" if not j["loaded"] else "loaded, stopped")


def _on_mains():
    """(on_mains, detail) from pmset: caffeinate -s only holds the Mac awake on mains power."""
    try:
        out = subprocess.run(["/usr/bin/pmset", "-g", "batt"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return True, "power source unknown"
    if "AC Power" in out:
        return True, "on mains power"
    if "Battery Power" in out:
        return False, "ON BATTERY: caffeinate -s does not stop sleep on battery; plug the Mac in"
    return True, "power source unknown"


def check_ready(cfg, client_env, phone):
    """Every reason the autopilot will not start. Empty means ready."""
    problems = []
    if client_env != AUTOPILOT_ENV:
        problems.append("it runs on the demo account only (T212_ENV is %s)" % client_env)
    if cfg.get("paper") is not True:
        problems.append("trader/config.json must have \"paper\": true")
    if cfg.get("live_enabled"):
        problems.append("trader/config.json has \"live_enabled\": true; the autopilot is demo only")
    if not t212.allowed_tickers(cfg):
        problems.append("trader/config.json has no allowed_tickers list: add the tickers it may scan")
    if cfg.get("require_stop", True) is not True:
        problems.append("\"require_stop\" must stay true")
    try:
        evidence.require_demo(cfg, client_env)
        if evidence.on(cfg):
            evidence.window(cfg)
            if (cfg.get("autopilot") or {}).get("auto_buy") is not True:
                problems.append("evidence mode needs autopilot.auto_buy true, so passing trades are placed")
    except evidence.EvidenceError as e:
        problems.append(str(e))
    if (cfg.get("autopilot") or {}).get("auto_buy") is True and client_env != AUTOPILOT_ENV:
        problems.append("autopilot.auto_buy works on the demo account only; there is no live automatic buying")
    awake, why = _keep_awake()
    if not awake:
        problems.append("the Mac may sleep: %s. Load it with: launchctl bootstrap gui/$(id -u) "
                        "~/Library/LaunchAgents/%s.plist" % (why, KEEP_AWAKE_LABEL))
    if phone is None:
        problems.append("no phone paired: set TELEGRAM_BOT_TOKEN, then run python3 trader-broker/phone.py setup")
    try:
        settings(cfg)
    except PilotError as e:
        problems.append(str(e))
    return problems


# ---------------------------------------------------------------- the pilot

class Pilot(object):
    def __init__(self, client, phone, state_dir, out=None):
        if client.env != AUTOPILOT_ENV:
            raise PilotError("the autopilot is demo only; refusing the %s account" % client.env)
        self.client, self.phone, self.sd = client, phone, os.path.abspath(state_dir)
        self.out = out or (lambda text: (sys.stdout.write(text + "\n"), sys.stdout.flush()))
        self.path = os.path.join(self.sd, "state", "autopilot.json")
        self.cfg = self.read_cfg()
        self.s = settings(self.cfg)
        self.state = self._load_state()

    # ------------------------------------------------------------ files

    def p(self, *parts):
        return os.path.join(self.sd, *parts)

    def read_cfg(self):
        try:
            return tk.read_config(self.p("config.json"))
        except tk.TicketError as e:
            raise PilotError(str(e))

    def _load_state(self):
        st = t212._read_json(self.path, {}) or {}
        today = _ny().date().isoformat()
        if st.get("day") != today:
            st = {"day": today, "tickets": [], "tried": {}, "events": [], "paused": bool(st.get("paused")),
                  "positions": st.get("positions") or {}, "start_value": None, "summary_sent": False}
        return st

    def save(self):
        t212._write_json(self.path, self.state)

    def say(self, text):
        self.out("%s  %s" % (_ny().strftime("%H:%M"), text))

    def event(self, text, push=True):
        self.state["events"].append({"at": _ny().strftime("%H:%M"), "text": text})
        self.save()
        self.say(text)
        if push:
            phone_mod.push(self.sd, text, phone=self.phone)

    def run(self, args, what):
        code, out, err = _run(args)
        if code not in (0, 1):
            raise PilotError("%s failed (exit %s): %s" % (what, code, (err or out).strip()[-300:]))
        return code, out, err

    def json_of(self, text, what):
        try:
            return json.loads(text)
        except ValueError:
            raise PilotError("%s did not print JSON" % what)

    # ------------------------------------------------------------ the cycle: scan to ticket

    def pending(self):
        return [t for t in self.state["tickets"] if t["status"] in ("pending", "approving")]

    def cycle(self):
        """One pass of the pipeline. Sends at most one ticket. Returns a short line of what happened."""
        self.cfg = self.read_cfg()
        self.s = settings(self.cfg)
        if self.state.get("paused"):
            return "paused from your phone: no scan"
        if self.pending():
            return "a ticket is waiting for your answer: no new scan"
        if len(self.state["tickets"]) >= self.s["max_tickets_per_day"]:
            return "%d tickets sent today, the most autopilot.max_tickets_per_day allows" % len(self.state["tickets"])
        allow = t212.allowed_tickers(self.cfg)
        if not allow:
            raise PilotError("no allowed_tickers in trader/config.json")
        cfgp, fx = self.p("config.json"), self.p("fx.json")
        self.fx_fresh()
        code, out, _ = self.run(["trader-scanner/scan.py", "orb", "--symbols", ",".join(sorted(allow)),
                                 "--config", cfgp, "--out", self.p("watchlist.json"), "--quiet", "--json"],
                                "the scanner")
        if code == 1:
            return "scanner: no opening-range breakouts on your list"
        code, out, _ = self.run(["trader-entry/entry.py", "watchlist", "--watchlist", self.p("watchlist.json"),
                                 "--config", cfgp, "--json"], "trader-entry")
        rows = self.json_of(out, "trader-entry") if out.strip() else []
        held = self.held_symbols()
        notes = []
        for r in rows:
            sym = str(r.get("symbol") or "").upper()
            res = r.get("result") or {}
            if res.get("status") != "triggered":
                continue
            if sym not in allow:
                notes.append("%s not on allowed_tickers" % sym)
                continue
            if sym in self.state["tried"]:
                continue
            if sym in held:
                self.state["tried"][sym] = "already held"
                continue
            self.sig = None
            got = self.build(sym, cfgp, fx)
            self.state["tried"][sym] = got if isinstance(got, str) else "passed every check"
            self.save()
            if isinstance(got, str):
                notes.append("%s: %s" % (sym, got))
                self.log_signal(got)
                continue
            if self.s["auto_buy"]:
                t = self.auto_buy(got)
                self.log_signal("traded" if t["status"] == "placed" else "not placed: %s" % t.get("note", t["status"]),
                                ticket=t["id"])
                if self.s["evidence_mode"]:
                    notes.append("%s: %s" % (sym, t["status"]))
                    continue                     # evidence mode: every passing signal is placed (limits permitting)
                return "%s: %s" % (sym, t["status"])
            self.log_signal("ticket sent to the phone")
            self.send_ticket(got)
            return "%s: ticket sent to your phone" % sym
        self.kronos_pass()
        return "no ticket: " + ("; ".join(notes) if notes else "nothing triggered that was not tried today")

    def log_signal(self, decision, **extra):
        """Evidence mode: every triggered signal goes in the log, traded or not."""
        if not self.s["evidence_mode"] or not getattr(self, "sig", None):
            return
        rec = dict(self.sig, decision=decision, **extra)
        rec["id"] = evidence.log_signal(self.sd, rec)
        self.cycle_signals = getattr(self, "cycle_signals", []) + [rec]

    def kronos_pass(self):
        """Kronos's verdict on this cycle's signals, in shadow: logged only, within a time budget."""
        todo, self.cycle_signals = getattr(self, "cycle_signals", []), []
        if not todo:
            return
        start = _now()
        for rec in todo:
            if rec.get("entry") is None:
                continue
            if _now() - start > KRONOS_BUDGET:
                evidence.update_signal(self.sd, rec["id"], kronos={"verdict": "skipped", "why": "time budget"})
                continue
            evidence.update_signal(self.sd, rec["id"], kronos=evidence.kronos_verdict(rec))

    def fx_fresh(self):
        doc = t212._read_json(self.p("fx.json"), {}) or {}
        when = t212._iso_ts(doc.get("fetched_at") or "")
        if when is None or _now() - when > 6 * 3600:
            self.run(["trader-broker/t212.py", "fx", "--state-dir", self.sd], "the GBP/USD rate")

    def held_symbols(self):
        out = set()
        for p in self.client.get(t212.POSITIONS) or []:
            t = t212._o_ticker(p) or ""
            if (t212._num(p.get("quantity")) or 0) > 0:
                out.add(t.split("_")[0].upper())
        return out

    def build(self, sym, cfgp, fx):
        """entry -> exit -> Time Machine -> budget -> risk for one symbol. A ticket dict, or why not.
        self.sig collects the signal for the evidence log as it goes."""
        flat = self.cfg.get("flat_by") or "15:55"
        ny = _ny()
        self.sig = sig = {"day": ny.date().isoformat(), "time": ny.strftime("%H:%M"), "symbol": sym, "at": _now()}
        self.run(["trader-entry/entry.py", "check", sym, "--config", cfgp, "--write", self.p("entry.json")],
                 "trader-entry check")
        code, out, err = self.run(["trader-exit/exit.py", "plan", "--from", self.p("entry.json"), "--config", cfgp,
                                   "--write", self.p("plan.json")], "trader-exit")
        if code != 0:
            return "no exit plan (%s)" % (err or out).strip()[-160:]
        plan = t212._read_json(self.p("plan.json"), {}) or {}
        sig.update(entry=plan.get("entry"), stop=plan.get("stop"), target=plan.get("target"),
                   target_r=plan.get("target_r"), stop_rule=plan.get("stop_rule"))
        code, out, err = self.run(["trader-timemachine/timemachine.py", "run", "--plan", self.p("plan.json"),
                                   "--until", flat, "--config", cfgp, "--write", self.p("timemachine.json"), "--json"],
                                  "the Time Machine")
        if code != 0:
            return "Time Machine could not run (%s)" % (err or out).strip()[-160:]
        doc = t212._read_json(self.p("timemachine.json"), {}) or {}
        mc0, c0 = doc.get("mc") or {}, doc.get("costs") or {}
        sig.update(tm_p_target=mc0.get("p_target"), tm_p_stop=mc0.get("p_stop"), tm_avg_R=mc0.get("avg_R"),
                   tm_avg_R_net=c0.get("mc_avg_R_net"), tm_cost_R=c0.get("mc_cost_R"), stop_pct=c0.get("stop_pct"))
        lean = mod("trader-timemachine", "timemachine").lean(doc.get("mc") or {}) if doc.get("mc") else None
        if lean is None or lean[0] <= self.s["min_edge"]:
            return "Time Machine: %s" % (doc.get("verdict") or "no edge")
        net = (doc.get("costs") or {}).get("mc_avg_R_net")
        if net is None:
            return "Time Machine: no average R after costs in its output, so no ticket"
        if net <= self.s["min_net_r"] and self.s["evidence_mode"]:
            sig["cost_block_ignored"] = True       # evidence mode: logged, not blocked
        elif net <= self.s["min_net_r"]:
            return ("Time Machine: average R after costs %+.3f (before %+.3f; costs %.3fR at a %.2f %% stop) is not "
                    "above autopilot.min_net_r %g" % (net, doc["costs"].get("mc_avg_R", 0), doc["costs"].get("mc_cost_R", 0),
                                                      doc["costs"].get("stop_pct", 0), self.s["min_net_r"]))
        self.run(["trader-alerts/alerts.py", "quote", sym, "--write", self.p("prices.json")], "a quote")
        code, out, err = self.run(["trader-budget/size.py", "--plan", self.p("plan.json"), "--config", cfgp,
                                   "--fx", fx, "--prices", self.p("prices.json"),
                                   "--portfolio", self.p("portfolio.json"), "--write", self.p("orders.json")],
                                  "trader-budget")
        if code != 0:
            return "trader-budget made no order (%s)" % (err or out).strip()[-160:]
        verdict = self.risk()
        sig.update(rr_net=verdict.get("reward_risk_net"), rr=verdict.get("reward_risk"), risk_verdict=verdict.get("verdict"))
        if verdict.get("verdict") != "PASS":
            return "trader-risk BLOCKED: %s" % "; ".join(verdict.get("reasons") or ["?"])
        order = self.order()
        mc = doc.get("mc") or {}
        return {"symbol": sym, "fingerprint": verdict["fingerprint"], "order": order, "risk": verdict,
                "verdict": doc.get("verdict") or "",
                "tm": {"p_target": mc.get("p_target"), "p_stop": mc.get("p_stop"), "verdict": doc.get("verdict") or "",
                       "avg_R": mc.get("avg_R"), "avg_R_net": (doc.get("costs") or {}).get("mc_avg_R_net")}}

    def risk(self):
        self.run(["trader-risk/check.py", "--orders", self.p("orders.json"), "--config", self.p("config.json"),
                  "--portfolio", self.p("portfolio.json"), "--prices", self.p("prices.json"), "--fx", self.p("fx.json"),
                  "--state", self.p("state", "broker.json"), "--out", self.p("state", "risk.json")], "trader-risk")
        doc = t212._read_json(self.p("state", "risk.json"), {}) or {}
        res = [r for r in doc.get("results") or [] if r.get("index") == 1]
        if doc.get("date") != _ny().date().isoformat() or not res:
            raise PilotError("trader-risk wrote no verdict for today")
        return res[0]

    def order(self):
        doc = t212._read_json(self.p("orders.json"), {}) or {}
        orders = doc.get("orders") or []
        if len(orders) != 1:
            raise PilotError("trader/orders.json should hold exactly one ticket, it holds %d" % len(orders))
        return orders[0]

    # ------------------------------------------------------------ the phone

    def rate(self):
        r = (t212._read_json(self.p("fx.json"), {}) or {}).get("usd_per_gbp")
        return float(r) if r else None

    def ticket_text(self, t):
        o, rv = t["order"], t["risk"]
        plan = o.get("plan") or {}
        rate = self.rate()
        loss = rv.get("max_loss")
        lines = ["DEMO TICKET: BUY %s %s at about $%.2f (market order)" % (o.get("qty"), t["symbol"],
                                                                         float(plan.get("entry") or 0)),
                 "Stop $%.2f, target $%.2f, reward:risk %s" % (float(plan.get("stop") or 0),
                                                               float(plan.get("target") or 0),
                                                               rv.get("reward_risk", "?")),
                 "Most you lose at the stop: about %s ($%.2f)" % (_gbp(loss / rate) if loss and rate else "?",
                                                                 loss or 0),
                 "Time Machine: %s" % t["verdict"],
                 "trader-risk: PASS",
                 "Expires %s (in %d minutes). Nothing is placed unless you tap Approve." % (
                     _uk(t["expires"]), round(self.s["expire_minutes"]))]
        return "\n".join(lines)

    def new_ticket(self, built, status):
        now = _now()
        return {"id": _rand.token_hex(4), "nonce": _rand.token_hex(8), "symbol": built["symbol"],
                "fingerprint": built["fingerprint"], "order": built["order"], "risk": built["risk"],
                "verdict": built["verdict"], "tm": built.get("tm"), "created": now,
                "expires": now + self.s["expire_minutes"] * 60, "status": status, "message_id": None,
                "auto": status == "approving"}

    def auto_buy(self, built):
        """DEMO ONLY: place a ticket that passed every check, with the same checks at placing time as
        an Approve tap (fresh quote, no chase past max_chase_r, trader-risk again: daily loss, trades
        per day, allow and block lists), then tell the phone what was bought or why not."""
        if self.client.env != AUTOPILOT_ENV or not self.s["auto_buy"]:
            raise PilotError("automatic buying runs on the demo account only, with autopilot.auto_buy true")
        t = self.new_ticket(built, "approving")
        t["approved_at"] = _now()
        self.state["tickets"].append(t)
        self.save()
        try:
            self._approve(t, by="auto-buy")
            text = self.bought_text(t)
            if self.s["evidence_mode"]:
                rec = self.placed_record(t)
                evidence.log_trade(self.sd, {"day": _ny().date().isoformat(), "symbol": t["symbol"],
                                             "fingerprint": t["fingerprint"], "t212_ticker": rec.get("t212_ticker"),
                                             "expected_entry": t.get("quote"), "qty": rec.get("qty"),
                                             "stop": rec.get("stop"), "target": rec.get("target")})
        except (PilotError, t212.T212Error) as e:
            text = self.not_placed(t, e)
        self.save()
        self.event(text)
        return t

    def not_placed(self, t, e):
        msg = t212.scrub(e, self.client._secrets)
        t["note"] = msg
        if isinstance(e, t212.T212Error) and e.code == 3:
            t["status"] = "entry placed, stop not placed"
            return "!!! %s BOUGHT BUT STOP NOT PLACED (demo) !!! %s" % (t["symbol"], msg)
        if isinstance(e, t212.T212Error) and "UNCLEAR" in msg:
            t["status"] = "unclear"
            return "!!! %s: UNCLEAR whether it was bought (demo). Check the app. !!! %s" % (t["symbol"], msg)
        t["status"] = "refused"
        return "NOT PLACED (demo): %s passed the checks when built, but at placing time: %s" % (t["symbol"], msg)

    def placed_record(self, t):
        rec = {}
        for r in (t212._read_json(self.p("state", "broker.json"), {}) or {}).get("orders") or []:
            if r.get("fingerprint") == t["fingerprint"] and r.get("status") in ("placed", "stop_placed", "software_stop"):
                rec = r
        return rec

    def bought_text(self, t):
        """What was bought, from the placement record and the position Trading 212 now shows."""
        rec = None
        for r in (t212._read_json(self.p("state", "broker.json"), {}) or {}).get("orders") or []:
            if r.get("fingerprint") == t["fingerprint"] and r.get("status") in ("placed", "stop_placed"):
                rec = r
        rec = rec or {}
        fill = None
        try:
            for p in self.client.get(t212.POSITIONS) or []:
                if t212._o_ticker(p) == rec.get("t212_ticker"):
                    fill = t212._num(p.get("averagePricePaid"))
        except t212.T212Error:
            pass
        tm = t.get("tm") or {}
        odds = ("target first %.1f %%, stop first %.1f %%" % (100 * tm["p_target"], 100 * tm["p_stop"])
                if tm.get("p_target") is not None and tm.get("p_stop") is not None else "no data")
        if tm.get("avg_R_net") is not None:
            odds += "; average %+.2fR after costs (%+.2fR before)" % (tm["avg_R_net"], tm.get("avg_R") or 0)
        stop_how = ("a stop order at Trading 212, good till cancelled" if rec.get("status") == "stop_placed"
                    else "enforced by the guard in software: a market sell at the stop")
        lines = ["BOUGHT (demo, automatic%s): %s %s" % (", EVIDENCE MODE" if self.s["evidence_mode"] else "",
                                                        rec.get("qty") or t["order"].get("qty"), t["symbol"]),
                 "Entry: $%s%s" % (t212._fmt(fill) if fill else t212._fmt(rec.get("entry")),
                                   " (filled)" if fill else " (estimate; not filled yet)"),
                 "Cost: about %s with the FX fee" % _gbp(rec.get("cost_gbp")),
                 "Stop: $%s (%s)" % (t212._fmt(rec.get("stop")), stop_how),
                 "Target: $%s" % t212._fmt(rec.get("target")),
                 "Max loss at the stop: about %s" % _gbp(rec.get("max_loss_gbp")),
                 "Time Machine: %s" % odds,
                 "Reply /pause to stop further buys today."]
        return "\n".join(lines)

    def send_ticket(self, built):
        t = self.new_ticket(built, "pending")
        self.state["tickets"].append(t)
        self.save()
        t["message_id"] = self.phone.send(self.ticket_text(t), [("Approve", "ap:%s:%s" % (t["id"], t["nonce"])),
                                                                ("Reject", "rj:%s:%s" % (t["id"], t["nonce"]))])
        self.save()
        self.say("ticket %s sent: BUY %s %s" % (t["id"], built["order"].get("qty"), built["symbol"]))
        return t

    def expire(self):
        for t in self.state["tickets"]:
            if t["status"] == "pending" and _now() >= t["expires"]:
                t["status"] = "expired"
                self.save()
                self._edit(t, self.ticket_text(t) + "\n\nEXPIRED at %s. Nothing was placed." % _uk(t["expires"]))
                self.say("ticket %s (%s) expired: nothing placed" % (t["id"], t["symbol"]))

    def _edit(self, t, text):
        try:
            self.phone.edit(t.get("message_id"), text)
        except phone_mod.PhoneError as e:
            self.say("could not update the phone message: %s" % e)

    def listen(self, wait):
        """Wait up to `wait` seconds for taps and commands from your phone, and act on them."""
        try:
            ups = self.phone.updates(wait=wait)
        except phone_mod.PhoneError as e:
            self.say("phone poll failed: %s" % e)
            if not net.online():
                self.net_down = True
                return
            _sleep(min(wait, 10))
            return
        for u in ups:
            if u["kind"] == "tap":
                self.tap(u)
            elif u["kind"] == "text":
                self.command(u["text"])

    def command(self, text):
        cmd = text.strip().split()[0].lower() if text.strip() else ""
        if cmd == "/pause":
            self.state["paused"] = True
            self.save()
            self.phone.send("Paused: no new tickets until you send /resume. The guard keeps protecting positions.")
        elif cmd == "/resume":
            self.state["paused"] = False
            self.save()
            self.phone.send("Resumed: the next cycle scans again.")
        elif cmd == "/status":
            self.phone.send(self.status_text())
        else:
            self.phone.send("Commands: /pause, /resume, /status. Tickets are answered with their buttons.")

    def tap(self, u):
        m = re.match(r"^(ap|rj):([0-9a-f]{8}):([0-9a-f]{16})$", u["data"])
        t = None
        if m:
            t = next((x for x in self.state["tickets"] if x["id"] == m.group(2)), None)
        if t is None or not hmac.compare_digest(t["nonce"], m.group(3)):
            self.phone.answer(u["id"], "Unknown ticket: nothing done.")
            return
        if t["status"] != "pending":
            self.phone.answer(u["id"], "Already %s: nothing more done." % t["status"])
            return
        if _now() >= t["expires"]:
            t["status"] = "expired"
            self.save()
            self.phone.answer(u["id"], "Expired: nothing placed.")
            self._edit(t, self.ticket_text(t) + "\n\nEXPIRED. You tapped after %s. Nothing was placed." % _uk(t["expires"]))
            return
        if m.group(1) == "rj":
            t["status"] = "rejected"
            self.save()
            self.phone.answer(u["id"], "Rejected: nothing placed.")
            self._edit(t, self.ticket_text(t) + "\n\nREJECTED by you. Nothing was placed.")
            self.say("ticket %s (%s) rejected" % (t["id"], t["symbol"]))
            return
        t["status"] = "approving"         # a second tap, or a crash and restart, never places twice
        t["approved_at"] = _now()
        self.save()
        self.phone.answer(u["id"], "Approved: checking again, then placing on demo.")
        self.approve(t)

    def approve(self, t):
        try:
            text = self._approve(t)
        except (PilotError, t212.T212Error) as e:
            msg = t212.scrub(e, self.client._secrets)
            if t["status"] == "approving":
                t["status"] = "refused"
            t["note"] = msg
            text = "NOT PLACED: %s" % msg
            if isinstance(e, t212.T212Error) and e.code == 3:
                t["status"] = "entry placed, stop not placed"
                text = "!!! ENTRY PLACED, STOP NOT PLACED !!! %s" % msg
            elif isinstance(e, t212.T212Error) and "UNCLEAR" in msg:
                t["status"] = "unclear"
                text = "!!! UNCLEAR whether it was placed. Check the app. !!! %s" % msg
        self.save()
        self._edit(t, self.ticket_text(t) + "\n\n" + text)
        self.event("%s %s" % (t["symbol"], text))

    def _approve(self, t, by="phone approve"):
        """Fresh quote, chase limit, trader-risk again, then t212.py place with your Approve (or, on demo
        with auto_buy, the autopilot) as the yes."""
        if self.client.env != AUTOPILOT_ENV:
            raise PilotError("demo only")
        o = self.order()
        if tk.fingerprint(o) != t["fingerprint"]:
            raise PilotError("trader/orders.json changed since the ticket was sent")
        plan = o.get("plan") or {}
        entry, stop = float(plan["entry"]), float(plan["stop"])
        self.run(["trader-alerts/alerts.py", "quote", t["symbol"], "--write", self.p("prices.json")], "a quote")
        px = (t212._read_json(self.p("prices.json"), {}) or {}).get("prices", {}).get(t["symbol"])
        if px is None:
            raise PilotError("no fresh quote for %s, so it was not placed" % t["symbol"])
        limit = entry + self.s["max_chase_r"] * (entry - stop)
        if float(px) > limit:
            raise PilotError("%s is at $%.2f, more than %gR above the $%.2f entry (limit $%.2f): too late to chase"
                             % (t["symbol"], float(px), self.s["max_chase_r"], entry, limit))
        if float(px) <= stop:
            raise PilotError("%s is at $%.2f, at or under the $%.2f stop" % (t["symbol"], float(px), stop))
        t["quote"] = float(px)                  # the price expected when the order goes in (slippage report)
        rv = self.risk()
        if rv.get("fingerprint") != t["fingerprint"] or rv.get("verdict") != "PASS":
            raise PilotError("trader-risk no longer passes it: %s" % "; ".join(rv.get("reasons") or ["changed"]))

        def confirm(index, order, sz):
            if tk.fingerprint(order) != t["fingerprint"]:
                return False, "", "not the ticket you approved"
            return True, "%s %s at %s" % (by, t["id"], t212._utc_now().strftime("%H:%M:%SZ")), ""

        args = argparse.Namespace(ticket_id=t["fingerprint"], resend=False, fx_rate=None, state_dir=self.sd,
                                  json=False, live=False)
        buf = io.StringIO()
        with redirect_stdout(buf):
            res = t212.cmd_place(self.client, args, confirm=confirm)
        self.say(buf.getvalue().strip())
        if res is None:
            raise PilotError("place did not send it")
        t["status"] = "placed"
        entry_id = t212._ident((res.get("entry") or {}).get("id"))
        stop_note = ("stop at $%.2f is in, good till cancelled" % stop if res.get("stop")
                     else "the guard enforces the $%.2f stop in software (market sell at the stop)" % stop)
        return "PLACED on demo: BUY %s %s, order id %s; %s" % (o.get("qty"), t["symbol"], entry_id, stop_note)

    # ------------------------------------------------------------ fills, exits, summary

    def watch_positions(self):
        """Compare positions with the last look: more shares is a fill, fewer is an exit."""
        try:
            pos = self.client.get(t212.POSITIONS) or []
        except t212.T212Error as e:
            self.say("positions check failed: %s" % t212.scrub(e, self.client._secrets))
            return
        now = {}
        for p in pos:
            t = t212._o_ticker(p)
            if t:
                now[t] = {"qty": t212._num(p.get("quantity")) or 0, "price": t212._num(p.get("currentPrice")),
                          "avg": t212._num(p.get("averagePricePaid"))}
        before = self.state.get("positions")
        self.state["positions"] = now
        self.save()
        if before is None:
            return
        for t in sorted(set(before) | set(now)):
            b, n = (before.get(t) or {}).get("qty", 0), (now.get(t) or {}).get("qty", 0)
            if n > b + 1e-9:
                self.event("FILLED (demo): bought %s %s at about $%s; you hold %s"
                           % (t212._qty(n - b), t, t212._fmt((now[t] or {}).get("avg")), t212._qty(n)))
            elif n + 1e-9 < b:
                px = (now.get(t) or before.get(t) or {}).get("price")
                self.event("EXIT (demo): sold %s %s near $%s; you hold %s"
                           % (t212._qty(b - n), t, t212._fmt(px), t212._qty(n) if n else "none"))

    def status_text(self):
        st = self.state
        placed = sum(1 for t in st["tickets"] if t["status"] == "placed")
        return ("Autopilot (demo, %s%s) %s: %d passing tickets today, %d placed, %s pending%s. %d tickers allowed." % (
            "buys automatically" if self.s["auto_buy"] else "asks for Approve",
            ", EVIDENCE MODE forward test" if self.s["evidence_mode"] else "", st["day"], len(st["tickets"]),
            placed, len(self.pending()), ", PAUSED" if st.get("paused") else "",
            len(t212.allowed_tickers(self.cfg) or [])) + self.status_extra())

    def status_extra(self):
        alerts = mod("trader-alerts", "alerts")
        m = alerts.market_status(_utc())
        lines = ["", "Market: %s%s" % ("OPEN" if m.get("open") else "closed (%s)" % (m.get("reason") or "closed"),
                                       "" if m.get("open") else "; next open %s New York" % m.get("next_open", "?")),
                 "Guard: %s" % _guard_state(self.sd)[1], "Keep-awake: %s" % _keep_awake()[1], self.budget_line()]
        if self.s["evidence_mode"]:
            lines.append("EVIDENCE MODE is ON: %s" % self.evidence_day())
        return "\n".join(lines)

    def record_day(self):
        """Start and close value of the account for today, for the morning briefing's result."""
        try:
            close = t212._num((self.client.get(t212.SUMMARY) or {}).get("totalValue"))
        except t212.T212Error:
            return
        path = self.p("state", "account-days.json")
        doc = t212._read_json(path, {}) or {}
        doc.setdefault("days", {})[self.state["day"]] = {"start": self.state.get("start_value"), "close": close}
        t212._write_json(path, doc)

    def send_summary(self):
        """The closing report (trader-broker/report.py), and today's value for the morning briefing."""
        self.record_day()
        report = mod("trader-broker", "report")
        report.t212, report.tk, report.phone_mod = t212, tk, phone_mod
        report._now = lambda: _now()              # the autopilot's clock
        try:
            ok, path, _ = report.send(self.client, self.sd, phone=self.phone)
            self.say("closing report %s (%s)" % ("sent" if ok else "NOT sent", path))
        except Exception as e:
            phone_mod.push(self.sd, "CLOSING REPORT (demo): could not be built (%s)"
                           % t212.scrub(e, self.client._secrets), phone=self.phone)
        self.state["summary_sent"] = True
        self.save()
        if self.s["evidence_mode"]:
            self.evidence_close(report)

    def evidence_close(self, report):
        """After the closing report: today's virtual results, and on Fridays (or the last day) the report."""
        try:
            n = evidence.resolve(self.sd)
            self.say("evidence: %d virtual results filled in" % n)
            ny = _ny()
            if evidence.due(self.cfg, ny.date().isoformat(), ny.weekday()):
                ok, _, r = evidence.send_report(self.client, self.sd, self.cfg, report, phone=self.phone)
                self.say("evidence report %s: %s" % ("sent" if ok else "NOT sent", r["status"]))
        except Exception as e:
            phone_mod.push(self.sd, "EVIDENCE REPORT (demo): could not be built (%s)"
                           % t212.scrub(e, self.client._secrets), phone=self.phone)

    # ------------------------------------------------------------ no internet

    def offline(self, next_cycle, step, last, summary_at):
        """Check every 2 minutes. Back within 30 minutes: carry on with the next scheduled cycle. Longer:
        buying is paused for the rest of the day (nothing is bought blind), and it keeps trying for the
        closing report. Once back, the phone is told what was missed. Returns the next cycle time."""
        self.net_down = False
        deadline = _now() + (summary_at - _ny()).total_seconds()
        w = net.wait_online(_sleep, _now, deadline=deadline, log=self.say, label="the autopilot")
        start = w["start"]
        paused = False
        if not w["ok"] and w["gave_up"]:
            paused = True
            self.state["paused"] = True
            self.state["paused_why"] = "no internet for %d minutes" % net.minutes(start, _now())
            self.save()
            phone_mod.push(self.sd, "AUTOPILOT OFFLINE for %d minutes: buying is PAUSED for the rest of today. "
                                    "The guard is separate and keeps protecting positions when it can reach "
                                    "Trading 212." % net.minutes(start, _now()), phone=self.phone)
            w = net.wait_online(_sleep, _now, limit=max(0.0, deadline - _now()), deadline=deadline,
                                log=self.say, label="the autopilot")
        end = _now()
        slots, nc = [], next_cycle
        while nc <= _ny() and nc <= last:
            slots.append(nc.strftime("%H:%M"))
            nc += step
        missed = []
        if slots:
            missed.append("%d scan cycle%s (%s New York), so no tickets then" % (
                len(slots), "" if len(slots) == 1 else "s", ", ".join(slots)))
        extra = ["Fills and exits during the outage are reported next, from the positions."]
        if paused:
            extra.append("Buying is PAUSED for the rest of today because the outage lasted %d minutes or more. "
                         "Send /resume to start again." % (net.GIVE_UP_AFTER // 60))
        net.record(self.sd, "autopilot", start, end, missed, gave_up=paused)
        self.state["events"].append({"at": _ny().strftime("%H:%M"),
                                     "text": "offline %d min; missed %s" % (net.minutes(start, end), "; ".join(missed) or "nothing")})
        self.save()
        if w["ok"]:
            phone_mod.push(self.sd, net.back_online("The autopilot (demo)", start, end, missed, extra),
                           phone=self.phone)
        return nc

    # ------------------------------------------------------------ always on: the phone at any hour

    def flush_phone(self):
        """Send what waited in the phone outbox, when there is any and the phone answers."""
        try:
            if (t212._read_json(phone_mod.outbox_path(self.sd), {}) or {}).get("items"):
                phone_mod.flush_outbox(self.phone, self.sd)
        except Exception as e:
            self.say("outbox not sent yet: %s" % e)

    def roll_day(self):
        if self.state.get("day") != _ny().date().isoformat():
            self.state = self._load_state()
            self.save()
            self.cfg = self.read_cfg()
            self.s = settings(self.cfg)

    def catch_up_briefing(self):
        """On a trading day, from 07:45 UK until the open: if the morning briefing has not started (the Mac was
        asleep at 07:30, say) or started over 40 minutes ago and never got through, run it. Once a day."""
        alerts = mod("trader-alerts", "alerts")
        ny = _ny()
        today = ny.date().isoformat()
        if not alerts.trading_day(ny.date())[0] or self.state.get("briefing_catchup") == today:
            return
        local = _local(_now())
        if (local.hour, local.minute) < BRIEFING_CATCHUP or (ny.hour, ny.minute) >= (9, 30):
            return
        mark = t212._read_json(self.p("state", "briefing.json"), {}) or {}
        if mark.get("sent") == today:
            return
        if mark.get("started") == today and _now() - float(mark.get("started_at") or 0) < 40 * 60:
            return
        self.state["briefing_catchup"] = today
        self.save()
        _spawn([sys.executable, os.path.join(HERE, "briefing.py"), "--state-dir", self.sd],
               os.path.join(self.sd, "logs", "briefing.log"))
        self.say("the morning briefing had not reached the phone: started it again")

    def apply_budget(self):
        """account_budget: today's budget and loss limits from the account value, once a trading day."""
        ab = t212.budget_module()
        today = _ny().date().isoformat()
        try:
            if not ab.settings(self.cfg) or self.state.get("budget_day") == today:
                return
            value = t212._num((self.client.get(t212.SUMMARY) or {}).get("totalValue"))
            r, changed = ab.apply(value, self.p("config.json"), today)
        except (ab.BudgetError, t212.T212Error) as e:
            phone_mod.push(self.sd, "BUDGET NOT UPDATED (demo): %s. Yesterday's budget and limits stay."
                           % t212.scrub(e, self.client._secrets), phone=self.phone, once_key="budget-" + today)
            return
        self.cfg = self.read_cfg()
        self.s = settings(self.cfg)
        self.state["budget_day"] = today
        self.save()
        self.say("budget %s: %s" % ("updated" if changed else "already set today", ab.plain(r).replace("\n", ";")))

    def budget_line(self):
        c = self.cfg
        last = ((c.get("account_budget") or {}).get("last") or {})
        f = last.get("figures") or {}
        if (c.get("account_budget") or {}).get("enabled") and f:
            return ("Budget (from the GBP {:,.2f} account on {}): invest up to GBP {:,.2f}, keep GBP {:,.2f} cash; "
                    "max loss GBP {:,.2f} a trade, GBP {:,.2f} a day{}".format(
                        f["account_value"], last.get("date"), f["invest_gbp"], f["cash_gbp"],
                        f["max_loss_per_trade_gbp"], f["max_daily_loss_gbp"],
                        " (capped by max_budget_gbp)" if f.get("capped") else ""))
        b = c.get("budget_gbp")
        return "Budget (fixed): GBP {}, cash buffer {}%, max loss GBP {} a trade, GBP {} a day".format(
            b, c.get("cash_buffer_pct"), c.get("max_loss_per_trade_gbp"), c.get("max_daily_loss_gbp"))

    def open_message(self, first=None, last=None):
        """The confirmation at the open: guard and autopilot running, positions and their stops, evidence mode."""
        ny = _ny()
        g_ok, g_line = _guard_state(self.sd)
        aj = _launchd(AUTOPILOT_LABEL)
        lines = ["MARKET OPEN (demo) %s New York" % ny.strftime("%a %d %b %H:%M"),
                 "Guard: %s" % g_line,
                 "Autopilot: %s, %s%s" % (
                     ("running, always on (pid %s)" % aj["pid"]) if aj["running"] else
                     "!!! the always-on service is NOT RUNNING (this message is from pid %d) !!!" % os.getpid(),
                     "buys automatically" if self.s["auto_buy"] else "asks for Approve",
                     ", PAUSED (send /resume)" if self.state.get("paused") else "")]
        if self.s["evidence_mode"]:
            lines.append("EVIDENCE MODE is ON: %s" % self.evidence_day())
        lines.append(self.budget_line())
        lines += self.positions_lines()
        if first and last:
            lines.append("Checks every %d min from %s to %s New York. /pause stops new buys." % (
                self.s["every_minutes"], first.strftime("%H:%M"), last.strftime("%H:%M")))
        return "\n".join(lines)

    def evidence_day(self):
        alerts = mod("trader-alerts", "alerts")
        try:
            start, end = evidence.window(self.cfg)
        except evidence.EvidenceError as e:
            return "but %s" % e
        d0, d1, today = (datetime.date.fromisoformat(x) for x in (start, end, _ny().date().isoformat()))
        days, n, d = [], 0, d0
        while d <= d1:
            if alerts.trading_day(d)[0]:
                days.append(d)
            d += datetime.timedelta(days=1)
        n = sum(1 for x in days if x <= today)
        return "forward test %s to %s, trading day %d of %d" % (start, end, n, len(days))

    def positions_lines(self):
        try:
            pos = [p for p in self.client.get(t212.POSITIONS) or [] if (t212._num(p.get("quantity")) or 0) > 0]
            pending = self.client.get(t212.ORDERS) or []
        except t212.T212Error as e:
            return ["Positions: no data (%s)" % t212.scrub(e, self.client._secrets)]
        if not pos:
            return ["Open positions: none."]
        soft = (t212._read_json(self.p("state", "t212-guard.json"), {}) or {}).get("software") or {}
        out = ["Open positions:"]
        for p in pos:
            ticker = t212._o_ticker(p)
            qty = t212._num(p.get("quantity")) or 0
            stops, _ = t212.guard_sell_orders(pending, ticker)
            covered = sum(abs(t212._num(o.get("quantity")) or 0) for o in stops)
            if stops and covered + 1e-9 >= qty:
                how = "stop $%s (stop order, %s)" % (t212._fmt(stops[0].get("stopPrice")), stops[0].get("status", "?"))
            elif soft.get(ticker):
                how = "software stop $%s (the guard sells at it)" % t212._fmt(soft[ticker].get("stop"))
            elif stops:
                how = "!!! stop covers only %s of %s !!!" % (t212._qty(covered), t212._qty(qty))
            else:
                how = "!!! NO STOP !!!"
            out.append("  %s %s at $%s: %s" % (ticker, t212._qty(qty), t212._fmt(p.get("currentPrice")), how))
        return out

    def serve(self, max_loops=None):
        """Always on (launchd keeps it alive): answer /status, /pause and /resume at any hour, deliver the outbox,
        catch up a missed briefing, and run each trading day."""
        alerts = mod("trader-alerts", "alerts")
        self.say("always on: the phone gets an answer at any hour")
        n = 0
        while max_loops is None or n < max_loops:
            n += 1
            try:
                self.roll_day()
                self.flush_phone()
                self.catch_up_briefing()
                st = alerts.market_status(_utc())
                if st.get("opens") and not self.state.get("summary_sent"):
                    self.day()
                    continue
                if getattr(self, "net_down", False):
                    self.net_down = False
                    _sleep(60)
                    continue
                self.listen(POLL_WAIT)
            except Exception as e:
                self.say("serve: %s; carrying on" % t212.scrub(e, self.client._secrets))
                _sleep(30)

    # ------------------------------------------------------------ the day

    def day(self):
        """Wait for the open, run the cycles, watch positions, answer taps, then the summary. One day."""
        alerts = mod("trader-alerts", "alerts")
        st = alerts.market_status(_utc())
        if not st.get("opens"):
            self.say("market closed today (%s): nothing to do" % (st.get("reason") or "closed"))
            return "closed"
        o_h, o_m = _hm(st["opens"], "09:30")
        c_h, c_m = _hm(st["closes"], "16:00")
        b_h, b_m = _hm(self.cfg.get("no_entries_before"), "09:45")
        a_h, a_m = _hm(self.cfg.get("no_entries_after"), "15:30")
        ny = _ny()
        open_at = ny.replace(hour=o_h, minute=o_m, second=0)
        close_at = ny.replace(hour=c_h, minute=c_m, second=0)
        first = ny.replace(hour=b_h, minute=b_m, second=0)
        last = min(ny.replace(hour=a_h, minute=a_m, second=0), close_at)
        e_h, e_m = _hm((self.cfg.get("eod_close") or {}).get("at"), EOD_DEFAULT)
        summary_at = min(ny.replace(hour=e_h, minute=e_m, second=0), close_at) + datetime.timedelta(minutes=REPORT_AFTER)
        if ny >= summary_at:
            if not self.state.get("summary_sent"):
                self.send_summary()
            return "after the close"
        self.apply_budget()
        if ny < open_at:
            self.say("waiting for the open (%s New York); answering the phone meanwhile" % st["opens"])
            while _ny() < open_at:
                self.flush_phone()
                self.catch_up_briefing()
                if getattr(self, "net_down", False):
                    self.net_down = False
                    _sleep(min(60.0, max(1.0, (open_at - _ny()).total_seconds())))
                    continue
                self.listen(int(max(1, min(POLL_WAIT, (open_at - _ny()).total_seconds() + 5))))
        self.cfg = self.read_cfg()
        if self.state.get("start_value") is None:
            try:
                self.state["start_value"] = t212._num((self.client.get(t212.SUMMARY) or {}).get("totalValue"))
            except t212.T212Error:
                pass
            self.save()
        if self.state.get("open_sent") != self.state["day"]:
            phone_mod.push(self.sd, self.open_message(first, last), phone=self.phone)
            self.state["open_sent"] = self.state["day"]
            self.save()
        step = datetime.timedelta(minutes=self.s["every_minutes"])
        next_cycle = first
        last_pos = 0
        while True:
            ny = _ny()
            if ny >= summary_at:
                break
            self.expire()
            if getattr(self, "net_down", False):
                next_cycle = self.offline(next_cycle, step, last, summary_at)
                continue
            if ny >= next_cycle and next_cycle <= last:
                try:
                    self.say("cycle: " + self.cycle())
                except (PilotError, t212.T212Error) as e:
                    self.say("cycle failed: %s" % t212.scrub(e, self.client._secrets))
                    if getattr(e, "network", False) and not net.online():
                        self.net_down = True
                while next_cycle <= ny:
                    next_cycle += step
            if _now() - last_pos >= POSITIONS_EVERY and ny < close_at + datetime.timedelta(minutes=2):
                self.watch_positions()
                last_pos = _now()
            self.listen(POLL_WAIT)
        self.expire()
        if not self.state.get("summary_sent"):
            self.send_summary()
        return "done"


# ---------------------------------------------------------------- command line

def make_client(state_dir, environ):
    env = t212.pick_env(False, environ)          # T212_ENV=live without --live is refused here
    if env != AUTOPILOT_ENV:
        raise PilotError("demo only")
    key, secret = t212.credentials(environ)
    c = t212.Client(env, key, secret, state_dir=state_dir)
    c.environ = environ
    return c


def main(argv=None, environ=None):
    ap = argparse.ArgumentParser(description="Autopilot (demo only): tickets to your phone, placed on your Approve.")
    ap.add_argument("--state-dir", default=os.path.join(ROOT, "trader"))
    sub = ap.add_subparsers(dest="cmd")
    sub.required = True
    sub.add_parser("run", help="the trading day: wait for the open, cycles, taps, fills, summary")
    sub.add_parser("cycle", help="one cycle now, then wait for your tap until the ticket expires")
    sub.add_parser("report", help="send today's closing report to your phone now")
    sub.add_parser("summary", help="the same as report")
    s = sub.add_parser("status", help="what it has done today")
    s.add_argument("--send", action="store_true", help="also send it to the phone")
    o = sub.add_parser("open-check", help="the market-open confirmation, now")
    o.add_argument("--send", action="store_true", help="send it to the phone")
    sub.add_parser("serve", help="always on: answer the phone at any hour and run each trading day (launchd)")
    a = ap.parse_args(argv)
    environ = os.environ if environ is None else environ
    client = None
    try:
        client = make_client(a.state_dir, environ)
        phone = phone_mod.open_phone(a.state_dir, environ)
        cfg = tk.read_config(os.path.join(a.state_dir, "config.json"))
        problems = check_ready(cfg, client.env, phone)
        if problems and a.cmd == "serve" and phone is not None:
            return serve_unready(a.state_dir, client, phone, problems, environ)
        if problems:
            raise PilotError("not starting:\n  - " + "\n  - ".join(problems))
        pilot = Pilot(client, phone, a.state_dir)
        if a.cmd == "serve":
            pilot.serve()
            return 0
        if a.cmd == "open-check":
            text = "TEST, not the real open:\n" + pilot.open_message()
            print(text)
            if a.send:
                pilot.phone.send(text)
            return 0
        if a.cmd == "status":
            print(pilot.status_text())
            if a.send:
                pilot.phone.send(pilot.status_text())
            print("keep-awake: %s; %s" % (_keep_awake()[1], _on_mains()[1]))
            if not _on_mains()[0]:
                print("WARNING: %s" % _on_mains()[1])
            for t in pilot.state["tickets"]:
                print("  %s %s %s: %s" % (t["id"], t["symbol"], t["order"].get("qty"), t["status"]))
            return 0
        if a.cmd in ("report", "summary"):
            pilot.send_summary()
            return 0
        if a.cmd == "cycle":
            pilot.say("cycle: " + pilot.cycle())
            while pilot.pending():
                pilot.listen(POLL_WAIT)
                pilot.expire()
            return 0
        pilot.day()
        return 0
    except (PilotError, t212.T212Error, tk.TicketError, phone_mod.PhoneError) as e:
        msg = t212.scrub(e, client._secrets) if client else str(e)
        sys.stderr.write("autopilot: %s\n" % msg)
        phone_mod.push(a.state_dir, "AUTOPILOT STOPPED (demo): %s" % msg, environ=environ)
        return 2


def serve_unready(state_dir, client, phone, problems, environ, max_loops=None):
    """Not ready to trade, but still on: answer every command with the reason, re-check every 5 minutes,
    and say so on the phone once a day (not on every launchd restart)."""
    why = "Autopilot NOT RUNNING (demo):\n  - " + "\n  - ".join(problems)
    sys.stderr.write("autopilot: %s\n" % why)
    mark = os.path.join(state_dir, "state", "autopilot-unready.json")
    today = _ny().date().isoformat()
    if (t212._read_json(mark, {}) or {}).get("day") != today:
        phone_mod.push(state_dir, why, phone=phone)
        t212._write_json(mark, {"day": today})
    started, n = _now(), 0
    while (max_loops is None or n < max_loops) and _now() - started < 300:
        n += 1
        try:
            for u in phone.updates(wait=POLL_WAIT):
                if u["kind"] == "text":
                    phone.send(why + "\nFix it, and it starts within 5 minutes.")
                elif u["kind"] == "tap":
                    phone.answer(u["id"], "The autopilot is not running: nothing done.")
        except phone_mod.PhoneError:
            _sleep(60)
    return 3                                     # launchd starts it again: the checks run afresh


if __name__ == "__main__":
    sys.exit(main())
