#!/usr/bin/env python3
"""Post-earnings drift, stage 2 of 4: a SHADOW scan. It finds drift signals, logs how they would
have done, and shows them on the phone. It never buys, sells or cancels anything: it has no
Trading 212 connection at all. Its only writes are its own files under trader/state/drift/.

The rule (research/README.md, research/pead_tracker.py, ranking "B", 10 slots):
  - an earnings beat: EPS above the forecast, a surprise of at least 10%, on a forecast of at
    least $0.10 either side of zero (as tested; "forecast_either_side": false needs +$0.10)
  - the stock is in the current universe (trader/universe/tradable.json, or allowed_tickers
    when the universe is off or stale) and not on blocked_tickers
  - report on trading day D: a virtual buy at the OPEN of the 2nd trading day after D, a virtual
    sell at the CLOSE of the 20th trading day held (the buy day is day 1)
  - ranked by surprise relative to the share price: (EPS - forecast) / price
  - a virtual book of 10 slots: a signal is shortlisted only when a slot is free on its buy day

Each trading day the autopilot runs the scan about 15:35 New York (before the closing report):
reports from the previous trading day become tomorrow's shortlist. After the closing report it
fills in virtual results at 5, 10, 20 and 40 trading days, after costs (0.15% FX fee + 0.05%
slippage on the buy and on the sell), each against SPY over the same dates (0.05% a leg).

Logged only, never used to decide anything (untested): up on the report day and the reaction day,
volume against its 20-day average, the highest gain and its day, the first day at +5/+10/+15%,
and whether a 3xATR stop would have been hit, and when.

Config, "drift" in trader/config.json (scan_enabled false, or no block, means nothing runs):
  {"scan_enabled": true, "min_surprise_pct": 10, "min_forecast": 0.10, "forecast_either_side": true,
   "entry_after_days": 2, "hold_days": 20, "slots": 10, "result_days": [5, 10, 20, 40],
   "fx_fee": 0.0015, "slippage": 0.0005, "tracker": "SPY", "tracker_cost": 0.0005, "scan_at": "15:35",
   "atr_days": 14, "atr_mult": 3, "gain_marks_pct": [5, 10, 15], "volume_days": 20}

    python3 trader-broker/drift.py scan        # tomorrow's shortlist now (once a day; --force again)
    python3 trader-broker/drift.py resolve     # fill in virtual results now
    python3 trader-broker/drift.py show        # the shortlist and the log so far

Demo only (it refuses when "paper" is not true). Education and tooling, not financial advice.
"""

import argparse
import datetime
import importlib.util
import json
import os
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EARNINGS = "https://api.nasdaq.com/api/calendar/earnings?date=%s"
CHART = "https://query1.finance.yahoo.com/v8/finance/chart/%s?period1=%d&period2=%d&interval=1d"
SOURCE = "Nasdaq earnings calendar (api.nasdaq.com), Yahoo Finance daily bars (unofficial)"
UA_BROWSER = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/120.0 Safari/537.36")
UA_SIMPLE = "Mozilla/5.0 (compatible; day-trader-skill/2.0)"
WHEN = {"time-pre-market": "before the open", "time-after-hours": "after the close"}
SHADOW = "SHADOW: nothing is bought"
NO_SCAN = "no drift scan yet"

DEFAULTS = {"scan_enabled": False, "min_surprise_pct": 10.0, "min_forecast": 0.10, "forecast_either_side": True,
            "entry_after_days": 2, "hold_days": 20, "slots": 10, "result_days": [5, 10, 20, 40],
            "fx_fee": 0.0015, "slippage": 0.0005, "tracker": "SPY", "tracker_cost": 0.0005, "scan_at": "15:35",
            "atr_days": 14, "atr_mult": 3.0, "gain_marks_pct": [5, 10, 15], "volume_days": 20,
            "scan_budget_s": 600, "resolve_budget_s": 900}


class DriftError(Exception):
    pass


