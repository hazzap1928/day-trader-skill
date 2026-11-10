#!/usr/bin/env python3
"""Tradable universe for trader-scanner: the stocks the autopilot may scan and buy, built from rules.

    python3 trader-scanner/universe.py build              # build it now (Wikipedia, Trading 212, Yahoo)
    python3 trader-scanner/universe.py build --no-write   # what it would be, nothing saved
    python3 trader-scanner/universe.py show               # the current list, its age and last changes
    python3 trader-scanner/universe.py changes            # every refresh: who came in, who went out, why

The rule, in order, for each stock in the starting pool:
    0. starting pool: the S&P 500 (Wikipedia's constituents table), or the Russell 1000 from an
       iShares IWB holdings CSV you download yourself ("source": "russell1000", "source_file"),
       or any ticker list ("source": "file")
    1. out if it is on blocked_tickers (blocked always wins)
    2. out if Trading 212 has no US stock for it (the instrument list, matched by symbol)
    3. out if Yahoo has no daily bars for it, or under 15 of the last 20 sessions
    4. out if the last close is under min_price ($5)
    5. out if the average traded value (close x volume) over the last avg_days (20) sessions is
       under min_avg_traded_usd ($20m)
    6. in: everything else

Spreads: no free source gives bid and ask, and a spread estimated from daily bars (Abdi-Ranaldo,
Corwin-Schultz) is noise at large-cap spreads (it put JPM at 0.9 % and AAPL at 0 % on 9 Nov 2026,
against real spreads of about 0.01 %). So there is no spread filter: the $5 price floor and the
$20m a day traded floor are the spread proxy (spreads shrink as price and traded value grow).

Safety: a build that looks broken keeps last week's list (the old file is not touched) and says
why: the pool is too small, over 10 % of Yahoo fetches failed, fewer than min_count stocks
passed, or the list shrank by more than max_drop_pct.

Files (in the state folder, trader/ by default):
    universe/tradable.json   the list, with each stock's figures and every dropped stock's reason
    universe/changes.jsonl   one line per refresh: added and removed, each with its reason
    universe/refresh.json    when the last build started (the autopilot's weekly schedule)

Config (trader/config.json):
    "universe": {"enabled": false, "source": "sp500", "min_avg_traded_usd": 20000000,
                 "min_price": 5, "avg_days": 20,
                 "refresh_day": "sun", "refresh_at": "18:00", "max_age_days": 14}

With "enabled": true and "paper": true, this list replaces allowed_tickers for the scanner, the
risk check, the autopilot and t212.py place. allowed_tickers stays as the fallback when the file
is missing, broken or older than max_age_days, and is what a live account always uses.
blocked_tickers wins everywhere. The always-on autopilot rebuilds it every refresh_day after
refresh_at (UK time on this Mac), or at its next chance outside market hours.

A universe is a list of stocks a rule may look at, not a list of stocks to buy.
Education and tooling, not financial advice.
"""

import argparse
import datetime
import html
import importlib.util
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
SOURCES = ("sp500", "russell1000", "file")
SOURCE_LABEL = {"sp500": "S&P 500", "russell1000": "Russell 1000", "file": "your ticker file"}
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DEFAULTS = {"enabled": False, "source": "sp500", "source_file": None, "min_avg_traded_usd": 20e6,
            "min_price": 5.0, "avg_days": 20, "refresh_day": "sun",
            "refresh_at": "18:00", "max_age_days": 14, "min_count": 100, "max_drop_pct": 25}
POOL_MIN = {"sp500": 450, "russell1000": 900, "file": 1}
MAX_FAIL_PCT = 10        # over this share of Yahoo fetches failing, the build is not saved
MIN_BARS_SHARE = 0.75    # at least 15 of 20 sessions
RETRY_AFTER = 6 * 3600   # a failed weekly build is tried again after this long
PAUSE = 0.3
NOTE = "A universe is a list of stocks a rule may look at, not a list of stocks to buy."


