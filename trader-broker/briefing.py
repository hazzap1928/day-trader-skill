#!/usr/bin/env python3
"""Morning briefing (read-only): a short message to your phone, the full one to trader/briefings/.

    python3 trader-broker/briefing.py            # launchd runs it at 07:30 UK time on weekdays
    python3 trader-broker/briefing.py --force    # on a weekend or holiday too (about the next session)
    python3 trader-broker/briefing.py --no-send  # write the file and print it, send nothing

What it reads, and from where (anything that fails says "no data", never a guess):
  1. Account: Trading 212 (demo) total value and cash. Yesterday's result comes from
     trader/state/account-days.json, which the autopilot writes at the open and close.
  2. Positions: Trading 212 positions and pending orders (is there a sell stop?), plus
     trader/logs/guard-alerts.jsonl and "unclear" in trader/state/t212-guard.json.
  3. Market mood: Yahoo's chart for S&P 500, Nasdaq 100 and Dow futures (ES=F, NQ=F,
     YM=F), Yahoo's GBP/USD, Yahoo's S&P 500 headline feed, and Nasdaq's economic
     calendar (US releases only).
  4. Earnings: Nasdaq's earnings calendar for the session and the next one, for what
     you hold and what the scanner or autopilot flagged.
  5. A to-do list, most urgent first, from the rules in todo().
  6. Cash allocation (demo, with "allocation" enabled): the day's plan from trader-broker/allocation.py,
     with up to two reservations for stocks on the list that reported earnings after the last close or
     report before today's open (ranked by money traded a day). The plan is written to
     trader/state/allocation.json for the autopilot. A plan is a plan, never a buy.

Read-only: it only GETs. It never places, changes or cancels an order. Education and
tooling, not financial advice.
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
import urllib.request
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
UA_SIMPLE = "Mozilla/5.0 (compatible; day-trader-skill/2.0)"
UA_BROWSER = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/120.0 Safari/537.36")
FUTURES = (("ES=F", "S&P 500"), ("NQ=F", "Nasdaq 100"), ("YM=F", "Dow"))
CHART = "https://query1.finance.yahoo.com/v8/finance/chart/%s?range=5d&interval=1d"
NEWS = "https://feeds.finance.yahoo.com/rss/2.0/headline?s=%5EGSPC&region=US&lang=en-US"
EARNINGS = "https://api.nasdaq.com/api/calendar/earnings?date=%s"
ECON = "https://api.nasdaq.com/api/calendar/economicevents?date=%s"
BIG_RELEASES = ("cpi", "consumer price", "nonfarm", "payroll", "fomc", "fed interest rate", "interest rate decision",
                "gdp", "pce", "retail sales", "ppi", "producer price", "unemployment rate", "jobless claims", "ism")
FLAT = 0.1          # % move counted as flat


def _load(folder, name):
    spec = importlib.util.spec_from_file_location("briefing_" + name, os.path.join(ROOT, folder, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


t212 = _load("trader-broker", "t212")
tk = t212.ticket_module()
phone_mod = t212.phone_module()
net = t212.net_module()
alerts = _load("trader-alerts", "alerts")
allocation = _load("trader-broker", "allocation")


# ---------------------------------------------------------------- seams the tests replace

def _now():
    return time.time()


def _sleep(s):
    time.sleep(s)


def _fetch(url, browser=False, timeout=20):
    """One GET with no credentials. Returns bytes; raises on any failure."""
    req = urllib.request.Request(url, headers={"User-Agent": UA_BROWSER if browser else UA_SIMPLE,
                                               "Accept": "application/json, text/xml, */*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _utc():
    return datetime.datetime.fromtimestamp(_now(), datetime.timezone.utc).replace(tzinfo=None, microsecond=0)


def _local(utc_naive):
    """UTC -> the Mac's own clock (UK time)."""
    return utc_naive.replace(tzinfo=datetime.timezone.utc).astimezone()


def _gbp(x):
    return "£{:,.2f}".format(x) if x is not None else "no data"


def _signed_gbp(x):
    return ("+" if x >= 0 else "-") + "£{:,.2f}".format(abs(x))


def _next_trading(d, step=1):
    for _ in range(15):
        d += datetime.timedelta(days=step)
        if alerts.trading_day(d)[0]:
            return d
    return d


# ---------------------------------------------------------------- 1 and 2: account and positions

