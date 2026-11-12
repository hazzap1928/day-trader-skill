#!/usr/bin/env python3
"""Evidence mode: an open-ended demo forward test of the day-trading autopilot.

With "autopilot": {"evidence_mode": true} (demo only; live refuses it):
  - the autopilot skips its cost block (min_net_r) and trader-risk uses a 1.0 after-costs
    reward:risk minimum, so trades are placed. Every other limit stays: trades a day, the
    daily loss limit, stops, the guard and the end-of-day close.
  - every signal the scanner triggers is logged, traded or not, with its entry, stop, target and
    cost-adjusted expectation, and after the close what the price did (a virtual result after costs).
  - real trades record the price expected when the order went in, so the report can measure
    slippage against the fills Trading 212 reports.
  - Kronos (trader-kronos/, shadow mode) gives a verdict on each signal. It never blocks anything.
  - Each signal and trade carries list_from: "allowed_tickers" (the hand list, before the 10 Nov switch) or
    "universe" (the filtered universe). The Friday report shows the two separately.
  - Each signal and trade also carries rules: "v1" (to 10 Nov: virtual results and the Time Machine
    used a fixed 2R target, and the daily loss limit counted the full risk of every entry) or "v2"
    (from 11 Nov: both model the real exit, the stop, the 1R trailing stop and the 15:50 close, and
    the daily loss limit counts losses realised today plus risk still open at the stops). Rows logged
    before the tag existed are v1. The Friday report shows the two separately.
  - Each signal carries a confidence score: the Time Machine's average R before costs (the number its
    go/no-go rule rests on), with a level, low / medium / high, by evidence.confidence_bands (default
    [0.0, 0.10]: low under 0.00R, high from 0.10R). It is logged only: nothing is sized or blocked by it.
    The Friday report shows results in R by level (v2 signals only: v1 used a different simulation).
  - Real trades from 12 Nov also carry pool ("free" or "reserved:SYM"), sizing ("v2": the per-trade cap
    and cash pools of trader-broker/allocation.py), risk_gbp (the planned loss at the stop) and confidence.
  - Each Friday after the close, a report goes to the phone, every Friday with no end date.

"evidence": {"start": "YYYY-MM-DD", "min_trades": 20}. There is no end date and no final PASS/FAIL day.
Each report gives a running status, ON TRACK or NOT ON TRACK, for this week and for since the start,
by the pass rule: a positive average per real trade after costs AND a better return than an S&P 500
tracker over the same days. Below min_trades closed trades, that status is "too few trades to judge".
An old "end" key is ignored.

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
KRONOS_TIMEOUT = 420                 # one verdict took 286 s on this Mac (Kronos-small, 10 paths, 69 steps)
UNIVERSE_FROM = "2026-11-10"         # the day the filtered universe replaced allowed_tickers (evidence.universe_from)
RULES = ("v1", "v2")
RULES_NOW = "v2"                     # what this code logs: real-exit simulations, open-risk daily loss count
RULES_NAMES = {"v1": "Old rules (2R target in the simulations, full-risk daily loss count)",
               "v2": "New rules (real exit in the simulations, open-risk daily loss count)"}
LISTS = ("allowed_tickers", "universe")
LIST_NAMES = {"allowed_tickers": "Hand list (allowed_tickers)", "universe": "Filtered universe"}
CONFIDENCE_BANDS = (0.0, 0.10)       # low < 0.00R <= medium < 0.10R <= high (evidence.confidence_bands)
CONFIDENCE_SOURCE = "Time Machine average R before costs"
LEVELS = ("low", "medium", "high")
SIZING_NOW = "v2"                    # trades: v2 = the per-trade cap (max_position_pct) and cash pools, from 12 Nov
FEE, SLIP = 0.0015, 0.0005           # the GBP account: FX fee and assumed slippage, each fill
TRACKER = "SPY"                      # stands in for the GBP-listed S&P 500 tracker (no FX fee)
TRACKER_COST = 0.0005                # slippage on each tracker leg
MIN_TRADES = 20                      # closed real trades before a running status counts (evidence.min_trades)
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


def settings(cfg):
    """(start, min_trades) from "evidence" in the config. No end date: the test runs until it is turned off."""
    w = (cfg or {}).get("evidence") or {}
    start = w.get("start")
    try:
        datetime.date.fromisoformat(str(start))
    except ValueError:
        raise EvidenceError("evidence.start in trader/config.json must be a date like 2026-11-10")
    n = w.get("min_trades", MIN_TRADES)
    if isinstance(n, bool) or not isinstance(n, int) or n < 1:
        raise EvidenceError("evidence.min_trades in trader/config.json must be a whole number, 1 or more")
    return start, n


def confidence_bands(cfg=None):
    """(low_below, high_from) in R from evidence.confidence_bands, else the default."""
    b = ((cfg or {}).get("evidence") or {}).get("confidence_bands")
    if b is None:
        return CONFIDENCE_BANDS
    ok = (isinstance(b, list) and len(b) == 2 and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in b)
          and b[0] < b[1])
    if not ok:
        raise EvidenceError("evidence.confidence_bands in trader/config.json must be two numbers in R, "
                            "low then high, like [0.0, 0.10]")
    return float(b[0]), float(b[1])


def level_of(score, bands=CONFIDENCE_BANDS):
    if score is None:
        return None
    return "low" if score < bands[0] else ("high" if score >= bands[1] else "medium")


def confidence(score, cfg=None):
    """{"score", "level", "source"} for a Time Machine average R (before costs), or None without one.
    Logged only: it never changes a size or a decision."""
    try:
        score = None if score is None else float(score)
    except (TypeError, ValueError):
        score = None
    if score is None:
        return None
    try:
        bands = confidence_bands(cfg)
    except EvidenceError:
        bands = CONFIDENCE_BANDS
    return {"score": round(score, 4), "level": level_of(score, bands), "source": CONFIDENCE_SOURCE}


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


def universe_from(cfg):
    return ((cfg or {}).get("evidence") or {}).get("universe_from") or UNIVERSE_FROM


def list_of(rec, cfg=None):
    """Which ticker list a signal or trade came from: its own tag, or (untagged rows) its day against the switch."""
    tag = rec.get("list_from")
    if tag in LISTS:
        return tag
    return "universe" if (rec.get("day") or "") >= universe_from(cfg) else "allowed_tickers"


def rules_of(rec):
    """"v1" or "v2": the rules a signal or trade was logged under. An untagged row is older than the tag: v1."""
    tag = rec.get("rules")
    return tag if tag in RULES else "v1"


def tag_lists(sd, cfg=None):
    """Tag every untagged signal and trade with list_from (before or after the universe switch) and rules
    (v1 before the real-exit change). Returns how many rows changed."""
    n = 0
    for name in ("signals", "trades"):
        rows = load(sd, name)
        todo = [r for r in rows if r.get("list_from") not in LISTS or r.get("rules") not in RULES]
        for r in todo:
            r["list_from"] = list_of(r, cfg)
            r["rules"] = rules_of(r)
        if todo:
            _save(sd, name, rows)
            n += len(todo)
    return n


def log_signal(sd, rec):
    """One scanner signal, traded or not. Returns its id. list_from says which ticker list it came from,
    rules which rules it was logged under (this code: v2)."""
    rec = dict(rec)
    rec.setdefault("rules", RULES_NOW)
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
    append(sd, "trades", dict(rec, at=rec.get("at") or _now(), rules=rec.get("rules") or RULES_NOW))


# ---------------------------------------------------------------- virtual results

def fetch_bars(symbol, rng="5d", interval="5m", volume=False):
    """[(utc timestamp, open, high, low, close[, volume])] from Yahoo (unofficial, can lag). Raises on failure."""
    req = urllib.request.Request(CHART % (symbol, rng, interval), headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        res = json.loads(r.read().decode("utf-8"))["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    rows = zip(res["timestamp"], q["open"], q["high"], q["low"], q["close"], q.get("volume") or [0] * len(res["timestamp"]))
    return [((t, o, h, l, c, v or 0) if volume else (t, o, h, l, c)) for t, o, h, l, c, v in rows
            if None not in (o, h, l, c)]


def _ny(ts):
    return t212().ticket_module().to_eastern(datetime.datetime.utcfromtimestamp(ts))


_TM = []


def timemachine():
    if not _TM:
        _TM.append(_load("trader-timemachine", "timemachine"))
    return _TM[0]


def virtual_result(bars, at, entry_plan, stop, target, k=FEE + SLIP, exits=None):
    """What a market buy at the first bar after `at` would have done.

    exits None (v1 rows): stop first (a gap opens under it), target first, else out at the 15:50 close.
    Stop and target in the same bar count as the stop.
    exits (v2 rows, timemachine.exits_from(config)): the exit the guard really runs: the stop (a gap
    sells at the open), the trailing stop from its activation profit, half at the target only if
    sell_half_at_target is on, else out at the 15:50 close. "how" is "stop", "trailed out" or
    "15:50 close". A half sale's price counts in R and in the costs.
    Returns {"entry", "exit", "how", "R", "R_net", "ret_net"} or None when there is no bar after `at`."""
    if exits is not None:
        return _virtual_real(bars, at, stop, target, k, exits)
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


def _virtual_real(bars, at, stop, target, k, exits):
    day = _ny(at).date()
    todays = [b for b in bars if _ny(b[0]).date() == day]
    after = [b for b in todays if b[0] >= at]
    end = [b for b in todays if (_ny(b[0]).hour, _ny(b[0]).minute) < EOD]
    if not after or not end:
        return None
    entry = after[0][1]
    risk = entry - stop
    if risk <= 0:
        return {"entry": entry, "exit": None, "how": "opened under the stop", "R": None, "R_net": None,
                "ret_net": None, "model": "real exit"}
    walk = timemachine().GuardExit(entry, stop, target, exits)
    n = 0
    for b in after:
        if (_ny(b[0]).hour, _ny(b[0]).minute) >= EOD:
            break
        walk.bar(*b[1:5])
        n += 1
    if not n:
        return None
    R, how = walk.result()
    exit_ = entry + R * risk                  # the average exit price (a half sale included)
    cost = k * (entry + exit_)
    return {"entry": round(entry, 4), "exit": round(exit_, 4), "how": "15:50 close" if how == "close" else how,
            "R": round(R, 3), "R_net": round(R - cost / risk, 3),
            "ret_net": round(exit_ * (1 - k) / (entry * (1 + k)) - 1, 5), "model": "real exit"}


def resolve(sd, day=None, fetch=None, cfg=None):
    """Fill in the virtual result of every logged signal of `day` (default today, New York) that has none.
    A v2 row uses the real exit from cfg (trailing_stop, eod_close); a v1 row keeps the fixed 2R target."""
    fetch = fetch or fetch_bars
    exits = timemachine().exits_from(cfg) if cfg is not None else None
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
            v = virtual_result(cache[r["symbol"]], r["at"], r["entry"], r["stop"], r["target"],
                               exits=exits if rules_of(r) == "v2" else None)
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


def _last_json(out):
    """The last line of stdout that is a JSON object: a library printing to stdout first must not lose the verdict."""
    for line in reversed((out or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    return json.loads(out)


def kronos_verdict(sig, run=None, timeout=KRONOS_TIMEOUT):
    """Ask trader-kronos/kronos_shadow.py (its own Python 3.10+ environment) about one signal. Never raises:
    any failure is a verdict of "unavailable" or "error", and nothing ever waits on it to place a trade."""
    if run is None and not kronos_available():
        return {"verdict": "unavailable", "why": "trader-kronos is not set up (see trader-kronos/README.md)"}
    payload = {k: sig.get(k) for k in ("symbol", "at", "entry", "stop", "target")}
    if run is None:
        try:          # fetched here, by the system Python: Kronos's own environment never touches the network
            payload["bars"] = fetch_bars(sig["symbol"], "5d", "5m", volume=True)
        except Exception as e:
            return {"verdict": "error", "why": "no bars for Kronos: %s" % str(e)[:120]}
    payload = json.dumps(payload)
    try:
        if run is not None:
            out = run(payload)
        else:
            p = subprocess.run([KRONOS_PY, KRONOS_SCRIPT], input=payload, capture_output=True, text=True,
                               timeout=timeout, cwd=KRONOS_DIR)
            if p.returncode != 0:
                return {"verdict": "error", "why": (p.stderr or p.stdout).strip()[-200:]}
            out = p.stdout
        res = _last_json(out)
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


def tracker_range(start, today):
    """A Yahoo daily range long enough to reach back to `start`: the test has no end date."""
    days = (datetime.date.fromisoformat(today) - datetime.date.fromisoformat(start)).days
    for rng, n in (("3mo", 85), ("1y", 360), ("2y", 725), ("5y", 1820), ("10y", 3645)):
        if days < n:
            return rng
    return "max"


def tracker_return(start, end_day, fetch=None, bars=None):
    """The S&P 500 tracker from the open on `start` to the latest close up to `end_day`, slippage in."""
    if bars is None:
        fetch = fetch or (lambda s: fetch_bars(s, tracker_range(start, end_day), "1d"))
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


def week_start(today):
    """Monday of the week `today` is in."""
    d = datetime.date.fromisoformat(today)
    return (d - datetime.timedelta(days=d.weekday())).isoformat()


def judge(ok_avg, ok_tracker, n, min_trades):
    """The running status by the pass rule, and below min_trades closed trades, "too few trades to judge"."""
    lean = "ON TRACK" if ok_avg and ok_tracker else "NOT ON TRACK"
    if n < min_trades:
        return "TOO FEW TRADES TO JUDGE", (lean if n else None)
    return lean, None


def period(closed, frm, upto, days, tracker_bars, tracker_error, min_trades):
    """The pass rule over closed real trades whose entry and exit both fall from `frm` to `upto`."""
    cs = [c for c in closed if frm <= _ny_dt(c["entry_at"]) and _ny_dt(c["exit_at"]) <= upto]
    pls = [c["pl"] for c in cs if c.get("pl") is not None]
    p = {"from": frm, "to": upto, "n": len(cs), "min_trades": min_trades, "avg_gbp": _mean(pls),
         "avg_ret": _mean([c["pl"] / c["cost_gbp"] for c in cs if c.get("pl") is not None and c.get("cost_gbp")]),
         "total_gbp": sum(pls) if cs else 0.0}
    p["start_value"] = next((v.get("start") for d, v in sorted(days.items()) if d >= frm and v.get("start")), None)
    p["strategy_ret"] = (p["total_gbp"] / p["start_value"]) if p["start_value"] else None
    p["tracker_ret"] = tracker_return(frm, upto, bars=tracker_bars) if tracker_bars is not None else None
    if tracker_error:
        p["tracker_error"] = tracker_error
    p["ok_avg"] = p["avg_gbp"] is not None and p["avg_gbp"] > 0
    p["ok_tracker"] = (p["strategy_ret"] is not None and p["tracker_ret"] is not None
                       and p["strategy_ret"] > p["tracker_ret"])
    p["status"], p["lean"] = judge(p["ok_avg"], p["ok_tracker"], p["n"], min_trades)
    return p


def build_report(client, sd, cfg, report_mod, fetch_daily=None, today=None):
    start, min_trades = settings(cfg)
    today = today or _ny(_now()).date().isoformat()
    upto = today
    r = {"start": start, "today": today, "min_trades": min_trades}
    since = datetime.datetime.combine(datetime.date.fromisoformat(start), datetime.time(0, 0)) - datetime.timedelta(days=21)
    try:
        fills = report_mod.history(client, since)
    except Exception as e:
        fills, r["history_error"] = None, str(e)[:160]
    closed = []
    if fills is not None:
        trips, _ = report_mod.round_trips(fills)
        broker = t212()._read_json(os.path.join(sd, "state", "broker.json"), {}) or {}
        drift_keys = {(x.get("t212_ticker"), str(x.get("date") or "")) for x in broker.get("orders") or []
                      if str(x.get("strategy") or "").lower() == "drift" and str(x.get("side", "")).lower() == "buy"}
        closed = [c for c in trips if start <= _ny_dt(c["exit_at"]) <= upto and c.get("entry_at")
                  and _ny_dt(c["entry_at"]) >= start
                  and (c["ticker"], _ny_dt(c["entry_at"])) not in drift_keys]     # drift has its own section
    r["real"] = closed
    r["real_avg_gbp"] = _mean([c["pl"] for c in closed if c.get("pl") is not None])
    r["real_avg_ret"] = _mean([c["pl"] / c["cost_gbp"] for c in closed if c.get("pl") is not None and c.get("cost_gbp")])
    r["real_total_gbp"] = sum(c["pl"] for c in closed if c.get("pl") is not None) if closed else 0.0
    tag_lists(sd, cfg)
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
    tracker_bars, tracker_error = None, None
    try:
        tracker_bars = (fetch_daily or (lambda s: fetch_bars(s, tracker_range(start, upto), "1d")))(TRACKER)
    except Exception as e:
        tracker_error = str(e)[:120]
    whole = period(closed, start, upto, days, tracker_bars, tracker_error, min_trades)
    week = period(closed, max(start, week_start(today)), upto, days, tracker_bars, tracker_error, min_trades)
    r["since_start"], r["week"] = whole, week
    r["start_value"], r["strategy_ret"], r["tracker_ret"] = whole["start_value"], whole["strategy_ret"], whole["tracker_ret"]
    if tracker_error:
        r["tracker_error"] = tracker_error
    r["ok_avg"], r["ok_tracker"], r["status"] = whole["ok_avg"], whole["ok_tracker"], whole["status"]
    trade_list = {}
    for t in trades:
        trade_list[(t.get("day"), t.get("t212_ticker"))] = list_of(t, cfg)
    groups = {}
    for name in LISTS:
        real = [c for c in closed
                if trade_list.get((_ny_dt(c["entry_at"]), c.get("ticker")),
                                  list_of({"day": _ny_dt(c["entry_at"])}, cfg)) == name]
        gs = [s for s in sigs if list_of(s, cfg) == name]
        gv = [s for s in gs if (s.get("virtual") or {}).get("R_net") is not None]
        gdays = sorted(set(s.get("day") for s in gs) | set(_ny_dt(c["entry_at"]) for c in real))
        groups[name] = {"real": len(real), "real_avg_gbp": _mean([c["pl"] for c in real if c.get("pl") is not None]),
                        "real_total_gbp": sum(c["pl"] for c in real if c.get("pl") is not None),
                        "signals": len(gs), "virtual": len(gv),
                        "virtual_avg_R": _mean([s["virtual"]["R_net"] for s in gv]),
                        "virtual_win": (100.0 * sum(1 for s in gv if s["virtual"]["R_net"] > 0) / len(gv)) if gv else None,
                        "days": (gdays[0], gdays[-1]) if gdays else None}
    r["by_list"], r["universe_from"] = groups, universe_from(cfg)
    trade_rules = {}
    for t in trades:
        trade_rules[(t.get("day"), t.get("t212_ticker"))] = rules_of(t)
    by_rules = {}
    for name in RULES:
        real = [c for c in closed if trade_rules.get((_ny_dt(c["entry_at"]), c.get("ticker")), "v1") == name]
        gs = [s for s in sigs if rules_of(s) == name]
        gv = [s for s in gs if (s.get("virtual") or {}).get("R_net") is not None]
        gdays = sorted(set(s.get("day") for s in gs) | set(_ny_dt(c["entry_at"]) for c in real))
        by_rules[name] = {"real": len(real), "real_avg_gbp": _mean([c["pl"] for c in real if c.get("pl") is not None]),
                          "real_total_gbp": sum(c["pl"] for c in real if c.get("pl") is not None),
                          "signals": len(gs), "virtual": len(gv),
                          "virtual_avg_R": _mean([s["virtual"]["R_net"] for s in gv]),
                          "virtual_win": (100.0 * sum(1 for s in gv if s["virtual"]["R_net"] > 0) / len(gv)) if gv else None,
                          "days": (gdays[0], gdays[-1]) if gdays else None}
    r["by_rules"] = by_rules
    k = {}
    for verdict in ("agree", "disagree"):
        vs = [s for s in virt if (s.get("kronos") or {}).get("verdict") == verdict]
        k[verdict] = (len(vs), _mean([s["virtual"]["R_net"] for s in vs]))
    k["other"] = sum(1 for s in sigs if (s.get("kronos") or {}).get("verdict") not in ("agree", "disagree"))
    r["kronos"] = k
    traded = {s.get("id"): s for s in sigs if s.get("decision") == "traded"}
    r["kronos_real"] = {v: sum(1 for s in traded.values() if (s.get("kronos") or {}).get("verdict") == v)
                        for v in ("agree", "disagree")}
    r["confidence_bands"] = confidence_bands(cfg)
    r["by_confidence"] = by_confidence(sigs, trades, closed, r["confidence_bands"])
    return r


def by_confidence(sigs, trades, closed, bands):
    """Results in R by confidence level, v2 rows only (v1's Time Machine used a fixed 2R target).
    Virtual: each signal's R after costs. Real: each closed trade's GBP result over its planned loss at the
    stop (risk_gbp), for trades logged with both. A row's own level is used; else its tm_avg_R decides."""
    def lvl(row):
        c = row.get("confidence") or {}
        return c.get("level") if c.get("level") in LEVELS else level_of(row.get("tm_avg_R"), bands)
    out = {}
    real_rows = {}
    for t in trades:
        if rules_of(t) == "v2" and t.get("risk_gbp") and lvl(t):
            real_rows[(t.get("day"), t.get("t212_ticker"))] = (lvl(t), float(t["risk_gbp"]))
    for name in LEVELS:
        gs = [s for s in sigs if rules_of(s) == "v2" and lvl(s) == name]
        gv = [s["virtual"]["R_net"] for s in gs if (s.get("virtual") or {}).get("R_net") is not None]
        rr = []
        for c in closed:
            hit = real_rows.get((_ny_dt(c["entry_at"]), c.get("ticker")))
            if hit and hit[0] == name and c.get("pl") is not None:
                rr.append(c["pl"] / hit[1])
        out[name] = {"signals": len(gs), "virtual": len(gv), "virtual_avg_R": _mean(gv),
                     "virtual_win": (100.0 * sum(1 for x in gv if x > 0) / len(gv)) if gv else None,
                     "real": len(rr), "real_avg_R": _mean(rr)}
    return out


def status_line(label, p):
    """One running-status line: the status, how many closed trades it rests on, and the two tests."""
    n = "%d closed trade%s" % (p["n"], "" if p["n"] == 1 else "s")
    if p["status"] == "TOO FEW TRADES TO JUDGE":
        head = "too few trades to judge (%s, minimum %d)" % (n, p["min_trades"])
        if p["lean"]:
            head += "; so far it reads %s" % p["lean"]
    else:
        head = "%s, based on %s" % (p["status"], n)
    return "%s (%s to %s): %s. Average %s a trade after costs, total %s; strategy %s (on %s) vs S&P 500 tracker %s" % (
        label, p["from"], p["to"], head, _gbp(p["avg_gbp"]), _gbp(p["total_gbp"]), _pct(p["strategy_ret"]),
        _gbp(p["start_value"]) if p["start_value"] else "no start value", _pct(p["tracker_ret"]))


def _ny_dt(utc_naive):
    return t212().ticket_module().to_eastern(utc_naive).date().isoformat()


def report_text(r):
    k = r["kronos"]
    lines = ["EVIDENCE MODE REPORT (demo forward test since %s, no end date), %s" % (r["start"], r["today"]),
             "Running status. ON TRACK needs a positive average per real trade after costs AND beating the "
             "tracker over the same days; under %d closed trades it is too few trades to judge." % r["min_trades"],
             status_line("This week", r["week"]),
             status_line("Since the start", r["since_start"]),
             "Real trades since the start: %d closed, average %s (%s) a trade after costs, total %s%s" % (
                 len(r["real"]), _gbp(r["real_avg_gbp"]), _pct(r["real_avg_ret"]), _gbp(r["real_total_gbp"]),
                 "" if not r.get("history_error") else " (order history: %s)" % r["history_error"]),
             "Virtual signals: %d logged, %d resolved, average %s after costs (%s a trade), %s won" % (
                 r["signals"], r["virtual"], "no data" if r["virtual_avg_R"] is None else "%+.3fR" % r["virtual_avg_R"],
                 _pct(r["virtual_avg_ret"]), "no data" if r["virtual_win"] is None else "%.0f%%" % r["virtual_win"]),
             "Slippage, adverse: entries %s (%d fills), exits %s (%d fills); assumed %.2f%% a fill" % (
                 _pct(r["slip_entry"]), r["slip_n"][0], _pct(r["slip_exit"]), r["slip_n"][1], 100 * SLIP),
             "Kronos (shadow): agreed on %d signals (virtual %s), disagreed on %d (virtual %s), no verdict on %d; "
             "real trades it agreed with %d, disagreed with %d" % (
                 k["agree"][0], "no data" if k["agree"][1] is None else "%+.3fR" % k["agree"][1],
                 k["disagree"][0], "no data" if k["disagree"][1] is None else "%+.3fR" % k["disagree"][1],
                 k["other"], r["kronos_real"]["agree"], r["kronos_real"]["disagree"]),
             ]
    for name in LISTS:
        g = (r.get("by_list") or {}).get(name)
        if not g or not (g["signals"] or g["real"]):
            continue
        lines.append("%s, %s: real %d closed, average %s, total %s; virtual %d logged, %d resolved, average %s, %s won" % (
            LIST_NAMES[name],
            ("before %s" % r["universe_from"]) if name == "allowed_tickers" else ("from %s" % r["universe_from"]),
            g["real"], _gbp(g["real_avg_gbp"]), _gbp(g["real_total_gbp"]), g["signals"], g["virtual"],
            "no data" if g["virtual_avg_R"] is None else "%+.3fR" % g["virtual_avg_R"],
            "no data" if g["virtual_win"] is None else "%.0f%%" % g["virtual_win"]))
    for name in RULES:
        g = (r.get("by_rules") or {}).get(name)
        if not g or not (g["signals"] or g["real"]):
            continue
        lines.append("%s, %s: real %d closed, average %s, total %s; virtual %d logged, %d resolved, average %s, %s won" % (
            RULES_NAMES[name], ("%s to %s" % g["days"]) if g["days"] else "no days",
            g["real"], _gbp(g["real_avg_gbp"]), _gbp(g["real_total_gbp"]), g["signals"], g["virtual"],
            "no data" if g["virtual_avg_R"] is None else "%+.3fR" % g["virtual_avg_R"],
            "no data" if g["virtual_win"] is None else "%.0f%%" % g["virtual_win"]))
    bc = r.get("by_confidence") or {}
    if any(g["signals"] or g["real"] for g in bc.values()):
        lo, hi = r.get("confidence_bands") or CONFIDENCE_BANDS
        lines.append("Confidence (%s, v2 signals; logged only, sizes unchanged): low under %+.2fR, medium "
                     "%+.2fR to %+.2fR, high from %+.2fR" % (CONFIDENCE_SOURCE, lo, lo, hi, hi))
        for name in LEVELS:
            g = bc.get(name)
            if not g:
                continue
            lines.append("  %s: virtual %d logged, %d resolved, average %s after costs, %s won; real %d closed, "
                         "average %s" % (name, g["signals"], g["virtual"],
                                         "no data" if g["virtual_avg_R"] is None else "%+.3fR" % g["virtual_avg_R"],
                                         "no data" if g["virtual_win"] is None else "%.0f%%" % g["virtual_win"],
                                         g["real"], "no data" if g["real_avg_R"] is None else "%+.3fR" % g["real_avg_R"]))
    lines.append("Demo money. Education and tooling, not financial advice.")
    return "\n".join(lines)


def drift_lines(sd, cfg, today):
    """The Friday report's drift section (trader-broker/drift_buy.py), or [] (off, or it could not be built)."""
    try:
        db = _load("trader-broker", "drift_buy")
        db.t212 = t212()
        return db.friday_lines(sd, cfg, today) or []
    except Exception as e:
        return ["", "## Post-earnings drift (demo)", "- no data (%s)" % str(e)[:120]]


def send_report(client, sd, cfg, report_mod, phone=None, environ=None):
    r = build_report(client, sd, cfg, report_mod)
    text = report_text(r)
    extra = drift_lines(sd, cfg, r["today"])
    if extra:
        text = text + "\n" + "\n".join(extra)
    d = os.path.join(sd, "reports")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "evidence-%s.md" % r["today"]), "w", encoding="utf-8") as fh:
        fh.write(text + "\n")
    ok = t212().phone_module().push(sd, phone_text(sd, cfg, r), phone=phone, environ=environ)
    return ok, text, r