class UniverseError(Exception):
    pass


def _now():
    return time.time()


def _load(folder, name):
    path = os.path.join(ROOT, folder, name + ".py")
    spec = importlib.util.spec_from_file_location("trader_" + name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


_MODS = {}


def mod(folder, name):
    """The scanner and the broker, loaded once and only when a build needs them (they load this file too)."""
    if name not in _MODS:
        _MODS[name] = _load(folder, name)
    return _MODS[name]


# ---------------------------------------------------------------- settings and the shared reader

def settings(cfg):
    """The universe settings with defaults, or None when the config has no "universe" section."""
    raw = (cfg or {}).get("universe")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise UniverseError("universe in trader/config.json must be an object")
    s = dict(DEFAULTS)
    s.update({k: v for k, v in raw.items() if k in DEFAULTS})
    if not isinstance(s["enabled"], bool):
        raise UniverseError("universe.enabled must be true or false")
    if s["source"] not in SOURCES:
        raise UniverseError("universe.source must be one of %s" % ", ".join(SOURCES))
    if s["source"] != "sp500" and not s["source_file"]:
        raise UniverseError("universe.source %s needs universe.source_file (a holdings CSV or a ticker list)"
                            % s["source"])
    for k in ("min_avg_traded_usd", "min_price", "avg_days", "max_age_days", "min_count",
              "max_drop_pct"):
        try:
            s[k] = float(s[k])
        except (TypeError, ValueError):
            raise UniverseError("universe.%s must be a number" % k)
        if s[k] < 0:
            raise UniverseError("universe.%s must not be negative" % k)
    s["avg_days"] = int(s["avg_days"])
    if not 5 <= s["avg_days"] <= 60:
        raise UniverseError("universe.avg_days must be 5 to 60")
    if str(s["refresh_day"]).lower()[:3] not in DAYS:
        raise UniverseError("universe.refresh_day must be a weekday name, like \"sun\"")
    s["refresh_day"] = str(s["refresh_day"]).lower()[:3]
    if not re.match(r"^\d{1,2}:\d{2}$", str(s["refresh_at"])):
        raise UniverseError("universe.refresh_at must be HH:MM (UK time on this Mac)")
    return s


def paths(state_dir):
    d = os.path.join(state_dir, "universe")
    return {"dir": d, "list": os.path.join(d, "tradable.json"), "changes": os.path.join(d, "changes.jsonl"),
            "refresh": os.path.join(d, "refresh.json")}


def _read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _write_json(path, doc):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def read(state_dir):
    """The saved universe, or None when there is none or it is not a valid one."""
    doc = _read_json(paths(state_dir)["list"])
    if not isinstance(doc, dict) or not isinstance(doc.get("tickers"), list) or not doc["tickers"]:
        return None
    syms = [t.get("symbol") for t in doc["tickers"] if isinstance(t, dict)]
    if not all(isinstance(x, str) and x.strip() for x in syms) or not isinstance(doc.get("built_at"), (int, float)):
        return None
    return doc


def symbols(doc):
    return [str(t["symbol"]).strip().upper() for t in doc["tickers"]]


def status(cfg, state_dir, now=None):
    """{"active", "why", "count", "built_at", "source"}: whether the universe replaces allowed_tickers now.
    active only with universe.enabled, "paper": true, and a valid list no older than max_age_days."""
    now = _now() if now is None else now
    try:
        s = settings(cfg)
    except UniverseError as e:
        return {"active": False, "why": str(e), "count": None, "configured": True}
    doc = read(state_dir) if state_dir else None
    out = {"active": False, "configured": s is not None, "count": len(doc["tickers"]) if doc else None,
           "built_at": doc.get("built_at") if doc else None, "source": (doc or {}).get("source")}
    if s is None:
        out["why"] = "no universe in the config: allowed_tickers is the list"
    elif not s["enabled"]:
        out["why"] = "universe.enabled is false: allowed_tickers is the list"
    elif (cfg or {}).get("paper") is not True:
        out["why"] = "live account: allowed_tickers is the list (the universe is demo only)"
    elif not state_dir:
        out["why"] = "no state folder given: allowed_tickers is the list"
    elif doc is None:
        out["why"] = "no valid universe file yet (run universe.py build): allowed_tickers is the list"
    elif now - doc["built_at"] > s["max_age_days"] * 86400:
        out["why"] = "the universe is %.0f days old, over max_age_days %g: allowed_tickers is the list" % (
            (now - doc["built_at"]) / 86400.0, s["max_age_days"])
    else:
        out["active"] = True
        out["why"] = "the %s universe of %d stocks replaces allowed_tickers" % (
            SOURCE_LABEL.get(doc.get("source"), doc.get("source")), len(doc["tickers"]))
    return out


def apply(cfg, state_dir, now=None):
    """(cfg, status). With the universe active, a copy of cfg whose allowed_tickers is the universe;
    otherwise cfg as it was. blocked_tickers is left alone: every reader still takes it out.
    Never write the returned cfg back to config.json."""
    st = status(cfg, state_dir, now)
    if not st["active"]:
        return cfg, st
    out = dict(cfg)
    out["allowed_tickers"] = symbols(read(state_dir))
    out["allowed_from"] = "universe"
    return out, st


def apply_path(cfg, config_path, now=None):
    """apply() for a script that knows only its config path: the state folder is the config's folder."""
    return apply(cfg, os.path.dirname(os.path.abspath(config_path)) if config_path else None, now)


# ---------------------------------------------------------------- the weekly schedule

def _hm(text):
    h, m = str(text).split(":")
    return int(h), int(m)


def last_refresh_moment(s, local_now):
    """The most recent refresh_day refresh_at at or before local_now (a naive local datetime)."""
    h, m = _hm(s["refresh_at"])
    day = DAYS.index(s["refresh_day"])
    back = (local_now.weekday() - day) % 7
    moment = (local_now - datetime.timedelta(days=back)).replace(hour=h, minute=m, second=0, microsecond=0)
    if moment > local_now:
        moment -= datetime.timedelta(days=7)
    return moment


def due(cfg, state_dir, local_now=None, now=None, market_hours=False):
    """(True, why) when the weekly build should start now: a universe section exists, the last good
    build is older than the latest refresh moment, no build started in the last RETRY_AFTER, and the
    market is not in session (the build shares Yahoo with the scanner)."""
    now = _now() if now is None else now
    local_now = local_now or datetime.datetime.fromtimestamp(now)
    try:
        s = settings(cfg)
    except UniverseError as e:
        return False, str(e)
    if s is None:
        return False, "no universe in the config"
    moment = time.mktime(last_refresh_moment(s, local_now).timetuple())
    doc = read(state_dir)
    if doc and doc["built_at"] >= moment:
        return False, "built this week"
    started = (_read_json(paths(state_dir)["refresh"], {}) or {}).get("started_at") or 0
    if now - started < RETRY_AFTER:
        return False, "a build started %.0f minutes ago" % ((now - started) / 60.0)
    if market_hours:
        return False, "waiting for the market to close"
    return True, "weekly refresh (%s %s)" % (s["refresh_day"], s["refresh_at"])


def mark_started(state_dir, now=None):
    _write_json(paths(state_dir)["refresh"], {"started_at": _now() if now is None else now})


# ---------------------------------------------------------------- starting pools

def parse_sp500(text):
    """Wikipedia's 'List of S&P 500 companies' page -> [(SYMBOL, name)] from the constituents table."""
    m = re.search(r'<table[^>]*id="constituents"[^>]*>(.*?)</table>', text, re.S)
    if not m:
        raise UniverseError("no constituents table on the S&P 500 page (Wikipedia changed its layout?)")
    out, seen = [], set()
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", m.group(1), re.S):
        cells = [" ".join(html.unescape(re.sub(r"<[^>]+>", "", c)).split())
                 for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        if len(cells) < 2:
            continue
        sym = cells[0].upper()
        if re.match(r"^[A-Z]{1,5}(\.[A-Z])?$", sym) and sym not in seen:
            seen.add(sym)
            out.append((sym, cells[1]))
    return out


def parse_ishares(text):
    """An iShares holdings CSV (IWB for the Russell 1000) -> [(SYMBOL, name)], equities only.
    iShares writes class shares without the dot (BRKB); the Trading 212 match tries BRK.B too."""
    import csv
    import io
    lines = text.replace("\r", "").split("\n")
    start = next((i for i, ln in enumerate(lines) if ln.startswith("Ticker,")), None)
    if start is None:
        raise UniverseError("not an iShares holdings CSV (no 'Ticker,' header row)")
    out, seen = [], set()
    for row in csv.DictReader(io.StringIO("\n".join(lines[start:]))):
        sym = (row.get("Ticker") or "").strip().upper()
        if (row.get("Asset Class") or "").strip() != "Equity" or not re.match(r"^[A-Z]{1,6}(\.[A-Z])?$", sym):
            continue
        if sym not in seen:
            seen.add(sym)
            out.append((sym, " ".join((row.get("Name") or "").split())))
    return out


def parse_list(text):
    """A plain ticker list: one SYMBOL or SYMBOL|Name a line, # comments."""
    out, seen = [], set()
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        parts = ln.split("|")
        sym = parts[0].strip().upper()
        if sym and sym not in seen:
            seen.add(sym)
            out.append((sym, parts[1].strip() if len(parts) > 1 else ""))
    return out


def load_pool(s, fetch=None, offline=None):
    """[(SYMBOL, name)] for the configured source. fetch(url) -> text; offline = a folder with sp500.html."""
    src = s["source"]
    if src == "sp500":
        if offline:
            with open(os.path.join(offline, "sp500.html"), encoding="utf-8") as fh:
                text = fh.read()
        else:
            try:
                text = (fetch or mod("trader-scanner", "scan").http_get)(SP500_URL)
            except Exception as e:
                raise UniverseError("could not fetch the S&P 500 list from Wikipedia: %s" % getattr(e, "reason", e))
        pool = parse_sp500(text)
    else:
        p = s["source_file"]
        if not os.path.isabs(p):
            p = os.path.join(ROOT, p)
        if not os.path.exists(p):
            raise UniverseError("no file at %s (universe.source_file)" % p)
        with open(p, encoding="utf-8-sig", errors="replace") as fh:
            text = fh.read()
        pool = parse_ishares(text) if src == "russell1000" else parse_list(text)
    if len(pool) < POOL_MIN[src]:
        raise UniverseError("the %s list has only %d stocks (expected at least %d): not trusted"
                            % (SOURCE_LABEL[src], len(pool), POOL_MIN[src]))
    return pool


# ---------------------------------------------------------------- Trading 212 and the figures

def t212_index(instruments):
    """{SYMBOL: instrument} for Trading 212's US stocks, by the symbol it shows (shortName: META is
    FB_US_EQ) and by its ticker (BRK_B_US_EQ is BRK.B)."""
    by = {}
    for inst in instruments or []:
        t = str(inst.get("ticker") or "")
        if not t.upper().endswith("_US_EQ") or str(inst.get("type") or "").upper() != "STOCK":
            continue
        if inst.get("currencyCode") not in (None, "USD"):
            continue
        short = str(inst.get("shortName") or "").upper()
        if short:
            by.setdefault(short, inst)
    for inst in instruments or []:
        t = str(inst.get("ticker") or "")
        if not t.upper().endswith("_US_EQ") or str(inst.get("type") or "").upper() != "STOCK":
            continue
        base = t[:-len("_US_EQ")].upper()
        by.setdefault(base.replace("_", ".").replace("/", "."), inst)
    return by


def match_t212(sym, index):
    """The Trading 212 instrument for a symbol, or None. iShares' BRKB is tried as BRK.B."""
    for cand in (sym, sym[:-1] + "." + sym[-1] if len(sym) > 2 and "." not in sym else None):
        if cand and cand in index:
            return cand, index[cand]
    return None, None


def figures(series, avg_days, now_utc):
    """A Yahoo daily series -> {"price", "adv_usd", "bars"} over the last avg_days complete
    sessions (today's bar is left out while the market is open)."""
    scan = mod("trader-scanner", "scan")
    now_et = scan.to_eastern(now_utc)
    rows = []
    for i, t in enumerate(series.get("t") or []):
        c = series["c"][i]
        v = (series.get("v") or [None] * (i + 1))[i]
        if c is None or v is None:
            continue
        d = scan.ts_to_eastern(t).date()
        if d == now_et.date() and now_et.time() < scan.CLOSE:
            continue
        rows.append((c, v))
    rows = rows[-avg_days:]
    if not rows:
        return {"bars": 0}
    adv = sum(c * v for c, v in rows) / len(rows)
    return {"bars": len(rows), "price": round(rows[-1][0], 4), "adv_usd": round(adv)}


def _m(x):
    return "$%.1fm" % (x / 1e6)


def judge(sym, f, s):
    """None when the stock passes the liquidity filters, else the reason it does not."""
    if f.get("bars", 0) < MIN_BARS_SHARE * s["avg_days"]:
        return "only %d of the last %d sessions have daily bars" % (f.get("bars", 0), s["avg_days"])
    if f["price"] < s["min_price"]:
        return "last close $%.2f is under $%g" % (f["price"], s["min_price"])
    if f["adv_usd"] < s["min_avg_traded_usd"]:
        return "average traded value %s a day (%d sessions) is under %s" % (
            _m(f["adv_usd"]), f["bars"], _m(s["min_avg_traded_usd"]))
    return None


# ---------------------------------------------------------------- the build

def build(cfg, instruments, pool, daily, now=None, previous=None):
    """Pure: the new universe and its changes against the previous one.
    pool = [(SYM, name)]; daily(sym) -> Yahoo series or raises; instruments = Trading 212's list.
    Returns (doc, change). Raises UniverseError when the result must not replace the old list."""
    now = _now() if now is None else now
    now_utc = datetime.datetime.utcfromtimestamp(now)
    s = settings(cfg)
    if s is None:
        s = settings({"universe": {}})
    blocked = {str(x).strip().upper() for x in (cfg or {}).get("blocked_tickers") or []}
    index = t212_index(instruments)
    if not index:
        raise UniverseError("the Trading 212 instrument list has no US stocks: not trusted")
    kept, dropped, fetched, failed = [], {}, 0, 0
    for sym, name in pool:
        if sym in blocked:
            dropped[sym] = "on blocked_tickers"
            continue
        shown, inst = match_t212(sym, index)
        if inst is None:
            dropped[sym] = "not on Trading 212 (no US stock with this symbol)"
            continue
        fetched += 1
        try:
            series = daily(shown)
        except Exception as e:
            failed += 1
            dropped[shown] = "no daily data from Yahoo: %s" % str(e)[:120]
            continue
        f = figures(series, s["avg_days"], now_utc)
        why = judge(shown, f, s)
        if why:
            dropped[shown] = why
            continue
        kept.append(dict({"symbol": shown, "t212": inst.get("ticker"), "name": name or inst.get("name") or ""}, **f))
    if fetched and failed * 100.0 / fetched > MAX_FAIL_PCT:
        raise UniverseError("Yahoo failed for %d of %d stocks (over %d%%): last week's list stays"
                            % (failed, fetched, MAX_FAIL_PCT))
    if len(kept) < s["min_count"]:
        raise UniverseError("only %d stocks passed (min_count %g): last week's list stays" % (len(kept), s["min_count"]))
    prev_syms = set(symbols(previous)) if previous else set()
    if prev_syms and len(kept) < len(prev_syms) * (1 - s["max_drop_pct"] / 100.0):
        raise UniverseError("the list would shrink from %d to %d stocks (over max_drop_pct %g%%): last week's "
                            "list stays" % (len(prev_syms), len(kept), s["max_drop_pct"]))
    kept.sort(key=lambda r: -r["adv_usd"])
    doc = {"built_at": round(now, 3), "built": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"), "source": s["source"],
           "rules": {k: s[k] for k in ("min_avg_traded_usd", "min_price", "avg_days")},
           "pool": len(pool), "pool_symbols": sorted(p[0] for p in pool), "count": len(kept),
           "tickers": kept, "dropped": dropped, "note": NOTE}
    return doc, diff(previous, doc)


def diff(previous, doc):
    """Who came in and who went out since the previous list, each with the reason."""
    new = {t["symbol"]: t for t in doc["tickers"]}
    if not previous:
        return {"at": doc["built_at"], "built": doc["built"], "count": doc["count"], "first": True,
                "added": [], "removed": []}
    old = set(symbols(previous))
    old_pool = set(previous.get("pool_symbols") or [])
    old_dropped = previous.get("dropped") or {}
    pool = set(doc["pool_symbols"])
    label = SOURCE_LABEL.get(doc["source"], doc["source"])
    added, removed = [], []
    for sym in sorted(set(new) - old):
        t = new[sym]
        if old_pool and sym not in old_pool:
            why = "joined the %s" % label
        elif sym in old_dropped:
            why = "was out (%s); now passes" % old_dropped[sym]
        else:
            why = "passes every filter"
        added.append({"symbol": sym, "why": "%s: %s a day, $%.2f" % (why, _m(t["adv_usd"]), t["price"])})
    for sym in sorted(old - set(new)):
        if sym not in pool:
            why = "left the %s" % label
        else:
            why = doc["dropped"].get(sym, "no longer passes")
        removed.append({"symbol": sym, "why": why})
    return {"at": doc["built_at"], "built": doc["built"], "count": doc["count"], "previous": len(old),
            "added": added, "removed": removed}


def save(state_dir, doc, change):
    p = paths(state_dir)
    _write_json(p["list"], doc)
    with open(p["changes"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps(change) + "\n")


def log_failure(state_dir, why, now=None):
    p = paths(state_dir)
    os.makedirs(p["dir"], exist_ok=True)
    with open(p["changes"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"at": _now() if now is None else now, "failed": why}) + "\n")


def change_text(change, doc, limit=8):
    """The phone message after a refresh."""
    label = SOURCE_LABEL.get(doc["source"], doc["source"])
    if change.get("first"):
        return ("UNIVERSE built (demo): %d stocks from the %s pass the filters (Trading 212, $%g+, %s+ a day "
                "traded)." % (doc["count"], label, doc["rules"]["min_price"], _m(doc["rules"]["min_avg_traded_usd"])))
    lines = ["UNIVERSE refreshed (demo): %d stocks (was %d), %d in, %d out." % (
        doc["count"], change.get("previous", 0), len(change["added"]), len(change["removed"]))]
    for tag, rows in (("In", change["added"]), ("Out", change["removed"])):
        for r in rows[:limit]:
            lines.append("%s: %s, %s" % (tag, r["symbol"], r["why"]))
        if len(rows) > limit:
            lines.append("%s: and %d more (universe.py changes)" % (tag, len(rows) - limit))
    return "\n".join(lines)


def briefing_line(cfg, state_dir, now=None):
    """One line for the morning briefing: what the autopilot may trade today and where the list came from."""
    st = status(cfg, state_dir, now)
    doc = read(state_dir)
    if st["active"]:
        when = datetime.datetime.fromtimestamp(doc["built_at"]).strftime("%a %d %b")
        last = last_change(state_dir)
        moved = ""
        if last and not last.get("first") and not last.get("failed"):
            moved = ", %d in and %d out at the last refresh" % (len(last["added"]), len(last["removed"]))
        return "Universe: %d stocks (%s, filtered; built %s%s)" % (
            len(doc["tickers"]), SOURCE_LABEL.get(doc.get("source"), doc.get("source")), when, moved)
    try:
        n = len(mod("trader-broker", "t212").allowed_tickers(cfg) or [])
    except Exception:
        n = len((cfg or {}).get("allowed_tickers") or [])
    extra = (" A filtered universe of %d is built but not in use (%s)." % (len(doc["tickers"]), st["why"])
             if doc and st.get("configured") else "")
    if st.get("configured") and not doc:
        extra = " (%s)" % st["why"]
    return "Universe: %d tickers from allowed_tickers.%s" % (n, extra)


def last_change(state_dir):
    try:
        with open(paths(state_dir)["changes"], encoding="utf-8") as fh:
            lines = [ln for ln in fh if ln.strip()]
    except OSError:
        return None
    try:
        return json.loads(lines[-1]) if lines else None
    except ValueError:
        return None


# ---------------------------------------------------------------- command line

def _daily_fetcher(pause, offline=None):
    scan = mod("trader-scanner", "scan")
    if offline:
        data, failed = scan.load_offline(offline)

        def daily(sym):
            if sym in data:
                return data[sym]
            raise UniverseError(failed.get(sym, "not in the offline data"))
        return daily
    n = [0]

    def daily(sym):
        if n[0] and pause:
            scan.SLEEP(pause)
        n[0] += 1
        return scan.parse_chart(scan.yahoo_get(scan.chart_path(sym, "3mo", "1d")), sym)
    return daily


def _instruments(a, environ):
    """Trading 212's instrument list: the 24-hour cache, else one call (1 a 50 s at most, spaced by t212.py)."""
    if a.instruments:
        doc = _read_json(a.instruments)
        return doc.get("instruments") if isinstance(doc, dict) else doc
    t212 = mod("trader-broker", "t212")
    env = t212.pick_env(False, environ)
    key, secret = t212.credentials(environ)
    client = t212.Client(env, key, secret, state_dir=a.state_dir)
    try:
        cached, _ = t212.load_instruments(client, a.state_dir)
    except t212.T212Error as e:
        raise UniverseError("Trading 212 instrument list: %s" % t212.scrub(e, client._secrets))
    return cached["instruments"]


def _notify(a, text, environ):
    if not a.notify:
        return
    try:
        mod("trader-broker", "t212").phone_module().push(a.state_dir, text, environ=environ)
    except Exception as e:
        print("universe.py: phone message not sent: %s" % e, file=sys.stderr)


def cmd_build(a, environ):
    t0 = _now()
    cfg = _read_json(os.path.join(a.state_dir, "config.json"), {}) or {}
    if not a.no_write:
        mark_started(a.state_dir)
    try:
        s = settings(cfg) or settings({"universe": {}})
        pool = load_pool(s, offline=a.offline)
        print("Starting pool: %d stocks (%s)." % (len(pool), SOURCE_LABEL[s["source"]]), file=sys.stderr)
        inst = _instruments(a, environ)
        print("Trading 212: %d instruments. Fetching daily bars from Yahoo..." % len(inst or []), file=sys.stderr)
        doc, change = build(cfg, inst, pool, _daily_fetcher(a.pause, a.offline), previous=read(a.state_dir))
    except UniverseError as e:
        if not a.no_write:
            log_failure(a.state_dir, str(e))
        _notify(a, "UNIVERSE NOT REFRESHED (demo): %s" % e, environ)
        raise
    doc["seconds"] = round(_now() - t0, 1)
    if not a.no_write:
        save(a.state_dir, doc, change)
        _notify(a, change_text(change, doc), environ)
    if a.json:
        print(json.dumps({"count": doc["count"], "pool": doc["pool"], "added": change["added"],
                          "removed": change["removed"], "seconds": doc["seconds"]}, indent=2))
        return 0
    reasons = {}
    for why in doc["dropped"].values():
        key = re.sub(r"\$\d[\d.,]*m?|\b\d+(?= sessions)", "N", why.split(":")[0])
        reasons[key] = reasons.get(key, 0) + 1
    print("Universe: %d of %d %s stocks pass (%.0f s)." % (doc["count"], doc["pool"], SOURCE_LABEL[doc["source"]],
                                                          doc["seconds"]))
    for k, v in sorted(reasons.items(), key=lambda kv: -kv[1]):
        print("  out: %d %s" % (v, k))
    print(change_text(change, doc, limit=50))
    if not a.no_write:
        print("Wrote %s and a line in %s." % (paths(a.state_dir)["list"], paths(a.state_dir)["changes"]))
    print(NOTE)
    return 0


def cmd_show(a, environ):
    cfg = _read_json(os.path.join(a.state_dir, "config.json"), {}) or {}
    doc = read(a.state_dir)
    st = status(cfg, a.state_dir)
    print(briefing_line(cfg, a.state_dir))
    print("In use: %s (%s)." % ("yes" if st["active"] else "no", st["why"]))
    if doc:
        print("Built %s from %d stocks; rules %s." % (doc["built"], doc["pool"], json.dumps(doc["rules"])))
        for t in doc["tickers"][:a.show]:
            print("  %-6s %-14s %10s a day  $%9.2f" % (t["symbol"], t["t212"], _m(t["adv_usd"]), t["price"]))
        if len(doc["tickers"]) > a.show:
            print("  ... and %d more (--show N)." % (len(doc["tickers"]) - a.show))
    return 0


def cmd_changes(a, environ):
    try:
        with open(paths(a.state_dir)["changes"], encoding="utf-8") as fh:
            rows = [json.loads(ln) for ln in fh if ln.strip()]
    except OSError:
        print("No refreshes yet.")
        return 0
    for r in rows[-a.last:]:
        when = datetime.datetime.fromtimestamp(r["at"]).strftime("%a %d %b %Y %H:%M")
        if r.get("failed"):
            print("%s  NOT refreshed: %s" % (when, r["failed"]))
            continue
        print("%s  %d stocks%s" % (when, r["count"], " (first build)" if r.get("first") else ""))
        for x in r.get("added", []):
            print("    + %-6s %s" % (x["symbol"], x["why"]))
        for x in r.get("removed", []):
            print("    - %-6s %s" % (x["symbol"], x["why"]))
    return 0


def main(argv=None, environ=None):
    environ = os.environ if environ is None else environ
    ap = argparse.ArgumentParser(description="Tradable universe (trader-scanner): S&P 500 stocks that pass the rules.")
    ap.add_argument("--state-dir", default=os.path.join(ROOT, "trader"))
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("build", help="build the universe now")
    p.add_argument("--offline", metavar="DIR", help="sp500.html and <T>.json daily charts instead of the network")
    p.add_argument("--instruments", metavar="FILE", help="a Trading 212 instrument list instead of the API")
    p.add_argument("--no-write", action="store_true")
    p.add_argument("--notify", action="store_true", help="send the result to your phone")
    p.add_argument("--pause", type=float, default=PAUSE)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("show", help="the current list")
    p.add_argument("--show", type=int, default=20)
    p = sub.add_parser("changes", help="who came in and went out at each refresh")
    p.add_argument("--last", type=int, default=10)
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"build": cmd_build, "show": cmd_show, "changes": cmd_changes}[a.cmd](a, environ)
    except UniverseError as e:
        print("universe.py: %s" % e, file=sys.stderr)
        return 1
    except Exception as e:
        print("universe.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
