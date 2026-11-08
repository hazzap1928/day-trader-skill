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
            "max_tickets_per_day": 10}
FLAGS = {"auto_buy": False}      # true: a ticket that passes every check is placed without a tap (demo only)
POSITIONS_EVERY = 60             # seconds between fill and exit checks
POLL_WAIT = 20                   # seconds a phone poll waits for a tap
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
    return s


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
    if (cfg.get("autopilot") or {}).get("auto_buy") is True and client_env != AUTOPILOT_ENV:
        problems.append("autopilot.auto_buy works on the demo account only; there is no live automatic buying")
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
            got = self.build(sym, cfgp, fx)
            self.state["tried"][sym] = got if isinstance(got, str) else "passed every check"
            self.save()
            if isinstance(got, str):
                notes.append("%s: %s" % (sym, got))
                continue
            if self.s["auto_buy"]:
                t = self.auto_buy(got)
                return "%s: %s" % (sym, t["status"])
            self.send_ticket(got)
            return "%s: ticket sent to your phone" % sym
        return "no ticket: " + ("; ".join(notes) if notes else "nothing triggered that was not tried today")

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
        """entry -> exit -> Time Machine -> budget -> risk for one symbol. A ticket dict, or why not."""
        flat = self.cfg.get("flat_by") or "15:55"
        self.run(["trader-entry/entry.py", "check", sym, "--config", cfgp, "--write", self.p("entry.json")],
                 "trader-entry check")
        code, out, err = self.run(["trader-exit/exit.py", "plan", "--from", self.p("entry.json"), "--config", cfgp,
                                   "--write", self.p("plan.json")], "trader-exit")
        if code != 0:
            return "no exit plan (%s)" % (err or out).strip()[-160:]
        code, out, err = self.run(["trader-timemachine/timemachine.py", "run", "--plan", self.p("plan.json"),
                                   "--until", flat, "--write", self.p("timemachine.json"), "--json"],
                                  "the Time Machine")
        if code != 0:
            return "Time Machine could not run (%s)" % (err or out).strip()[-160:]
        doc = t212._read_json(self.p("timemachine.json"), {}) or {}
        lean = mod("trader-timemachine", "timemachine").lean(doc.get("mc") or {}) if doc.get("mc") else None
        if lean is None or lean[0] <= self.s["min_edge"]:
            return "Time Machine: %s" % (doc.get("verdict") or "no edge")
        self.run(["trader-alerts/alerts.py", "quote", sym, "--write", self.p("prices.json")], "a quote")
        code, out, err = self.run(["trader-budget/size.py", "--plan", self.p("plan.json"), "--config", cfgp,
                                   "--fx", fx, "--prices", self.p("prices.json"),
                                   "--portfolio", self.p("portfolio.json"), "--write", self.p("orders.json")],
                                  "trader-budget")
        if code != 0:
            return "trader-budget made no order (%s)" % (err or out).strip()[-160:]
        verdict = self.risk()
        if verdict.get("verdict") != "PASS":
            return "trader-risk BLOCKED: %s" % "; ".join(verdict.get("reasons") or ["?"])
        order = self.order()
        mc = doc.get("mc") or {}
        return {"symbol": sym, "fingerprint": verdict["fingerprint"], "order": order, "risk": verdict,
                "verdict": doc.get("verdict") or "",
                "tm": {"p_target": mc.get("p_target"), "p_stop": mc.get("p_stop"), "verdict": doc.get("verdict") or ""}}

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
        stop_how = ("a stop order at Trading 212, good till cancelled" if rec.get("status") == "stop_placed"
                    else "enforced by the guard (stop_mode market)")
        lines = ["BOUGHT (demo, automatic): %s %s" % (rec.get("qty") or t["order"].get("qty"), t["symbol"]),
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
                     else "the guard enforces the $%.2f stop (stop_mode market)" % stop)
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
        return ("Autopilot (demo, %s) %s: %d passing tickets today, %d placed, %s pending%s. %d tickers allowed." % (
            "buys automatically" if self.s["auto_buy"] else "asks for Approve", st["day"], len(st["tickets"]),
            placed, len(self.pending()), ", PAUSED" if st.get("paused") else "",
            len(t212.allowed_tickers(self.cfg) or [])))

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
        if ny < open_at:
            self.say("waiting for the open (%s New York)" % st["opens"])
            _sleep((open_at - ny).total_seconds() + 5)
        self.cfg = self.read_cfg()
        if self.state.get("start_value") is None:
            try:
                self.state["start_value"] = t212._num((self.client.get(t212.SUMMARY) or {}).get("totalValue"))
            except t212.T212Error:
                pass
            self.save()
        phone_mod.push(self.sd, "Autopilot started (demo). It %s, checking every %d min from %s to %s New York, "
                                "from your %d allowed tickers. Reply /pause to stop new %s."
                       % ("BUYS AUTOMATICALLY what passes every check" if self.s["auto_buy"]
                          else "sends tickets for your Approve", self.s["every_minutes"], first.strftime("%H:%M"),
                          last.strftime("%H:%M"), len(t212.allowed_tickers(self.cfg) or []),
                          "buys" if self.s["auto_buy"] else "tickets"), phone=self.phone)
        step = datetime.timedelta(minutes=self.s["every_minutes"])
        next_cycle = first
        last_pos = 0
        while True:
            ny = _ny()
            if ny >= summary_at:
                break
            self.expire()
            if ny >= next_cycle and next_cycle <= last:
                try:
                    self.say("cycle: " + self.cycle())
                except (PilotError, t212.T212Error) as e:
                    self.say("cycle failed: %s" % t212.scrub(e, self.client._secrets))
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
    sub.add_parser("status", help="what it has done today")
    a = ap.parse_args(argv)
    environ = os.environ if environ is None else environ
    client = None
    try:
        client = make_client(a.state_dir, environ)
        phone = phone_mod.open_phone(a.state_dir, environ)
        cfg = tk.read_config(os.path.join(a.state_dir, "config.json"))
        problems = check_ready(cfg, client.env, phone)
        if problems:
            raise PilotError("not starting:\n  - " + "\n  - ".join(problems))
        pilot = Pilot(client, phone, a.state_dir)
        if a.cmd == "status":
            print(pilot.status_text())
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


if __name__ == "__main__":
    sys.exit(main())
