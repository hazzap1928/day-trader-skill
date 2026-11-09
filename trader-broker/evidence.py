#!/usr/bin/env python3
"""Evidence mode: a 2-week demo forward test of the day-trading autopilot.

With "autopilot": {"evidence_mode": true} (demo only; live refuses it):
  - the autopilot skips its cost block (min_net_r) and trader-risk uses a 1.0 after-costs
    reward:risk minimum, so trades are placed. Every other limit stays: trades a day, the
    daily loss limit, stops, the guard and the end-of-day close.
  - every signal the scanner triggers is logged, traded or not, with its entry, stop, target and
    cost-adjusted expectation, and after the close what the price did (a virtual result after costs).
  - real trades record the price expected when the order went in, so the report can measure
    slippage against the fills Trading 212 reports.
  - Kronos (trader-kronos/, shadow mode) gives a verdict on each signal. It never blocks anything.
  - Each Friday after the close, a report goes to the phone.

Pass, after the window in "evidence": {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}: a positive
average per real trade after costs AND a better return than an S&P 500 tracker over the same days.

    python3 trader-broker/evidence.py report      # build the report now (also sends it with --send)
    python3 trader-broker/evidence.py resolve     # fill in today's virtual results now

Demo only. Read-only except for its own log files. Education and tooling, not financial advice.
"""

import argparse
import datetime
import importlib.util
import json
import os
import statistics as st
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
KRONOS_DIR = os.path.join(ROOT, "trader-kronos")
KRONOS_PY = os.path.join(KRONOS_DIR, ".venv", "bin", "python")
KRONOS_SCRIPT = os.path.join(KRONOS_DIR, "kronos_shadow.py")
KRONOS_TIMEOUT = 240
FEE, SLIP = 0.0015, 0.0005           # the GBP account: FX fee and assumed slippage, each fill
TRACKER = "SPY"                      # stands in for the GBP-listed S&P 500 tracker (no FX fee)
TRACKER_COST = 0.0005                # slippage on each tracker leg
EOD = (15, 50)                       # virtual results close at the end-of-day close, New York time
CHART = "https://query1.finance.yahoo.com/v8/finance/chart/%s?range=%s&interval=%s"
UA = "Mozilla/5.0 (compatible; day-trader-skill/2.0)"


class EvidenceError(Exception):
    pass


def _load(folder, name):
    spec = importlib.util.spec_from_file_location("evidence_" + name, os.path.join(ROOT, folder, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_T212 = []


def t212():
    if not _T212:
        _T212.append(_load("trader-broker", "t212"))
    return _T212[0]


def _now():
    return time.time()


# ---------------------------------------------------------------- the setting

def on(cfg):
    return ((cfg or {}).get("autopilot") or {}).get("evidence_mode") is True


def require_demo(cfg, env):
    """Evidence mode places trades a cost check would block: demo money only."""
    if on(cfg) and (env != "demo" or (cfg or {}).get("paper") is not True or (cfg or {}).get("live_enabled")):
        raise EvidenceError("evidence mode is demo only: turn off autopilot.evidence_mode before anything touches "
                            "a live account")


def window(cfg):
    w = (cfg or {}).get("evidence") or {}
    start, end = w.get("start"), w.get("end")
    for name, v in (("start", start), ("end", end)):
        try:
            datetime.date.fromisoformat(str(v))
        except ValueError:
            raise EvidenceError("evidence.%s in trader/config.json must be a date like 2026-11-10" % name)
    if end < start:
        raise EvidenceError("evidence.end is before evidence.start")
    return start, end


# ---------------------------------------------------------------- files

def _dir(sd):
    d = os.path.join(sd, "state", "evidence")
    os.makedirs(d, exist_ok=True)
    return d


def _path(sd, name):
    return os.path.join(_dir(sd), name + ".jsonl")


def load(sd, name):
    p = _path(sd, name)
    if not os.path.exists(p):
        return []
    out = []
    with open(p, encoding="utf-8") as fh:
        for line in fh:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def _save(sd, name, rows):
    p = _path(sd, name)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, sort_keys=True) + "\n")
    os.replace(tmp, p)