def account(client, state_dir, session):
    out = {"total": None, "cash": None, "yesterday": None, "yesterday_day": None, "error": None}
    try:
        s = client.get(t212.SUMMARY) or {}
        out["total"] = t212._num(s.get("totalValue"))
        out["cash"] = t212._num((s.get("cash") or {}).get("availableToTrade"))
        out["currency"] = s.get("currency")
    except t212.T212Error as e:
        out["error"] = t212.scrub(e, client._secrets)
    days = (t212._read_json(os.path.join(state_dir, "state", "account-days.json"), {}) or {}).get("days") or {}
    prev = _next_trading(session, -1).isoformat()
    d = days.get(prev) or {}
    if d.get("start") is not None and d.get("close") is not None:
        out["yesterday"] = float(d["close"]) - float(d["start"])
        out["yesterday_day"] = prev
    return out


def positions(client, state_dir, since_utc):
    out = {"rows": [], "alerts": [], "unclear": {}, "error": None}
    try:
        pos = client.get(t212.POSITIONS) or []
        pending = client.get(t212.ORDERS) or []
    except t212.T212Error as e:
        out["error"] = t212.scrub(e, client._secrets)
        pos, pending = [], []
    for p in pos:
        ticker = t212._o_ticker(p)
        qty = t212._num(p.get("quantity")) or 0
        if not ticker or qty <= 0:
            continue
        stops, others = t212.guard_sell_orders(pending, ticker)
        covered = sum(abs(t212._num(o.get("quantity")) or 0) for o in stops)
        w = p.get("walletImpact") or {}
        out["rows"].append({"ticker": ticker, "symbol": ticker.split("_")[0].upper(), "qty": qty,
                            "avg": t212._num(p.get("averagePricePaid")), "price": t212._num(p.get("currentPrice")),
                            "pl": t212._num(w.get("unrealizedProfitLoss")),
                            "stop": t212._num(stops[0].get("stopPrice")) if stops else None,
                            "covered": covered, "fully_covered": covered + 1e-9 >= qty})
    path = os.path.join(state_dir, "logs", "guard-alerts.jsonl")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    a = json.loads(line)
                    at = datetime.datetime.strptime(a["at"], "%Y-%m-%dT%H:%M:%SZ")
                except (ValueError, KeyError):
                    continue
                if at >= since_utc:
                    out["alerts"].append({"at": at, "text": a.get("text", "")})
    out["unclear"] = (t212._read_json(os.path.join(state_dir, "state", "t212-guard.json"), {}) or {}).get("unclear") or {}
    return out


# ---------------------------------------------------------------- 3: market mood

def futures():
    out = []
    for sym, name in FUTURES:
        try:
            meta = json.loads(_fetch(CHART % sym.replace("=", "%3D")).decode("utf-8"))["chart"]["result"][0]["meta"]
            pct = meta.get("regularMarketChangePercent")
            if pct is None:
                raise KeyError("no change")
            out.append({"symbol": sym, "name": name, "price": float(meta["regularMarketPrice"]), "pct": float(pct),
                        "time": meta.get("regularMarketTime")})
        except Exception:
            out.append({"symbol": sym, "name": name, "price": None, "pct": None})
    return out


def mood_word(fut):
    got = [f["pct"] for f in fut if f.get("pct") is not None]
    if not got:
        return "no data"
    avg = sum(got) / len(got)
    if abs(avg) < FLAT:
        return "flat"
    return "up" if avg > 0 else "down"


def fx():
    try:
        rate, when, src = t212.fetch_gbpusd()
        return {"rate": rate, "when": when, "src": src}
    except t212.T212Error:
        return {"rate": None}


def headlines(limit=5, hours=24):
    try:
        root = ET.fromstring(_fetch(NEWS))
    except Exception:
        return None
    out = []
    cutoff = _utc() - datetime.timedelta(hours=hours)
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        when = None
        try:
            when = datetime.datetime.strptime((item.findtext("pubDate") or "").strip()[:25],
                                              "%a, %d %b %Y %H:%M:%S")
        except ValueError:
            pass
        if title and (when is None or when >= cutoff):
            out.append({"title": html.unescape(title), "link": (item.findtext("link") or "").strip()})
        if len(out) >= limit:
            break
    return out