def _load(folder, name):
    spec = importlib.util.spec_from_file_location("drift_" + name, os.path.join(ROOT, folder, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_MODS = {}


def mod(folder, name):
    if (folder, name) not in _MODS:
        _MODS[(folder, name)] = _load(folder, name)
    return _MODS[(folder, name)]


def alerts():
    return mod("trader-alerts", "alerts")       # the NYSE calendar


# ---------------------------------------------------------------- seams the tests replace

def _now():
    return time.time()


def _get(url, browser=False, timeout=30):
    """One plain GET (no keys, no login). Returns bytes; raises on any failure."""
    req = urllib.request.Request(url, headers={"User-Agent": UA_BROWSER if browser else UA_SIMPLE,
                                               "Accept": "application/json, */*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# ---------------------------------------------------------------- settings

def settings(cfg):
    """The drift settings with defaults. Raises DriftError on a bad value."""
    raw = (cfg or {}).get("drift")
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise DriftError("drift in trader/config.json must be an object, e.g. {\"scan_enabled\": true}")
    s = dict(DEFAULTS)
    s.update(raw)
    for k in ("scan_enabled", "forecast_either_side"):
        if not isinstance(s[k], bool):
            raise DriftError("drift.%s must be true or false" % k)
    for k in ("min_surprise_pct", "min_forecast", "fx_fee", "slippage", "tracker_cost", "atr_mult",
              "scan_budget_s", "resolve_budget_s"):
        if isinstance(s[k], bool) or not isinstance(s[k], (int, float)) or s[k] < 0:
            raise DriftError("drift.%s must be a number, 0 or more" % k)
        s[k] = float(s[k])
    for k, lo, hi in (("entry_after_days", 1, 10), ("hold_days", 1, 60), ("slots", 1, 50), ("atr_days", 2, 50),
                      ("volume_days", 2, 60)):
        if isinstance(s[k], bool) or not isinstance(s[k], int) or not lo <= s[k] <= hi:
            raise DriftError("drift.%s must be a whole number from %d to %d" % (k, lo, hi))
    for k in ("result_days", "gain_marks_pct"):
        v = s[k]
        if not isinstance(v, list) or not v or not all(isinstance(x, (int, float)) and not isinstance(x, bool)
                                                       and x > 0 for x in v):
            raise DriftError("drift.%s must be a list of positive numbers" % k)
    if not all(isinstance(x, int) and x <= 60 for x in s["result_days"]):
        raise DriftError("drift.result_days must be whole numbers of trading days, at most 60")
    s["result_days"] = sorted(set(s["result_days"]))
    if not isinstance(s["tracker"], str) or not s["tracker"].strip():
        raise DriftError("drift.tracker must be a ticker, like SPY")
    if not _hm(s["scan_at"]):
        raise DriftError("drift.scan_at must be a New York time like 15:35")
    return s


def _hm(text):
    try:
        h, m = str(text).strip().split(":")
        h, m = int(h), int(m)
    except (ValueError, AttributeError):
        return None
    return (h, m) if 9 <= h < 16 and 0 <= m < 60 else None


def enabled(cfg):
    """True when the scan should run: scan_enabled, on a demo ("paper": true) config."""
    try:
        return settings(cfg)["scan_enabled"] and (cfg or {}).get("paper") is True
    except DriftError:
        return False


def require_demo(cfg):
    if (cfg or {}).get("paper") is not True or (cfg or {}).get("live_enabled"):
        raise DriftError("the drift scan is demo only: trader/config.json must have \"paper\": true")


# ---------------------------------------------------------------- trading days

def _date(v):
    if isinstance(v, datetime.date):
        return v
    try:
        return datetime.date.fromisoformat(str(v).strip()[:10])
    except (TypeError, ValueError):
        raise DriftError("not a date: %r" % (v,))


def is_trading_day(d):
    return alerts().trading_day(_date(d))[0]


def add_trading_days(d, n):
    """The trading date n trading days after d (before it when n < 0). Weekends and NYSE holidays
    are skipped; a date the holiday list does not cover is refused."""
    al = alerts()
    d = _date(d)
    step = 1 if n >= 0 else -1
    left = abs(n)
    while left:
        d += datetime.timedelta(days=step)
        if d.year not in al.CALENDAR_YEARS:
            raise DriftError("the NYSE holiday list covers %s only, so %d cannot be counted"
                             % (" and ".join(map(str, al.CALENDAR_YEARS)), d.year))
        if al.trading_day(d)[0]:
            left -= 1
    return d


def buy_day(report_day, s):
    return add_trading_days(report_day, s["entry_after_days"])


def exit_day(entry_day, n):
    """The buy day is day 1: the close of the nth trading day held."""
    return add_trading_days(entry_day, n - 1)


def _today_ny():
    tk = mod("trader-broker", "ticket")
    return tk.to_eastern(datetime.datetime.utcfromtimestamp(_now())).date()


# ---------------------------------------------------------------- data

def num(x):
    """Nasdaq's "$1.23", "($0.05)", "12.5" -> float, or None."""
    if x is None:
        return None
    try:
        return float(str(x).replace("$", "").replace(",", "").replace("%", "").replace("(", "-").replace(")", "").strip())
    except ValueError:
        return None


def key(sym):
    """BRK.B, BRK-B, BRK/B -> BRKB: one spelling for comparing lists."""
    return "".join(ch for ch in str(sym or "").upper() if ch.isalnum())


def yahoo_symbol(sym):
    return str(sym).upper().replace(".", "-").replace("/", "-")


def earnings_rows(day):
    """Nasdaq's earnings calendar for one day: [{symbol, eps, forecast, surprise_pct, when}]. Raises on failure."""
    doc = json.loads(_get(EARNINGS % _date(day).isoformat(), browser=True).decode("utf-8"))
    rows = ((doc.get("data") or {}).get("rows")) or []
    out = []
    for r in rows:
        sym = str(r.get("symbol") or "").strip().upper()
        if not sym:
            continue
        out.append({"symbol": sym, "name": r.get("name") or "", "eps": num(r.get("eps")),
                    "forecast": num(r.get("epsForecast")), "surprise_pct": num(r.get("surprise")),
                    "when": WHEN.get(r.get("time"), "time not given"), "time_raw": r.get("time")})
    return out


def daily_bars(symbol, start, end):
    """{date: (open, high, low, close, volume)} from Yahoo, New York dates from start to end. Raises on failure."""
    a = datetime.datetime.combine(_date(start), datetime.time(0, 0)).replace(tzinfo=datetime.timezone.utc)
    b = datetime.datetime.combine(_date(end) + datetime.timedelta(days=2), datetime.time(0, 0)).replace(
        tzinfo=datetime.timezone.utc)
    url = CHART % (urllib.parse.quote(yahoo_symbol(symbol)), int(a.timestamp()), int(b.timestamp()))
    res = json.loads(_get(url).decode("utf-8"))["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    tk = mod("trader-broker", "ticket")
    out = {}
    vols = q.get("volume") or [0] * len(res["timestamp"])
    for t, o, h, l, c, v in zip(res["timestamp"], q["open"], q["high"], q["low"], q["close"], vols):
        if None in (o, h, l, c):
            continue
        out[tk.to_eastern(datetime.datetime.utcfromtimestamp(t)).date()] = (float(o), float(h), float(l), float(c),
                                                                            float(v or 0))
    return out


# ---------------------------------------------------------------- the rule

def universe(cfg, state_dir):
    """(set of keys, where it came from). The universe on demo when it is on and fresh, else allowed_tickers;
    blocked_tickers always taken out. None when there is no list at all."""
    t212 = mod("trader-broker", "t212")
    eff = mod("trader-scanner", "universe").apply(cfg or {}, state_dir)[0]
    allow = t212.allowed_tickers(eff)
    blocked = {key(x) for x in t212.blocked_tickers(cfg or {})}
    if allow is None:
        return None, blocked, "no allow list"
    src = "universe" if eff.get("allowed_from") == "universe" else "allowed_tickers"
    return {key(x) for x in allow} - blocked, blocked, src


def check(row, s, allowed, blocked):
    """(qualifies, why not)."""
    eps, fc, sur = row.get("eps"), row.get("forecast"), row.get("surprise_pct")
    if eps is None or fc is None or sur is None:
        return False, "no EPS, forecast or surprise figure"
    if not eps > fc:
        return False, "not a beat"
    if sur < s["min_surprise_pct"]:
        return False, "surprise %.1f%% under %g%%" % (sur, s["min_surprise_pct"])
    floor = abs(fc) if s["forecast_either_side"] else fc
    if floor < s["min_forecast"] - 1e-12:
        return False, "forecast $%.2f under $%.2f" % (fc, s["min_forecast"])
    k = key(row["symbol"])
    if k in blocked:
        return False, "on blocked_tickers"
    if allowed is None or k not in allowed:
        return False, "not in the universe"
    return True, ""


def score(row, price):
    """Ranking B: the surprise relative to the share price, (EPS - forecast) / price."""
    if not price or price <= 0:
        return None
    return (row["eps"] - row["forecast"]) / price


# ---------------------------------------------------------------- files

def _dir(sd):
    d = os.path.join(sd, "state", "drift")
    os.makedirs(d, exist_ok=True)
    return d


def signals_path(sd):
    return os.path.join(sd, "state", "drift", "signals.jsonl")


def shortlist_path(sd):
    return os.path.join(sd, "state", "drift", "shortlist.json")


def load_signals(sd):
    out = []
    try:
        with open(signals_path(sd), encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        continue
    except OSError:
        pass
    return out


def save_signals(sd, rows):
    _dir(sd)
    tmp = signals_path(sd) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, sort_keys=True) + "\n")
    os.replace(tmp, signals_path(sd))


def _write(path, data):
    _dir(os.path.dirname(os.path.dirname(os.path.dirname(path))))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, sort_keys=True)
    os.replace(tmp, path)


def read_shortlist(sd):
    try:
        with open(shortlist_path(sd), encoding="utf-8") as fh:
            doc = json.load(fh)
        return doc if isinstance(doc, dict) else None
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- the scan

def open_in_book(signals, day):
    """Shortlisted signals holding a virtual slot on `day` (bought on or before it, sold at the close on or after it)."""
    d = _date(day).isoformat()
    return [r for r in signals if r.get("shortlisted") and r.get("entry_day", "9") <= d <= r.get("exit_day", "0")]


def scan(sd, cfg, today=None, force=False, budget_s=None):
    """Tomorrow's shortlist from the previous trading day's reports. Returns the shortlist doc, or None when off.
    Shadow only: reads Nasdaq and Yahoo, writes trader/state/drift/."""
    s = settings(cfg)
    if not s["scan_enabled"]:
        return None
    require_demo(cfg)
    today = _date(today or _today_ny())
    if not is_trading_day(today):
        return None
    rday = add_trading_days(today, -1)
    bday = buy_day(rday, s)
    old = read_shortlist(sd)
    if old and old.get("report_day") == rday.isoformat() and not old.get("error") and not force:
        return old
    started = _now()
    budget = s["scan_budget_s"] if budget_s is None else budget_s
    doc = {"scan_day": today.isoformat(), "report_day": rday.isoformat(), "buy_day": bday.isoformat(),
           "exit_day": exit_day(bday, s["hold_days"]).isoformat(), "generated_utc":
           datetime.datetime.utcfromtimestamp(_now()).strftime("%Y-%m-%dT%H:%M:%SZ"), "source": SOURCE,
           "shadow": True, "slots": s["slots"], "rows": 0, "qualified": 0, "shortlist": [], "notes": []}
    try:
        rows = earnings_rows(rday)
    except Exception as e:
        doc["error"] = "the Nasdaq earnings calendar did not answer (%s)" % str(e)[:120]
        _write(shortlist_path(sd), doc)
        return doc
    allowed, blocked, list_src = universe(cfg, sd)
    doc["rows"], doc["list_from"] = len(rows), list_src
    signals = load_signals(sd)
    seen = {(r.get("report_day"), key(r.get("symbol"))) for r in signals}
    fresh = []
    for row in rows:
        ok, why = check(row, s, allowed, blocked)
        if not ok or (rday.isoformat(), key(row["symbol"])) in seen:
            continue
        sig = {"id": "%s-%s" % (rday.isoformat(), key(row["symbol"])), "symbol": row["symbol"], "name": row["name"],
               "report_day": rday.isoformat(), "when": row["when"], "time_raw": row["time_raw"],
               "eps": row["eps"], "forecast": row["forecast"], "surprise_pct": row["surprise_pct"],
               "entry_day": bday.isoformat(), "exit_day": exit_day(bday, s["hold_days"]).isoformat(),
               "source": SOURCE, "list_from": list_src, "logged_utc": doc["generated_utc"], "shadow": True,
               "results": {}, "extras": {}}
        if _now() - started > budget:
            sig.update(price=None, score=None, shortlisted=False, why="not ranked: the scan ran out of time")
        else:
            try:
                bars = daily_bars(row["symbol"], rday - datetime.timedelta(days=10), today)
                last = bars[max(bars)] if bars else None
                sig["price"] = last[3] if last else None
                sig["price_day"] = max(bars).isoformat() if bars else None
            except Exception as e:
                sig["price"], sig["price_error"] = None, str(e)[:120]
            sig["score"] = score(row, sig["price"])
            if sig["score"] is None:
                sig.update(shortlisted=False, why="not ranked: no share price")
        fresh.append(sig)
    doc["qualified"] = len(fresh)
    ranked = sorted([x for x in fresh if x.get("score") is not None], key=lambda x: -x["score"])
    book = open_in_book(signals, bday)
    held = {key(r["symbol"]) for r in book}
    free = s["slots"] - len(book)
    for i, sig in enumerate(ranked, 1):
        sig["rank"] = i
        if key(sig["symbol"]) in held:
            sig.update(shortlisted=False, why="already held in the virtual book")
        elif free <= 0:
            sig.update(shortlisted=False, why="no free slot (%d of %d in use)" % (s["slots"], s["slots"]))
        else:
            sig.update(shortlisted=True, why="")
            held.add(key(sig["symbol"]))
            free -= 1
    doc["in_book"] = len(book)
    doc["shortlist"] = [{"symbol": x["symbol"], "surprise_pct": x["surprise_pct"], "score": x["score"],
                         "when": x["when"], "price": x["price"], "rank": x["rank"]}
                        for x in ranked if x.get("shortlisted")]
    doc["not_shortlisted"] = [{"symbol": x["symbol"], "why": x["why"]} for x in fresh if not x.get("shortlisted")]
    save_signals(sd, signals + fresh)
    _write(shortlist_path(sd), doc)
    return doc


# ---------------------------------------------------------------- virtual results

def net_return(entry, exit_, k):
    """A buy at entry and a sell at exit_, each paying k (FX fee + slippage)."""
    return (exit_ * (1 - k)) / (entry * (1 + k)) - 1


def held_days(entry_day, upto, bars):
    """The trading days from entry_day to upto (inclusive) that have a bar, in order."""
    return [d for d in sorted(bars) if _date(entry_day) <= d <= _date(upto)]


def atr(bars, before, n):
    """Average true range over the n trading days before `before`, or None."""
    days = [d for d in sorted(bars) if d < _date(before)]
    if len(days) < n + 1:
        return None
    trs = []
    for prev, d in zip(days[-n - 1:-1], days[-n:]):
        o, h, l, c, v = bars[d]
        pc = bars[prev][3]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / n


def extras(sig, bars, s, upto):
    """The untested extras, from daily bars up to the close of `upto` (final closes only). Logged, never used."""
    out = {}
    rd = _date(sig["report_day"])
    before = [d for d in sorted(bars) if d < rd]
    if rd in bars and before:
        out["report_day_change_pct"] = round(100 * (bars[rd][3] / bars[before[-1]][3] - 1), 3)
    after = [d for d in sorted(bars) if rd < d <= _date(upto)]
    if after and rd in bars:
        out["reaction_day"] = after[0].isoformat() if sig.get("when") == "after the close" else rd.isoformat()
        rx = after[0] if sig.get("when") == "after the close" else rd
        prev = [d for d in sorted(bars) if d < rx]
        if prev:
            out["reaction_day_change_pct"] = round(100 * (bars[rx][3] / bars[prev[-1]][3] - 1), 3)
    vn = s["volume_days"]
    if rd in bars and len(before) >= vn:
        avg = sum(bars[d][4] for d in before[-vn:]) / vn
        if avg > 0:
            out["report_day_volume_x_avg"] = round(bars[rd][4] / avg, 3)
    e = sig.get("entry_open")
    days = held_days(sig["entry_day"], upto, bars)[:max(s["result_days"])]
    if not e or not days:
        return out
    best, best_day = None, None
    marks = {}
    for n, d in enumerate(days, 1):
        g = 100 * (bars[d][1] / e - 1)
        if best is None or g > best:
            best, best_day = g, n
        for m in s["gain_marks_pct"]:
            if g >= m and str(m) not in marks:
                marks[str(m)] = n
    out["days_seen"] = len(days)
    out["max_gain_pct"] = round(best, 3)
    out["max_gain_day"] = best_day
    out["max_gain_date"] = days[best_day - 1].isoformat()
    out["first_day_at_gain_pct"] = {str(m): marks.get(str(m)) for m in s["gain_marks_pct"]}
    a = atr(bars, sig["entry_day"], s["atr_days"])
    if a:
        stop = e - s["atr_mult"] * a
        out["atr"] = round(a, 4)
        out["atr_stop"] = round(stop, 4)
        out["atr_stop_hit_day"] = next((n for n, d in enumerate(days, 1) if bars[d][2] <= stop), None)
        hit = out["atr_stop_hit_day"]
        out["atr_stop_hit_date"] = days[hit - 1].isoformat() if hit else None
    out["note"] = "untested extras before costs, logged only; never used to decide anything"
    return out


def result_for(sig, n, bars, spy, s):
    """The n-day virtual result after costs and against the tracker, or None when the data is not there."""
    x = exit_day(sig["entry_day"], n)
    e, d0 = _date(sig["entry_day"]), x
    if e not in bars or x not in bars or e not in spy or x not in spy:
        return None
    k = s["fx_fee"] + s["slippage"]
    ret = net_return(bars[e][0], bars[x][3], k)
    tr = net_return(spy[e][0], spy[x][3], s["tracker_cost"])
    return {"exit_day": d0.isoformat(), "entry_open": bars[e][0], "exit_close": bars[x][3],
            "ret_net_pct": round(100 * ret, 4), "tracker_ret_pct": round(100 * tr, 4),
            "vs_tracker_pct": round(100 * (ret - tr), 4), "costs_each_way": k}


def resolve(sd, cfg, today=None, budget_s=None):
    """Fill in what is final: the buy-day open, the 5/10/20/40-day results (closes before today only, so a close
    that is still moving is never used), and the extras. Returns the number of signals updated."""
    s = settings(cfg)
    if not s["scan_enabled"]:
        return 0
    require_demo(cfg)
    today = _date(today or _today_ny())
    rows = load_signals(sd)
    started, budget = _now(), (s["resolve_budget_s"] if budget_s is None else budget_s)
    todo = [r for r in rows if not r.get("done") and r.get("entry_day") and _date(r["entry_day"]) <= today]
    if not todo:
        return 0
    first = min(_date(r["report_day"]) for r in todo) - datetime.timedelta(days=60)
    try:
        spy = daily_bars(s["tracker"], first, today)
    except Exception:
        return 0
    final = add_trading_days(today, -1)            # the last close that is not still moving
    changed = 0
    for r in todo:
        if _now() - started > budget:
            break
        try:
            bars = daily_bars(r["symbol"], _date(r["report_day"]) - datetime.timedelta(days=60), today)
        except Exception as e:
            r["last_error"] = str(e)[:120]
            continue
        e = _date(r["entry_day"])
        if e in bars and r.get("entry_open") is None:
            r["entry_open"] = bars[e][0]
        for n in s["result_days"]:
            if str(n) in r["results"]:
                continue
            x = exit_day(e, n)
            if x > final:
                continue
            res = result_for(r, n, bars, spy, s)
            if res is None and (today - x).days > 7:
                res = {"exit_day": x.isoformat(), "error": "no price data for the buy or sell day"}
            if res is not None:
                r["results"][str(n)] = res
        if _date(r["report_day"]) <= final:
            r["extras"] = extras(r, bars, s, final)
        r["done"] = all(str(n) in r["results"] for n in s["result_days"])
        r["updated_utc"] = datetime.datetime.utcfromtimestamp(_now()).strftime("%Y-%m-%dT%H:%M:%SZ")
        changed += 1
    save_signals(sd, rows)
    return changed


# ---------------------------------------------------------------- phone lines (never raise)

def _day(d):
    return _date(d).strftime("%a %d %b")


def _names(doc, limit=10):
    items = ["%s (+%.0f%% surprise%s)" % (x["symbol"], x["surprise_pct"] or 0,
                                          ", reported " + x["when"] if x.get("when") in WHEN.values() else "")
             for x in (doc.get("shortlist") or [])[:limit]]
    more = len(doc.get("shortlist") or []) - limit
    return ", ".join(items) + (", and %d more" % more if more > 0 else "")


def _line_for(sd, cfg, want_buy_day, head):
    if cfg is not None and not settings(cfg)["scan_enabled"]:
        return None
    doc = read_shortlist(sd)
    if not doc or doc.get("buy_day") != _date(want_buy_day).isoformat():
        return "%s (%s): %s." % (head, SHADOW, NO_SCAN)
    if doc.get("error"):
        return "%s (%s): %s." % (head, SHADOW, NO_SCAN + ", " + doc["error"])
    if not doc.get("shortlist"):
        return "%s (%s): none. %d report%s on %s, %d qualified%s." % (
            head, SHADOW, doc.get("rows", 0), "" if doc.get("rows") == 1 else "s", _day(doc["report_day"]),
            doc.get("qualified", 0), "" if not doc.get("qualified") else ", no free virtual slot" if
            any("slot" in (x.get("why") or "") for x in doc.get("not_shortlisted") or []) else "")
    return "%s (%s): %s. From %s's reports, %d qualified; virtual sell at the close on %s." % (
        head, SHADOW, _names(doc), _day(doc["report_day"]), doc.get("qualified", 0), _day(doc["exit_day"]))


def closing_line(sd, cfg, today):
    """For the closing report on New York day `today`: tomorrow's shortlist. Never raises; None when the scan is off."""
    try:
        return _line_for(sd, cfg, add_trading_days(today, 1), "Drift shortlist for tomorrow")
    except Exception:
        return "Drift shortlist for tomorrow (%s): %s." % (SHADOW, NO_SCAN)


def briefing_line(sd, cfg, session):
    """For the morning briefing on the session day: what the shortlist would buy today. Never raises."""
    try:
        return _line_for(sd, cfg, session, "Drift would buy today")
    except Exception:
        return "Drift would buy today (%s): %s." % (SHADOW, NO_SCAN)


# ---------------------------------------------------------------- command line

def main(argv=None):
    ap = argparse.ArgumentParser(description="Post-earnings drift, SHADOW scan (never places an order).")
    ap.add_argument("what", choices=("scan", "resolve", "show"))
    ap.add_argument("--state-dir", default=os.path.join(ROOT, "trader"))
    ap.add_argument("--force", action="store_true", help="scan again even if today's scan is done")
    a = ap.parse_args(argv)
    try:
        with open(os.path.join(a.state_dir, "config.json"), encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (OSError, ValueError) as e:
        sys.stderr.write("drift: cannot read trader/config.json (%s)\n" % e)
        return 2
    try:
        if a.what == "scan":
            doc = scan(a.state_dir, cfg, force=a.force)
            print("drift scan is off (drift.scan_enabled)" if doc is None else json.dumps(doc, indent=1, sort_keys=True))
        elif a.what == "resolve":
            print("drift: %d signals updated" % resolve(a.state_dir, cfg))
        else:
            print(json.dumps(read_shortlist(a.state_dir), indent=1, sort_keys=True))
            print("%d signals logged" % len(load_signals(a.state_dir)))
    except DriftError as e:
        sys.stderr.write("drift: %s\n" % e)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
