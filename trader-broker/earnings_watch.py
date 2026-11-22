#!/usr/bin/env python3
"""Next results date during a hold (drift and news holdings). REPORT ONLY.

A company's quarterly results are the biggest known event in most holds: the share can jump or drop a lot on
the day. This looks up each held drift or news share's next results date (Nasdaq's company page, which takes
it from Zacks: "expected" when the company has said, "estimated" when it is worked out from past dates) and
flags a holding whose results come BEFORE its sell date.

Where it shows:
  - the daily review: each holding's next results date, and whether it falls inside the hold. The phone is
    told when a date inside the hold is first seen or moves, and again in the last 3 trading days before it
    (the 07:30 briefing already says "it reports earnings today / next session" for anything held).
  - each new news idea (trader-broker/news_ideas.py, its extra information): whether results fall between its
    buy day and its sell date. Logged only.

It never places, cancels or changes an order, never asks, never edits a holdings page and blocks nothing.
Off unless the config has "earnings_watch": {"enabled": true}. Broker-neutral: it reads the holdings record only.
State: trader/state/earnings-watch.json (the last date seen for each page, what the phone was told);
every change goes to earnings-watch.jsonl.

    python3 trader-broker/earnings_watch.py show            # each holding's next results date (network: Nasdaq)
    python3 trader-broker/earnings_watch.py lookup SYMBOL   # one company's next results date

Dates from Nasdaq/Zacks can be wrong or move. Education and tooling, not financial advice.
"""

import argparse
import datetime
import importlib.util
import json
import os
import re
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

URL = "https://api.nasdaq.com/api/analyst/%s/earnings-date"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/120 Safari/537.36")
SOON_DAYS = 3                   # trading days before the date when the phone is told again
WHEN = (("after market close", "after the close"), ("before market open", "before the open"))

_MODS = {}


def _load(folder, name):
    key = (folder, name)
    if key not in _MODS:
        spec = importlib.util.spec_from_file_location("earnwatch_%s_%s" % (folder.replace("-", "_"), name),
                                                      os.path.join(ROOT, folder, name + ".py"))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        _MODS[key] = m
    return _MODS[key]


def _date(v):
    try:
        return datetime.date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def settings(cfg):
    s = (cfg or {}).get("earnings_watch")
    s = s if isinstance(s, dict) else {}
    return {"enabled": s.get("enabled") is True}


def get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json", "Accept-Language": "en-US"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# ---------------------------------------------------------------- one company

def parse(doc, sym):
    """Nasdaq's answer -> {"symbol", "date" (ISO or None), "when", "sure": "expected" | "estimated" | None,
    "quarter", "why"}. Pure."""
    data = (doc or {}).get("data") or {}
    text = " ".join(str(data.get(k) or "") for k in ("reportText", "announcement"))
    out = {"symbol": sym, "date": None, "when": "time not given", "sure": None, "quarter": None, "why": None}
    m = re.search(r"report earnings on\s+(\d{1,2})/(\d{1,2})/(\d{4})", text)
    if m:
        out["date"] = datetime.date(int(m.group(3)), int(m.group(1)), int(m.group(2))).isoformat()
    else:
        m = re.search(r":\s*([A-Z][a-z]{2}) (\d{1,2}), (\d{4})", str(data.get("announcement") or ""))
        if m:
            try:
                out["date"] = datetime.datetime.strptime(" ".join(m.groups()), "%b %d %Y").date().isoformat()
            except ValueError:
                pass
    if not out["date"]:
        out["why"] = "the next results date is not announced yet"
        return out
    low = text.lower()
    out["sure"] = "expected" if "is expected" in low else "estimated" if "estimated" in low else None
    for k, v in WHEN:
        if k in low:
            out["when"] = v
    q = re.search(r"fiscal Quarter ending (\w+ \d{4})", text)
    out["quarter"] = q.group(1) if q else None
    return out


def lookup(sym, fetch=None):
    """One company's next results date. Raises on a failed fetch."""
    raw = (fetch or get)(URL % str(sym).upper().replace(".", "-"))
    return parse(json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw), str(sym).upper())


def trading_days_between(a, b):
    """NYSE trading days after a, up to and including b (0 when b <= a)."""
    al = _load("trader-alerts", "alerts")
    n, d = 0, a
    while d < b:
        d += datetime.timedelta(days=1)
        if al.trading_day(d)[0]:
            n += 1
    return n


def in_window(info, buy, sell):
    """True when the results date falls on or after the buy day and on or before the sell date."""
    d = _date((info or {}).get("date"))
    return bool(d and _date(buy) and _date(sell) and _date(buy) <= d <= _date(sell))


# ---------------------------------------------------------------- the holdings

def path(sd):
    return os.path.join(sd, "state", "earnings-watch.json")