def econ(day):
    """US releases for the day from Nasdaq's calendar, or None on any failure."""
    try:
        rows = json.loads(_fetch(ECON % day.isoformat(), browser=True).decode("utf-8"))["data"]["rows"] or []
    except Exception:
        return None
    out = []
    for r in rows:
        if str(r.get("country", "")).strip() != "United States":
            continue
        name = html.unescape(str(r.get("eventName") or "")).strip()
        uk = None
        m = re.match(r"^(\d{1,2}):(\d{2})$", str(r.get("gmt") or "").strip())
        if m:     # the column says "gmt" but holds New York time (jobless claims show as 08:30)
            t = datetime.datetime.combine(day, datetime.time(int(m.group(1)), int(m.group(2))))
            uk = _local(alerts.eastern_to_utc(t)).strftime("%H:%M")
        out.append({"name": name, "uk_time": uk, "consensus": str(r.get("consensus") or "").strip(),
                    "previous": str(r.get("previous") or "").strip(),
                    "big": any(k in name.lower() for k in BIG_RELEASES)})
    return out


# ---------------------------------------------------------------- 4: earnings

MAX_FLAGGED = 20


def flagged(state_dir, cfg=None, days=5):
    """Tickers the scanner or the autopilot flagged in the last few days: the scanner's top
    MAX_FLAGGED hits (on your allow-list, when there is one), and every autopilot ticket."""
    out = set()
    wl = t212._read_json(os.path.join(state_dir, "watchlist.json"), {}) or {}
    as_of = _as_of(wl.get("as_of"))
    try:
        allow = t212.allowed_tickers(cfg or {})
    except t212.T212Error:
        allow = None
    if as_of is not None and _now() - as_of <= days * 86400:
        hits = [str(h.get("symbol")).upper() for h in wl.get("hits") or [] if isinstance(h, dict) and h.get("symbol")]
        out |= set([h for h in hits if allow is None or h in allow][:MAX_FLAGGED])
    ap = t212._read_json(os.path.join(state_dir, "state", "autopilot.json"), {}) or {}
    out |= {str(s).upper() for s in (ap.get("tried") or {})}
    out |= {str(x.get("symbol")).upper() for x in ap.get("tickets") or [] if x.get("symbol")}
    return out


def _as_of(text):
    """The watchlist's "2026-11-06 12:02 ET" (or ISO with Z) as a UTC timestamp, or None."""
    t = str(text or "").strip()
    m = re.match(r"^(\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2})(?: ET)?$", t)
    if m:
        local = datetime.datetime.strptime("%s %s:%s" % m.groups(), "%Y-%m-%d %H:%M")
        return alerts.eastern_to_utc(local).replace(tzinfo=datetime.timezone.utc).timestamp()
    return t212._iso_ts(t)


def earnings(days, held, watched):
    """{day: [rows for held or watched symbols]} or {day: None} when the calendar did not answer."""
    out = {}
    want = held | watched
    for d in days:
        try:
            rows = json.loads(_fetch(EARNINGS % d.isoformat(), browser=True).decode("utf-8"))["data"]["rows"] or []
        except Exception:
            out[d] = None
            continue
        when = {"time-pre-market": "before the open", "time-after-hours": "after the close"}
        out[d] = [{"symbol": r["symbol"].upper(), "name": r.get("name", ""), "held": r["symbol"].upper() in held,
                   "when": when.get(r.get("time"), "time not given")}
                  for r in rows if str(r.get("symbol", "")).upper() in want]
    return out


# ---------------------------------------------------------------- 5: to-do, most urgent first