def append(sd, name, rec):
    with open(_path(sd, name), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, sort_keys=True) + "\n")


def log_signal(sd, rec):
    """One scanner signal, traded or not. Returns its id."""
    rec = dict(rec)
    rec.setdefault("at", _now())
    rec["id"] = rec.get("id") or "%s-%s-%d" % (rec.get("day", ""), rec.get("symbol", ""), int(rec["at"]))
    append(sd, "signals", rec)
    return rec["id"]


def update_signal(sd, sig_id, **fields):
    rows = load(sd, "signals")
    for r in rows:
        if r.get("id") == sig_id:
            r.update(fields)
    _save(sd, "signals", rows)


def log_trade(sd, rec):
    """A real order the autopilot placed: the expected entry price, for the slippage report."""
    append(sd, "trades", dict(rec, at=rec.get("at") or _now()))


# ---------------------------------------------------------------- virtual results

def fetch_bars(symbol, rng="5d", interval="5m"):
    """[(utc timestamp, open, high, low, close)] from Yahoo (unofficial, can lag). Raises on failure."""
    req = urllib.request.Request(CHART % (symbol, rng, interval), headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        res = json.loads(r.read().decode("utf-8"))["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    return [(t, o, h, l, c) for t, o, h, l, c in zip(res["timestamp"], q["open"], q["high"], q["low"], q["close"])
            if None not in (o, h, l, c)]


def _ny(ts):
    return t212().ticket_module().to_eastern(datetime.datetime.utcfromtimestamp(ts))


def virtual_result(bars, at, entry_plan, stop, target, k=FEE + SLIP):
    """What a market buy at the first bar after `at` would have done: stop first (a gap opens under it),
    target first, else out at the 15:50 close. Stop and target in the same bar count as the stop.
    Returns {"entry", "exit", "how", "R", "R_net", "ret_net"} or None when there is no bar after `at`."""
    day = _ny(at).date()
    todays = [b for b in bars if _ny(b[0]).date() == day]
    after = [b for b in todays if b[0] >= at]
    end = [b for b in todays if (_ny(b[0]).hour, _ny(b[0]).minute) < EOD]
    if not after or not end:
        return None
    entry = after[0][1]
    risk = entry - stop
    if risk <= 0:
        return {"entry": entry, "exit": None, "how": "opened under the stop", "R": None, "R_net": None, "ret_net": None}
    exit_, how = None, None
    for b in after:
        if (_ny(b[0]).hour, _ny(b[0]).minute) >= EOD:
            break
        o, h, l, c = b[1:]
        if l <= stop:
            exit_, how = (min(stop, o) if b is not after[0] else stop), "stop"
            break
        if h >= target:
            exit_, how = target, "target"
            break
        exit_, how = c, "15:50 close"
    if exit_ is None:
        return None
    R = (exit_ - entry) / risk
    cost = k * (entry + exit_)
    return {"entry": round(entry, 4), "exit": round(exit_, 4), "how": how, "R": round(R, 3),
            "R_net": round(R - cost / risk, 3), "ret_net": round(exit_ * (1 - k) / (entry * (1 + k)) - 1, 5)}


def resolve(sd, day=None, fetch=None):
    """Fill in the virtual result of every logged signal of `day` (default today, New York) that has none."""
    fetch = fetch or fetch_bars
    day = day or _ny(_now()).date().isoformat()
    rows = load(sd, "signals")
    cache, done = {}, 0
    for r in rows:
        if r.get("day") != day or r.get("virtual") or r.get("entry") is None or r.get("stop") is None \
                or r.get("target") is None:
            continue
        try:
            if r["symbol"] not in cache:
                cache[r["symbol"]] = fetch(r["symbol"])
            v = virtual_result(cache[r["symbol"]], r["at"], r["entry"], r["stop"], r["target"])
        except Exception as e:
            v = {"error": str(e)[:120]}
        if v is not None:
            r["virtual"] = v
            done += 1
    _save(sd, "signals", rows)
    return done


# ---------------------------------------------------------------- Kronos, shadow only

def kronos_available():
    return os.path.exists(KRONOS_PY) and os.path.exists(KRONOS_SCRIPT)


def kronos_verdict(sig, run=None, timeout=KRONOS_TIMEOUT):
    """Ask trader-kronos/kronos_shadow.py (its own Python 3.10+ environment) about one signal. Never raises:
    any failure is a verdict of "unavailable" or "error", and nothing ever waits on it to place a trade."""
    if run is None and not kronos_available():
        return {"verdict": "unavailable", "why": "trader-kronos is not set up (see trader-kronos/README.md)"}
    payload = json.dumps({k: sig.get(k) for k in ("symbol", "at", "entry", "stop", "target")})
    try:
        if run is not None:
            out = run(payload)
        else:
            p = subprocess.run([KRONOS_PY, KRONOS_SCRIPT], input=payload, capture_output=True, text=True,
                               timeout=timeout, cwd=KRONOS_DIR)
            if p.returncode != 0:
                return {"verdict": "error", "why": (p.stderr or p.stdout).strip()[-200:]}
            out = p.stdout
        res = json.loads(out)
    except subprocess.TimeoutExpired:
        return {"verdict": "error", "why": "timed out after %ds" % timeout}
    except Exception as e:
        return {"verdict": "error", "why": str(e)[:200]}
    if res.get("avg_R_net") is None:
        return dict(res, verdict="error")
    res["verdict"] = "agree" if res["avg_R_net"] > 0 else "disagree"
    return res


# ---------------------------------------------------------------- the report

def _mean(xs):
    return st.mean(xs) if xs else None


def _pct(x):
    return "no data" if x is None else "%+.2f%%" % (100 * x)


def _gbp(x):
    return "no data" if x is None else ("+" if x >= 0 else "-") + "GBP {:,.2f}".format(abs(x))


def tracker_return(start, end_day, fetch=None):
    """The S&P 500 tracker from the open on `start` to the latest close up to `end_day`, slippage in."""
    fetch = fetch or (lambda s: fetch_bars(s, "3mo", "1d"))
    bars = fetch(TRACKER)
    days = [(datetime.datetime.utcfromtimestamp(b[0]).date().isoformat(), b) for b in bars]
    inside = [b for d, b in days if start <= d <= end_day]
    if not inside:
        return None
    return inside[-1][4] * (1 - TRACKER_COST) / (inside[0][1] * (1 + TRACKER_COST)) - 1


def slippage(trades, fills, guard_state):
    """Adverse slippage, as a fraction: entries against the price expected when the order went in;
    stop exits against the stop price; the guard's market sells against the price it saw."""
    entries, exits = [], []
    used = set()
    for t in trades:
        best = None
        for f in fills:
            if f["side"] == "BUY" and f["ticker"] == t.get("t212_ticker") and f["order_id"] not in used \
                    and f["at"] >= datetime.datetime.utcfromtimestamp(t["at"] - 120):
                best = f
                break
        if best and t.get("expected_entry"):
            used.add(best["order_id"])
            entries.append(best["price"] / t["expected_entry"] - 1)
    seen = {}
    for x in (guard_state or {}).get("sold") or []:
        if x.get("order_id") and x.get("price_seen"):
            seen[x["order_id"]] = x["price_seen"]
    for x in ((guard_state or {}).get("eod") or {}).get("sold") or []:
        if x.get("order_id") and x.get("price_seen"):
            seen[x["order_id"]] = x["price_seen"]
    for f in fills:
        if f["side"] != "SELL":
            continue
        expected = f.get("stop_price") if f["type"] in ("STOP", "STOP_LIMIT") else seen.get(f["order_id"])
        if expected:
            exits.append(1 - f["price"] / expected)
    return entries, exits


def build_report(client, sd, cfg, report_mod, fetch_daily=None, today=None):
    start, end = window(cfg)
    today = today or _ny(_now()).date().isoformat()
    upto = min(today, end)
    r = {"start": start, "end": end, "today": today, "final": today >= end}
    since = datetime.datetime.combine(datetime.date.fromisoformat(start), datetime.time(0, 0)) - datetime.timedelta(days=21)
    try:
        fills = report_mod.history(client, since)
    except Exception as e:
        fills, r["history_error"] = None, str(e)[:160]
    closed = []
    if fills is not None:
        trips, _ = report_mod.round_trips(fills)
        closed = [c for c in trips if start <= _ny_dt(c["exit_at"]) <= upto and c.get("entry_at")
                  and _ny_dt(c["entry_at"]) >= start]
    r["real"] = closed
    r["real_avg_gbp"] = _mean([c["pl"] for c in closed if c.get("pl") is not None])
    r["real_avg_ret"] = _mean([c["pl"] / c["cost_gbp"] for c in closed if c.get("pl") is not None and c.get("cost_gbp")])
    r["real_total_gbp"] = sum(c["pl"] for c in closed if c.get("pl") is not None) if closed else 0.0
    sigs = [s for s in load(sd, "signals") if start <= s.get("day", "") <= upto]
    virt = [s for s in sigs if (s.get("virtual") or {}).get("R_net") is not None]
    r["signals"] = len(sigs)
    r["virtual"] = len(virt)
    r["virtual_avg_R"] = _mean([s["virtual"]["R_net"] for s in virt])
    r["virtual_avg_ret"] = _mean([s["virtual"]["ret_net"] for s in virt])
    r["virtual_win"] = (100.0 * sum(1 for s in virt if s["virtual"]["R_net"] > 0) / len(virt)) if virt else None
    trades = [t for t in load(sd, "trades") if start <= t.get("day", "") <= upto]
    guard = t212()._read_json(os.path.join(sd, "state", "t212-guard.json"), {}) or {}
    ent, ex = slippage(trades, fills or [], guard)
    r["slip_entry"], r["slip_exit"], r["slip_n"] = _mean(ent), _mean(ex), (len(ent), len(ex))
    days = (t212()._read_json(os.path.join(sd, "state", "account-days.json"), {}) or {}).get("days") or {}
    start_value = next((v.get("start") for d, v in sorted(days.items()) if d >= start and v.get("start")), None)
    r["start_value"] = start_value
    r["strategy_ret"] = (r["real_total_gbp"] / start_value) if start_value else None
    try:
        r["tracker_ret"] = tracker_return(start, upto, fetch_daily)
    except Exception as e:
        r["tracker_ret"], r["tracker_error"] = None, str(e)[:120]
    k = {}
    for verdict in ("agree", "disagree"):
        vs = [s for s in virt if (s.get("kronos") or {}).get("verdict") == verdict]
        k[verdict] = (len(vs), _mean([s["virtual"]["R_net"] for s in vs]))
    k["other"] = sum(1 for s in sigs if (s.get("kronos") or {}).get("verdict") not in ("agree", "disagree"))
    r["kronos"] = k
    traded = {s.get("id"): s for s in sigs if s.get("decision") == "traded"}
    r["kronos_real"] = {v: sum(1 for s in traded.values() if (s.get("kronos") or {}).get("verdict") == v)
                        for v in ("agree", "disagree")}
    ok_avg = r["real_avg_gbp"] is not None and r["real_avg_gbp"] > 0
    ok_tracker = r["strategy_ret"] is not None and r["tracker_ret"] is not None and r["strategy_ret"] > r["tracker_ret"]
    r["ok_avg"], r["ok_tracker"] = ok_avg, ok_tracker
    if not closed:
        r["status"] = "NO REAL TRADES YET" if not r["final"] else "FAIL: no real trades in the window"
    elif r["final"]:
        r["status"] = "PASS" if ok_avg and ok_tracker else "FAIL"
    else:
        r["status"] = "ON TRACK" if ok_avg and ok_tracker else "NOT ON TRACK"
    return r


def _ny_dt(utc_naive):
    return t212().ticket_module().to_eastern(utc_naive).date().isoformat()


def report_text(r):
    k = r["kronos"]
    lines = ["EVIDENCE MODE REPORT (demo forward test %s to %s), %s" % (r["start"], r["end"], r["today"]),
             "Status: %s. Pass needs a positive average per real trade after costs AND beating the tracker." % r["status"],
             "Real trades: %d closed, average %s (%s) a trade after costs, total %s%s" % (
                 len(r["real"]), _gbp(r["real_avg_gbp"]), _pct(r["real_avg_ret"]), _gbp(r["real_total_gbp"]),
                 "" if not r.get("history_error") else " (order history: %s)" % r["history_error"]),
             "Virtual signals: %d logged, %d resolved, average %s after costs (%s a trade), %s won" % (
                 r["signals"], r["virtual"], "no data" if r["virtual_avg_R"] is None else "%+.3fR" % r["virtual_avg_R"],
                 _pct(r["virtual_avg_ret"]), "no data" if r["virtual_win"] is None else "%.0f%%" % r["virtual_win"]),
             "Slippage, adverse: entries %s (%d fills), exits %s (%d fills); assumed %.2f%% a fill" % (
                 _pct(r["slip_entry"]), r["slip_n"][0], _pct(r["slip_exit"]), r["slip_n"][1], 100 * SLIP),
             "Return: strategy %s (real P&L on %s) vs S&P 500 tracker %s over the same days" % (
                 _pct(r["strategy_ret"]), _gbp(r["start_value"]) if r["start_value"] else "no start value",
                 _pct(r["tracker_ret"])),
             "Kronos (shadow): agreed on %d signals (virtual %s), disagreed on %d (virtual %s), no verdict on %d; "
             "real trades it agreed with %d, disagreed with %d" % (
                 k["agree"][0], "no data" if k["agree"][1] is None else "%+.3fR" % k["agree"][1],
                 k["disagree"][0], "no data" if k["disagree"][1] is None else "%+.3fR" % k["disagree"][1],
                 k["other"], r["kronos_real"]["agree"], r["kronos_real"]["disagree"]),
             "Demo money. Education and tooling, not financial advice."]
    return "\n".join(lines)


def send_report(client, sd, cfg, report_mod, phone=None, environ=None):
    r = build_report(client, sd, cfg, report_mod)
    text = report_text(r)
    d = os.path.join(sd, "reports")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "evidence-%s.md" % r["today"]), "w", encoding="utf-8") as fh:
        fh.write(text + "\n")
    ok = t212().phone_module().push(sd, text, phone=phone, environ=environ)
    return ok, text, r