def load(sd):
    try:
        with open(path(sd), encoding="utf-8") as fh:
            doc = json.load(fh)
        if isinstance(doc, dict):
            doc.setdefault("pages", {})
            return doc
    except (OSError, ValueError):
        pass
    return {"pages": {}}


def save(sd, doc):
    os.makedirs(os.path.dirname(path(sd)), exist_ok=True)
    tmp = "%s.%d.tmp" % (path(sd), os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, sort_keys=True)
    os.replace(tmp, path(sd))


def _log(sd, row):
    with open(os.path.join(sd, "state", "earnings-watch.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")


def check(sd, today, fetch=None):
    """Each held drift and news page's next results date. Returns {page id: info} where info adds "in_hold",
    "sell_date", "days_to" (trading days from today) and "tell" (True when the phone should hear it today).
    Saves what it saw. Never raises for one company; never changes an order or a page."""
    hw = _load("trader-broker", "hold_watch")
    pages = hw.held_pages(_load("trader-broker", "holdings").load(sd))
    doc = load(sd)
    out, errors = {}, []
    for p in pages:
        sell = _date((p.get("sell_rule") or {}).get("date"))
        try:
            info = lookup(p["symbol"], fetch)
        except Exception as e:
            errors.append("%s: %s" % (p["symbol"], str(e)[:120]))
            continue
        info["sell_date"] = sell.isoformat() if sell else None
        info["in_hold"] = bool(info["date"] and sell and today <= _date(info["date"]) <= sell)
        info["days_to"] = trading_days_between(today, _date(info["date"])) if info["date"] and _date(info["date"]) >= today else None
        rec = doc["pages"].setdefault(p["id"], {"symbol": p["symbol"]})
        told = rec.get("told") or {}
        tell = False
        if info["in_hold"]:
            if told.get("date") != info["date"]:
                tell = "new"                                   # first seen, or the date moved
            elif info["days_to"] is not None and info["days_to"] <= SOON_DAYS and not told.get("soon"):
                tell = "soon"
        info["tell"] = tell
        if rec.get("date") != info["date"] or rec.get("sure") != info["sure"]:
            _log(sd, {"day": today.isoformat(), "page": p["id"], "symbol": p["symbol"], "date": info["date"],
                      "sure": info["sure"], "when": info["when"], "in_hold": info["in_hold"], "sell_date": info["sell_date"],
                      "was": rec.get("date")})
        rec.update(date=info["date"], sure=info["sure"], when=info["when"], checked=today.isoformat())
        if tell == "new":
            rec["told"] = {"date": info["date"], "on": today.isoformat(), "soon": info["days_to"] is not None
                           and info["days_to"] <= SOON_DAYS}
        elif tell == "soon":
            rec["told"] = dict(told, soon=True)
        out[p["id"]] = info
    doc["errors"] = errors
    save(sd, doc)
    return out, errors


def run(sd, cfg, today, fetch=None, say=None):
    """check(), when switched on. {"on", "by_page", "errors"}. Never raises."""
    if not settings(cfg)["enabled"]:
        return {"on": False, "by_page": {}, "errors": []}
    try:
        got, errors = check(sd, today, fetch)
        if say:
            say("earnings watch: %d holding(s); results inside the hold: %s%s" % (
                len(got), ", ".join("%s %s" % (i["symbol"], i["date"]) for i in got.values() if i["in_hold"]) or "none",
                "" if not errors else "; notes: " + "; ".join(errors[:3])))
        return {"on": True, "by_page": got, "errors": errors}
    except Exception as e:
        if say:
            say("earnings watch: could not check (%s: %s)" % (type(e).__name__, str(e)[:200]))
        return {"on": True, "by_page": {}, "errors": [str(e)[:160]], "failed": True}


def words(info):
    """One line for a log."""
    if not info or not info.get("date"):
        return "next results: not announced yet"
    return "next results %s (%s, %s)%s" % (info["date"], info.get("when"), info.get("sure") or "source unclear",
                                           " INSIDE the hold (sell date %s)" % info["sell_date"] if info.get("in_hold")
                                           else "")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Next results date during a hold (report only).")
    ap.add_argument("cmd", choices=("show", "lookup"))
    ap.add_argument("symbol", nargs="?")
    ap.add_argument("--state-dir", default=os.path.join(ROOT, "trader"))
    a = ap.parse_args(argv)
    if a.cmd == "lookup":
        print(json.dumps(lookup(a.symbol or "AAPL"), indent=1))
        return 0
    hw = _load("trader-broker", "hold_watch")
    for p in hw.held_pages(_load("trader-broker", "holdings").load(a.state_dir)):
        try:
            info = lookup(p["symbol"])
            info["sell_date"] = (p.get("sell_rule") or {}).get("date")
            info["in_hold"] = in_window(info, datetime.date.today(), info["sell_date"])
            print("%-6s %s" % (p["symbol"], words(info)))
        except Exception as e:
            print("%-6s could not look it up (%s)" % (p["symbol"], str(e)[:120]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