def todo(b):
    """[(priority, text)]: 1 act now, 2 before the open, 3 today, 4 note."""
    items = []
    if b["account"].get("error") or b["positions"].get("error"):
        items.append((1, "Trading 212 did not answer (%s). Check the API key: the guard and autopilot need it."
                      % (b["account"].get("error") or b["positions"].get("error"))))
    for t, why in sorted(b["positions"]["unclear"].items()):
        items.append((1, "%s has an UNCLEAR guard order (%s). Check it in the app, then clear it in "
                         "trader/state/t212-guard.json." % (t, why)))
    if b["positions"]["alerts"]:
        items.append((1, "%d guard alert%s since the last close; latest: %s" % (
            len(b["positions"]["alerts"]), "" if len(b["positions"]["alerts"]) == 1 else "s",
            b["positions"]["alerts"][-1]["text"][:160])))
    for r in b["positions"]["rows"]:
        if not r["fully_covered"]:
            items.append((1, "%s: %s held with %s. Set a stop in the app, or let the guard put the ticket's "
                             "stop back after the open." % (r["symbol"], t212._qty(r["qty"]),
                                                            "NO stop" if not r["covered"] else
                                                            "only %s covered by a stop" % t212._qty(r["covered"]))))
    for d, rows in sorted(b["earnings"].items()):
        for e in rows or []:
            if e["held"]:
                items.append((1 if d == b["session"] else 2, "You hold %s and it reports earnings %s %s. Decide "
                              "before then whether to keep it through the report." % (
                                  e["symbol"], "today" if d == b["session"] else "on %s" % d.strftime("%a"), e["when"])))
    if b["positions"]["rows"]:
        names = ", ".join(r["symbol"] for r in b["positions"]["rows"])
        eod = b.get("eod") or {}
        if eod.get("date") == b["prev"].isoformat():
            items.append((2, "Still held overnight: %s, although the end-of-day close ran on %s. Check the app and "
                             "trader/logs/guard.log." % (names, b["prev"].strftime("%a"))))
        else:
            items.append((3, "Held overnight: %s. The 15:50 New York close on %s sells it if eod_close is on."
                          % (names, b["session"].strftime("%a"))))
    big = [e for e in b["econ"] or [] if e["big"]]
    if big:
        items.append((3, "Big US release%s today: %s. Expect sharp moves around then." % (
            "" if len(big) == 1 else "s",
            "; ".join("%s at %s UK" % (e["name"], e["uk_time"] or "a time not given") for e in big[:3]))))
    near = ["%s %s" % (e["symbol"], "today" if d == b["session"] else d.strftime("%a"))
            for d, rows in sorted(b["earnings"].items()) for e in rows or [] if not e["held"]]
    if near:
        items.append((4, "Flagged recently and reporting soon (a breakout near earnings can reverse): %s."
                      % ", ".join(near)))
    if b["mood"] == "no data" or b["fx"].get("rate") is None:
        items.append((4, "Some market data did not load (see the full briefing); nothing was guessed."))
    if not items:
        items.append((4, "Nothing urgent. The guard and autopilot start at 13:00 UK and wait for the open."))
    items.sort(key=lambda x: x[0])
    return items


# ---------------------------------------------------------------- putting it together

def build(client, state_dir):
    now = _utc()
    today_ny = tk.to_eastern(now).date()
    session = today_ny if alerts.trading_day(today_ny)[0] else _next_trading(today_ny)
    prev = _next_trading(session, -1)
    close_utc = alerts.eastern_to_utc(datetime.datetime.combine(prev, datetime.time(16, 0)))
    acct = account(client, state_dir, session)
    pos = positions(client, state_dir, close_utc)
    held = {r["symbol"] for r in pos["rows"]}
    fut = futures()
    cfg = t212._read_json(os.path.join(state_dir, "config.json"), {}) or {}
    b = {"generated": now, "session": session, "next": _next_trading(session), "prev": prev,
         "account": acct, "positions": pos, "futures": fut, "mood": mood_word(fut), "fx": fx(),
         "news": headlines(), "econ": econ(session),
         "flagged": sorted(flagged(state_dir, t212.with_universe(cfg, state_dir))),
         "universe": universe_line(cfg, state_dir),
         "eod": (t212._read_json(os.path.join(state_dir, "state", "t212-guard.json"), {}) or {}).get("eod")}
    b["earnings"] = earnings([session, b["next"]], held, set(b["flagged"]))
    b["allocation"] = allocation_line(cfg, state_dir, session, prev, held)
    b["todo"] = todo(b)
    b["drift"] = drift_line(state_dir, session, "briefing")
    return b


def all_earnings(day):
    """Every row of Nasdaq's earnings calendar for `day`, as [{"symbol", "when"}], or None on failure."""
    try:
        rows = json.loads(_fetch(EARNINGS % day.isoformat(), browser=True).decode("utf-8"))["data"]["rows"] or []
    except Exception:
        return None
    when = {"time-pre-market": "before the open", "time-after-hours": "after the close"}
    return [{"symbol": str(r.get("symbol") or "").upper(), "when": when.get(r.get("time"), "time not given")}
            for r in rows if r.get("symbol")]