def due(cfg, today_iso, weekday):
    """Friday, or the last day of the window, while the window is open."""
    if not on(cfg):
        return False
    start, end = window(cfg)
    return start <= today_iso <= end and (weekday == 4 or today_iso == end)


def main(argv=None, environ=None):
    ap = argparse.ArgumentParser(description="Evidence mode: the demo forward test's log and report.")
    ap.add_argument("--state-dir", default=os.path.join(ROOT, "trader"))
    sub = ap.add_subparsers(dest="cmd")
    sub.required = True
    rp = sub.add_parser("report", help="build the report now")
    rp.add_argument("--send", action="store_true", help="also send it to the phone")
    sub.add_parser("resolve", help="fill in today's virtual results now")
    a = ap.parse_args(argv)
    environ = os.environ if environ is None else environ
    T = t212()
    try:
        cfg = T.ticket_module().read_config(os.path.join(a.state_dir, "config.json"))
        if a.cmd == "resolve":
            print("resolved %d signals" % resolve(a.state_dir))
            return 0
        env = T.pick_env(False, environ)
        require_demo(cfg, env)
        key, secret = T.credentials(environ)
        client = T.Client(env, key, secret, state_dir=a.state_dir, log=lambda m: None)
        rep = _load("trader-broker", "report")
        rep.t212, rep.tk = T, T.ticket_module()
        if a.send:
            ok, text, _ = send_report(client, a.state_dir, cfg, rep, environ=environ)
        else:
            text = report_text(build_report(client, a.state_dir, cfg, rep))
        print(text)
        return 0
    except (EvidenceError, T.T212Error) as e:
        sys.stderr.write("evidence: %s\n" % e)
        return 2


if __name__ == "__main__":
    sys.exit(main())