def phone_text(sd, cfg, r):
    """The Friday report for the phone, in plain words (trader-broker/messages.py). The saved file keeps the detail."""
    M = t212().phone_module().messages()
    M.use(sd)
    drift = None
    try:
        db = _load("trader-broker", "drift_buy")
        db.t212 = t212()
        drift = db.friday_phone(sd, cfg, r["today"])
    except Exception:
        drift = ["\u26a0\ufe0f The earnings-trades figures couldn't be read this week."]
    return M.friday(r, drift)


def due(cfg, today_iso, weekday):
    """Every Friday from the start date on, with no end date."""
    if not on(cfg):
        return False
    start, _ = settings(cfg)
    return today_iso >= start and weekday == 4


def main(argv=None, environ=None):
    ap = argparse.ArgumentParser(description="Evidence mode: the demo forward test's log and report.")
    ap.add_argument("--state-dir", default=os.path.join(ROOT, "trader"))
    sub = ap.add_subparsers(dest="cmd")
    sub.required = True
    rp = sub.add_parser("report", help="build the report now")
    rp.add_argument("--send", action="store_true", help="also send it to the phone")
    sub.add_parser("resolve", help="fill in today's virtual results now")
    sub.add_parser("tag", help="tag untagged signals and trades: before or after the universe switch, and rules v1")
    a = ap.parse_args(argv)
    environ = os.environ if environ is None else environ
    T = t212()
    try:
        cfg = T.ticket_module().read_config(os.path.join(a.state_dir, "config.json"))
        if a.cmd == "tag":
            print("tagged %d rows" % tag_lists(a.state_dir, cfg))
            return 0
        if a.cmd == "resolve":
            print("resolved %d signals" % resolve(a.state_dir, cfg=cfg))
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