def allocation_line(cfg, state_dir, session, prev, held):
    """Make (or keep) the day's cash allocation plan and return its one line, or None with allocation off.
    Demo only: allocation.active() is false unless "paper" is true."""
    if not allocation.active(cfg):
        return None
    try:
        day = session.isoformat()
        plan = allocation.load_plan(state_dir, day)
        if plan is None:
            ucfg = t212.with_universe(cfg, state_dir)
            allowed = t212.allowed_tickers(ucfg) or []
            uni = t212._read_json(os.path.join(state_dir, "universe", "tradable.json"), {}) or {}
            ranks = {str(x.get("symbol") or "").upper(): x.get("adv_usd") or 0
                     for x in uni.get("tickers") or [] if isinstance(x, dict)}
            today_rows, prev_rows = all_earnings(session), all_earnings(prev)
            cands = allocation.catalysts(today_rows, prev_rows, allowed, ranks, held)
            plan = allocation.save_plan(state_dir, allocation.make_plan(cfg, day, cands))
            if today_rows is None or prev_rows is None:
                return allocation.plan_text(plan) + " (earnings calendar: no data, so no reservations from it)"
        return allocation.plan_text(plan)
    except (allocation.AllocationError, t212.T212Error) as e:
        return "Allocation (demo): no plan (%s). Every buy uses the free pool." % e


def universe_line(cfg, state_dir):
    """How many stocks the autopilot may trade today, and where the list came from."""
    try:
        return t212.universe_module().briefing_line(cfg, state_dir)
    except Exception as e:
        return "Universe: no data (%s)" % e


def _pct(x):
    return "no data" if x is None else "%+.2f%%" % x


def drift_line(state_dir, day, which):
    """The drift shadow line for the phone, or None when the scan is off. Never raises: a missing,
    old or broken shortlist (or drift.py itself) gives "no drift scan yet"."""
    head = "Drift shortlist for tomorrow" if which == "closing" else "Drift would buy today"
    try:
        drift = _load("trader-broker", "drift")
        cfg = t212._read_json(os.path.join(state_dir, "config.json"), {}) or {}
        fn = drift.closing_line if which == "closing" else drift.briefing_line
        return fn(state_dir, cfg, day)
    except Exception:
        return "%s (SHADOW: nothing is bought): no drift scan yet." % head


def full_text(b):
    a, p = b["account"], b["positions"]
    L = ["# Morning briefing, %s" % _local(b["generated"]).strftime("%A %d %B %Y, %H:%M UK"),
         "", "Session: %s (New York). Demo account. Read-only: nothing was placed or cancelled." %
         b["session"].strftime("%A %d %B"), b.get("universe") or "Universe: no data", "", "## 1. Account"]
    if a.get("error"):
        L.append("- Trading 212: no data (%s)" % a["error"])
    L.append("- Total value: %s" % _gbp(a.get("total")))
    L.append("- Cash available: %s" % _gbp(a.get("cash")))
    L.append("- Result on %s, after fees: %s" % (
        b["prev"].strftime("%a %d %b"), _signed_gbp(a["yesterday"]) if a.get("yesterday") is not None else
        "no data (the autopilot records the open and close; it had no record for that day)"))
    L += ["", "## 2. Open positions"]
    if p.get("error"):
        L.append("- no data (%s)" % p["error"])
    elif not p["rows"]:
        L.append("- None held.")
    for r in p["rows"]:
        L.append("- %s: %s @ $%s, now $%s, P/L %s; stop: %s" % (
            r["symbol"], t212._qty(r["qty"]), t212._fmt(r["avg"]), t212._fmt(r["price"]), _gbp(r["pl"]),
            ("$%s (covers %s)" % (t212._fmt(r["stop"]), t212._qty(r["covered"]))) if r["stop"] else "NONE"))
    L.append("- Guard alerts since the %s close: %s" % (b["prev"].strftime("%a"), len(p["alerts"]) or "none"))
    L += ["  - %s UTC %s" % (x["at"].strftime("%a %H:%M"), x["text"]) for x in p["alerts"]]
    if p["unclear"]:
        L += ["- UNCLEAR guard orders: " + "; ".join("%s (%s)" % kv for kv in sorted(p["unclear"].items()))]
    L += ["", "## 3. Market mood", "- US futures: %s" % b["mood"]]
    for f in b["futures"]:
        L.append("  - %s (%s): %s" % (f["name"], f["symbol"], "no data" if f["price"] is None else
                                      "%s, %s" % ("{:,.2f}".format(f["price"]), _pct(f["pct"]))))
    fxr = b["fx"]
    L.append("- GBP/USD: %s" % ("no data" if fxr.get("rate") is None else "%.4f (%s, %s)" % (fxr["rate"], fxr["src"], fxr["when"])))
    L.append("- Headlines (Yahoo's S&P 500 feed, last 24 hours, in the feed's order, not ranked):")
    if b["news"] is None:
        L.append("  - no data")
    elif not b["news"]:
        L.append("  - none in the last 24 hours")
    L += ["  - %s" % n["title"] for n in b["news"] or []]
    L.append("- US economic releases today (Nasdaq calendar, UK times):")
    if b["econ"] is None:
        L.append("  - no data")
    elif not b["econ"]:
        L.append("  - none listed")
    for e in b["econ"] or []:
        L.append("  - %s %s%s%s" % (e["uk_time"] or "--:--", e["name"], " (BIG)" if e["big"] else "",
                                     "" if not (e["consensus"] or e["previous"]) else
                                     ", consensus %s, previous %s" % (e["consensus"] or "-", e["previous"] or "-")))
    L += ["", "## 4. Earnings (Nasdaq calendar)",
          "Watching: what you hold%s." % ((" and " + ", ".join(b["flagged"])) if b["flagged"] else "")]
    for d, rows in sorted(b["earnings"].items()):
        label = "%s (%s)" % ("Today" if d == b["session"] else "Next session", d.strftime("%a %d %b"))
        if rows is None:
            L.append("- %s: no data" % label)
        elif not rows:
            L.append("- %s: none of yours" % label)
        for e in rows or []:
            L.append("- %s: %s (%s), %s%s" % (label, e["symbol"], e["name"], e["when"], ", YOU HOLD IT" if e["held"] else ""))
    if b.get("drift"):
        L += ["", "## Post-earnings drift (shadow)", "- " + b["drift"]]
    if b.get("allocation"):
        L += ["", "## Cash allocation (demo)", "- " + b["allocation"]]
    L += ["", "## 5. To do, most urgent first"]
    names = {1: "NOW", 2: "before the open", 3: "today", 4: "note"}
    L += ["%d. [%s] %s" % (i, names[pr], t) for i, (pr, t) in enumerate(b["todo"], 1)]
    L += ["", "Data: Trading 212 (demo), Yahoo Finance (unofficial, can lag), Nasdaq calendars. "
              "Anything that did not load says no data. Education and tooling, not financial advice."]
    return "\n".join(L) + "\n"


def facts(b):
    """Plain facts to fill the short message when there are fewer than three to-dos."""
    p, out = b["positions"], []
    if p.get("error"):
        out.append("Positions: no data.")
    elif p["rows"]:
        out.append("Open: " + "; ".join("%s %s, stop %s" % (r["symbol"], t212._qty(r["qty"]),
                                                              ("$" + t212._fmt(r["stop"])) if r["stop"] else "NONE")
                                         for r in p["rows"]) + ".")
    else:
        out.append("No open positions.")
    if b["econ"] is None:
        out.append("US releases today: no data.")
    else:
        big = [e for e in b["econ"] if e["big"]]
        out.append("US releases today: %s." % ("; ".join("%s %s UK" % (e["name"], e["uk_time"] or "?") for e in big[:2])
                                              if big else "nothing major listed"))
    ear = b["earnings"].values()
    if any(r is None for r in ear):
        out.append("Earnings: no data.")
    elif not any(ear):
        out.append("Earnings: none for your holdings or recent flags today or next session.")
    out.append("Nothing urgent. The guard and autopilot start at 13:00 UK.")
    return out


def short_text(b, path):
    a = b["account"]
    head = "MORNING BRIEFING %s: account %s%s. Futures %s, GBP/USD %s." % (
        _local(b["generated"]).strftime("%a %d %b"), _gbp(a.get("total")),
        "" if a.get("yesterday") is None else " (%s on %s)" % (_signed_gbp(a["yesterday"]), b["prev"].strftime("%a")),
        b["mood"], "no data" if b["fx"].get("rate") is None else "%.4f" % b["fx"]["rate"])
    points = [t for _, t in b["todo"] if not t.startswith("Nothing urgent")][:3]
    for extra in facts(b):
        if len(points) >= 3:
            break
        points.append(extra)
    if b.get("universe"):
        head += " " + b["universe"].rstrip(".") + "."
    top = ["%d. %s" % (i, t) for i, t in enumerate(points, 1)]
    if b.get("drift"):
        top.append(b["drift"])
    if b.get("allocation"):
        top.append(b["allocation"])
    return "\n".join([head] + top + ["Full briefing: %s" % os.path.relpath(path, ROOT)])


def save(b, state_dir):
    d = os.path.join(state_dir, "briefings")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "%s.md" % _local(b["generated"]).strftime("%Y-%m-%d"))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(full_text(b))
    return path


def main(argv=None, environ=None):
    ap = argparse.ArgumentParser(description="Morning briefing (read-only) to your phone and trader/briefings/.")
    ap.add_argument("--state-dir", default=os.path.join(ROOT, "trader"))
    ap.add_argument("--force", action="store_true", help="run on a weekend or US market holiday too")
    ap.add_argument("--no-send", action="store_true", help="save and print it, send nothing")
    a = ap.parse_args(argv)
    environ = os.environ if environ is None else environ
    today_ny = tk.to_eastern(_utc()).date()
    ok, why = alerts.trading_day(today_ny)
    if not ok and not a.force:
        print("briefing: %s in New York, no briefing today" % why)
        return 0
    mark_path = os.path.join(a.state_dir, "state", "briefing.json")
    t212._write_json(mark_path, dict(t212._read_json(mark_path, {}) or {}, started=today_ny.isoformat(),
                                     started_at=_now()))
    late = None
    if not net.online():
        w = net.wait_online(_sleep, _now, log=lambda t: print("briefing: " + t), label="the briefing")
        if not w["ok"]:
            net.record(a.state_dir, "briefing", w["start"], w["end"], ["the morning briefing"], gave_up=True)
            phone_mod.queue(a.state_dir, "MORNING BRIEFING MISSED: no internet from %s for %d minutes, so it was "
                                         "not built. Run python3 trader-broker/briefing.py when you can."
                            % (net.hm(w["start"]), net.minutes(w["start"], w["end"])))
            sys.stderr.write("briefing: no internet for %d minutes; gave up\n" % net.minutes(w["start"], w["end"]))
            return 1
        late = w
    client = None
    try:
        env = t212.pick_env(False, environ)        # demo; T212_ENV=live without --live is refused
        key, secret = t212.credentials(environ)
        client = t212.Client(env, key, secret, state_dir=a.state_dir, log=lambda m: None)
    except t212.T212Error as e:
        client = _NoAccount(str(e))
    b = build(client, a.state_dir)
    path = save(b, a.state_dir)
    msg = short_text(b, path)
    print(msg)
    if late is not None:
        missed = ["the briefing ran %d minutes late, at %s" % (net.minutes(late["start"], late["end"]), net.hm(late["end"]))]
        net.record(a.state_dir, "briefing", late["start"], late["end"], missed)
        msg = net.back_online("The morning briefing", late["start"], late["end"], missed) + "\n\n" + msg
    if not a.no_send:
        if not send_with_retry(a.state_dir, msg, environ):
            sys.stderr.write("briefing: not sent to the phone after %d tries; it waits in the outbox; saved %s\n"
                             % (SEND_TRIES, path))
            return 1
        t212._write_json(mark_path, dict(t212._read_json(mark_path, {}) or {}, sent=today_ny.isoformat(), sent_at=_now()))
    return 0


SEND_TRIES = 16          # the first try, then every 2 minutes for 30 minutes


def send_with_retry(state_dir, text, environ=None, phone=None):
    """Send the briefing itself, trying again every 2 minutes for 30 minutes. If it still fails it goes to the
    outbox once, and the always-on autopilot delivers it as soon as Telegram answers."""
    p = phone or phone_mod.open_phone(state_dir, environ)
    if p is None:
        return False
    for i in range(SEND_TRIES):
        try:
            phone_mod.flush_outbox(p, state_dir)
            p.send(text)
            return True
        except Exception as e:
            sys.stderr.write("briefing: send %d of %d failed: %s\n" % (i + 1, SEND_TRIES, p.scrub(e)))
            if i + 1 < SEND_TRIES:
                _sleep(net.RETRY_EVERY)
    phone_mod.queue(state_dir, text)
    return False


class _NoAccount(object):
    """No Trading 212 keys: every read says so, and the rest of the briefing still runs."""
    _secrets = []

    def __init__(self, why):
        self.why = why

    def get(self, *a, **k):
        raise t212.T212Error(self.why, 2)


if __name__ == "__main__":
    sys.exit(main())
