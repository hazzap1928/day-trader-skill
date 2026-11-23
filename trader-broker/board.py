#!/usr/bin/env python3
"""The evening strategy board: one phone-friendly HTML page, one card per strategy. REPORT ONLY.

Built each US trading day in the autopilot's daily review step (Pilot.daily_review, after the review message),
saved to trader/state/boards/DATE.html and sent to the phone as a file (phone.Phone.send_document).

Each card (drift, news; day trading as one "switched off" line while autopilot.intraday_enabled is false):
  1. today, start to end, in UK time: the runs, buys, sales and what was held at the close (real events only)
  2. what it holds now: company, days held, sell date, gain or loss after fees, the reason it was bought
  3. progress since the strategy started: completed trades, wins and losses, total after fees, and the S&P 500
     over the same days where it was recorded
The top of the page: the date, the account value, cash and the cash always kept aside, and the day's market move.

It reads only what the agent already saved (holdings.json, fills.json, news/ runs, ideas and buys, drift/ buys,
trades and shortlist, reviews/DATE.json, account-days.json, market-day.json). No broker call, no web call.
It never places, cancels or changes an order and never edits a holdings page. Demo. Education and tooling,
not financial advice.

    python3 trader-broker/board.py [DATE]            # writes the file and prints its path; sends nothing
    python3 trader-broker/board.py [DATE] --send     # the same, then sends it to your phone
"""

import argparse
import datetime
import html
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
_MODS = {}

TITLES = {"drift": ("Earnings trades", "Buys shares the morning after they beat their earnings forecast, "
                                        "and sells after 20 trading days."),
          "news": ("News ideas", "Buys on dated news found by the twice-daily research runs, "
                                 "and sells on the date set when it was bought."),
          "intraday": ("Day trading", "Buys in the morning and sells the same afternoon.")}
SOLD_WHY = {"stop": "hit its safety net", "sold at the close": "reached its sell date",
            "sold because you approved it": "you approved the sale",
            "sold in the Trading 212 app": "sold in the Trading 212 app", "sold by the guard": "sold by the guard"}


def _load(folder, name):
    key = (folder, name)
    if key not in _MODS:
        spec = importlib.util.spec_from_file_location("board_%s_%s" % (folder.replace("-", "_"), name),
                                                      os.path.join(ROOT, folder, name + ".py"))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        _MODS[key] = m
    return _MODS[key]


def M():
    return _load("trader-broker", "messages")


def _num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v else None


def _date(v):
    try:
        return datetime.date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def _json(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _jsonl(path):
    out = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return out


def path(sd, day):
    return os.path.join(sd, "state", "boards", "%s.html" % day)


def _last_sunday(y, m):
    d = (datetime.date(y + 1, 1, 1) if m == 12 else datetime.date(y, m + 1, 1)) - datetime.timedelta(days=1)
    return d - datetime.timedelta(days=(d.weekday() + 1) % 7)


def to_uk(utc):
    """Naive UTC -> naive UK time (British Summer Time: last Sunday of March to last Sunday of October, 01:00 UTC).
    Worked out here, not from the Mac's own clock, so a test gives the same answer anywhere (Python 3.8: no zoneinfo)."""
    y = utc.year
    start = datetime.datetime.combine(_last_sunday(y, 3), datetime.time(1, 0))
    end = datetime.datetime.combine(_last_sunday(y, 10), datetime.time(1, 0))
    return utc + datetime.timedelta(hours=1 if start <= utc < end else 0)


def _epoch(utc):
    return utc.replace(tzinfo=datetime.timezone.utc).timestamp()


def uk_hm(ts):
    """An epoch time or ISO time as UK clock time, 14:35. None when it cannot be read."""
    try:
        if isinstance(ts, (int, float)):
            utc = datetime.datetime.utcfromtimestamp(float(ts))
        else:
            dt = datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            utc = dt.astimezone(datetime.timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt
        return to_uk(utc).strftime("%H:%M"), _epoch(utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def close_uk(day):
    """The New York close on `day` in UK time (21:00, or 18:00 on an early close), with its epoch. None when shut."""
    try:
        hm = _load("trader-broker", "t212").market_close(day)
    except Exception:
        hm = (16, 0)
    if not hm:
        return None
    utc = _load("trader-alerts", "alerts").eastern_to_utc(datetime.datetime(day.year, day.month, day.day, hm[0], hm[1]))
    return to_uk(utc).strftime("%H:%M"), _epoch(utc)


def trading_days(a, b):
    try:
        return _load("trader-broker", "drift_buy").trading_days_between(a, b)
    except Exception:
        return None


def intraday_on(cfg):
    ap = (cfg or {}).get("autopilot")
    return isinstance(ap, dict) and ap.get("intraday_enabled") is True


def _strategy(p):
    return str((p or {}).get("strategy") or "intraday").strip().lower()


# ---------------------------------------------------------------- the figures (pure but for reading files)

def build(sd, cfg, day, review=None):
    """Everything the page shows, as plain data. Reads saved files only."""
    day = _date(day)
    st = os.path.join(sd, "state")
    M().use(sd)                                           # company names from the state dir's universe file
    if review is None or str(review.get("day")) != day.isoformat():
        review = _json(os.path.join(st, "reviews", "%s.json" % day), None)
    pages = (_json(os.path.join(st, "holdings.json"), {}) or {}).get("pages") or []
    fills = (_json(os.path.join(st, "fills.json"), {}) or {}).get("rows") or {}
    strategies = ["drift", "news"] + (["intraday"] if intraday_on(cfg) else [])
    return {"day": day.isoformat(), "top": _top(sd, cfg, day, review),
            "cards": [_card(sd, s, day, review, pages, fills) for s in strategies],
            "intraday_off": not intraday_on(cfg), "review_ok": bool(review and review.get("ok"))}


def _top(sd, cfg, day, review):
    st = os.path.join(sd, "state")
    out = {"account_gbp": None, "cash_gbp": None, "buffer_gbp": None, "buffer_pct": _num((cfg or {}).get("cash_buffer_pct")),
           "day_change_gbp": None, "market_pct": None}
    if review and review.get("ok"):
        out["account_gbp"] = _num(review.get("account_gbp"))
        for x in review.get("limits") or []:
            if x.get("name") == "cash":
                out["cash_gbp"], out["buffer_gbp"] = _num(x.get("now_gbp")), _num(x.get("limit_gbp"))
    days = ((_json(os.path.join(st, "account-days.json"), {}) or {}).get("days") or {}).get(day.isoformat()) or {}
    if out["account_gbp"] is None:
        out["account_gbp"] = _num(days.get("close"))
    if _num(days.get("close")) is not None and _num(days.get("start")) is not None:
        out["day_change_gbp"] = round(days["close"] - days["start"], 2)
    if out["buffer_gbp"] is None and out["account_gbp"] and out["buffer_pct"]:
        out["buffer_gbp"] = round(out["account_gbp"] * out["buffer_pct"] / 100.0, 2)
    mk = _json(os.path.join(st, "market-day.json"), {}) or {}
    if mk.get("day") == day.isoformat():
        out["market_pct"] = _num(mk.get("spy_pct"))
    return out


def _event(when, text):
    """when: (hh:mm, epoch) or None (shown without a time, kept in place)."""
    return {"time": when[0] if when else None, "at": when[1] if when else None, "text": text}


def _short_why(why):
    """"its confidence is low: only medium and high ..." -> "its confidence is low"."""
    return str(why or "no reason given").split(":")[0].strip()


def _sold_line(p):
    res = p.get("result") or {}
    why = SOLD_WHY.get(str(res.get("why") or ""), html.escape(str(res.get("why") or "sold"), quote=False))
    return {"sym": p.get("symbol"), "why": why, "pl": _num(res.get("pl_gbp"))}


def _card(sd, s, day, review, pages, fills):
    st = os.path.join(sd, "state")
    D = day.isoformat()
    mine = [p for p in pages if _strategy(p) == s]
    events = []
    buys_log = _jsonl(os.path.join(st, s, "buys.jsonl")) if s in ("drift", "news") else []
    buys_log = [b for b in buys_log if b.get("day") == D]

    if s == "news":
        ideas = [i for i in _jsonl(os.path.join(st, "news", "ideas.jsonl")) if i.get("day") == D]
        for r in _jsonl(os.path.join(st, "news", "runs.jsonl")):
            if r.get("day") != D:
                continue
            kind = "morning news run" if r.get("kind") == "main" else "pre-open check"
            if r.get("status") != "ok":
                events.append(_event(uk_hm(r.get("at") or r.get("started")), "%s: %s" % (
                    kind, "skipped (the Mac was asleep or it was too late)" if r.get("status") == "skipped"
                    else "did not finish, so nothing came of it")))
                continue
            syms = sorted({str(i.get("symbol")) for i in ideas
                           if i.get("symbol") and (i.get("from") or "main") == r.get("kind")})
            n = int(r.get("ideas") or 0)
            if r.get("kind") == "main":
                txt = "%s: %d idea%s%s" % (kind, n, "" if n == 1 else "s",
                                           (" (%s)" % ", ".join(M().company(x) for x in syms)) if n and syms else "")
            else:
                bits = []
                if n:
                    bits.append("%d new idea%s" % (n, "" if n == 1 else "s"))
                if r.get("dropped"):
                    bits.append("%d dropped" % r["dropped"])
                txt = "%s: %s" % (kind, ", ".join(bits) if bits else "nothing changed")
            events.append(_event(uk_hm(r.get("at")), txt))
    if s == "drift":
        sl = _json(os.path.join(st, "drift", "shortlist.json"), {}) or {}
        if sl.get("scan_day") == D:
            syms = [r.get("symbol") for r in sl.get("shortlist") or [] if r.get("symbol")]
            events.append(_event(uk_hm(sl.get("generated_utc")), "earnings check: %s" % (
                "%s to buy next trading day" % ", ".join(M().company(x) for x in syms) if syms
                else "nothing qualified for the next trading day")))

    # buys: the holdings pages bought today (time from the fill, else the buy log)
    for p in mine:
        if p.get("buy_date") != D:
            continue
        f = fills.get("%s:buy" % p.get("id")) or {}
        when = uk_hm(f.get("fill_at")) if f.get("fill_at") else None
        if when is None:
            got = [b for b in buys_log if b.get("status") == "bought" and str(b.get("symbol")).upper() == str(p.get("symbol")).upper()]
            when = uk_hm(got[-1].get("at")) if got else None
        what = "bought" if p.get("status") != "waiting" else "order placed, not filled yet:"
        events.append(_event(when, "%s %s: %s" % (what, M().company(p.get("symbol")), html.escape(str(p.get("reason") or "no reason written"), quote=False))))
    # ideas or names not bought today, one line per reason
    skipped = {}
    for b in buys_log:
        if b.get("status") != "bought":
            skipped.setdefault(_short_why(b.get("why")), []).append(b)
    for why, rows in skipped.items():
        events.append(_event(uk_hm(max(r.get("at") or 0 for r in rows)), "not bought: %s (%s)" % (
            ", ".join(M().company(r.get("symbol")) for r in rows), html.escape(why, quote=False))))
    # sales
    for p in mine:
        res = p.get("result") or {}
        if p.get("status") != "closed" or res.get("date") != D:
            continue
        f = fills.get("%s:sell" % p.get("id")) or {}
        sl = _sold_line(p)
        events.append(dict(_event(uk_hm(f.get("fill_at")) if f.get("fill_at") else None,
                                  "sold %s (%s)" % (M().company(sl["sym"]), sl["why"])), pl=sl["pl"]))

    holding = _holding(s, day, review, pages)
    if events or holding:
        names = ", ".join(M().company(h["symbol"]) for h in holding)
        events.append(_event(close_uk(day), "market closed: %s" % (
            "holding %s" % names if holding else "nothing held")))
    timed = sorted([e for e in events if e["at"] is not None], key=lambda e: e["at"])
    events = timed + [e for e in events if e["at"] is None and not e["text"].startswith("market closed")] \
        + [e for e in events if e["at"] is None and e["text"].startswith("market closed")]
    return {"strategy": s, "title": TITLES.get(s, (s, ""))[0], "about": TITLES.get(s, (s, ""))[1],
            "events": events, "holding": holding, "progress": _progress(sd, s, mine)}


def _holding(s, day, review, pages):
    """What the strategy holds after the close: from the review (with the gain after fees), else the record."""
    live = {}
    for p in pages:
        if p.get("status") in ("open", "sell_asked", "selling", "waiting", "mismatch"):
            live.setdefault((p.get("broker_ref") or {}).get("ref"), p)
    out = []
    if review and review.get("ok"):
        for r in review.get("rows") or []:
            if r.get("strategy") != s:
                continue
            p = live.get(r.get("ref")) or {}
            out.append({"symbol": p.get("symbol") or r.get("symbol"), "buy_date": r.get("buy_date") or p.get("buy_date"),
                        "sell_date": r.get("sell_date"), "gain_gbp": _num(r.get("gain_gbp")),
                        "reason": r.get("reason") or p.get("reason"), "status": r.get("status")})
    else:
        for p in live.values():
            if _strategy(p) == s and p.get("status") != "waiting":
                out.append({"symbol": p.get("symbol"), "buy_date": p.get("buy_date"),
                            "sell_date": (p.get("sell_rule") or {}).get("date"), "gain_gbp": None,
                            "reason": p.get("reason"), "status": p.get("status")})
    for h in out:
        h["days"] = trading_days(h["buy_date"], day) if _date(h["buy_date"]) else None
    return out


def _progress(sd, s, mine):
    """Completed trades since the strategy's first buy, after fees, with the S&P 500 where it was recorded."""
    closed = [p for p in mine if p.get("status") == "closed"]
    pls = [_num((p.get("result") or {}).get("pl_gbp")) for p in closed]
    known = [x for x in pls if x is not None]
    first = min([p.get("buy_date") for p in mine if p.get("buy_date")] or [None]) if mine else None
    spy_gbp, spy_n = 0.0, 0
    if s == "drift":
        trades = _jsonl(os.path.join(sd, "state", "drift", "trades.jsonl"))
        for p in closed:
            t = [x for x in trades if x.get("buy_day") == p.get("buy_date")
                 and str(x.get("ticker")) == str((p.get("broker_ref") or {}).get("ref"))]
            if t and _num(t[-1].get("spy_pct")) is not None and _num(t[-1].get("cost_gbp")):
                spy_gbp += t[-1]["cost_gbp"] * t[-1]["spy_pct"] / 100.0
                spy_n += 1
    return {"since": first, "trades": len(closed), "wins": sum(1 for x in known if x > 0),
            "losses": sum(1 for x in known if x < 0), "unknown": len(pls) - len(known),
            "total_gbp": round(sum(known), 2) if known else (0.0 if not closed else None),
            "spy_gbp": round(spy_gbp, 2) if spy_n else None, "spy_n": spy_n}


# ---------------------------------------------------------------- the page

CSS = """
:root{color-scheme:light dark;--bg:#f4f4f6;--card:#fff;--ink:#1c1c1e;--dim:#6e6e73;--line:#e3e3e8;--up:#1a7f37;--down:#c62828}
@media (prefers-color-scheme:dark){:root{--bg:#000;--card:#1c1c1e;--ink:#f2f2f7;--dim:#a1a1a6;--line:#38383a;--up:#4cd964;--down:#ff6b6b}}
*{box-sizing:border-box}body{margin:0;padding:14px;background:var(--bg);color:var(--ink);
font:16px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;-webkit-text-size-adjust:100%}
h1{font-size:22px;margin:4px 0 2px}h2{font-size:19px;margin:0}h3{font-size:13px;margin:14px 0 6px;color:var(--dim);
text-transform:uppercase;letter-spacing:.04em}.card{background:var(--card);border-radius:14px;padding:14px 16px;margin:12px 0;
border:1px solid var(--line)}.dim{color:var(--dim);font-size:14px}.row{display:flex;justify-content:space-between;gap:10px;
padding:3px 0}.row b{font-weight:600;text-align:right}ul{list-style:none;margin:0;padding:0}li{display:flex;gap:8px;padding:5px 0;
border-top:1px solid var(--line)}li:first-child{border-top:0}.t{flex:none;min-width:3em;color:var(--dim);
font-variant-numeric:tabular-nums}.up{color:var(--up);font-weight:600}.down{color:var(--down);font-weight:600}
.off{color:var(--dim)}.hold{padding:7px 0;border-top:1px solid var(--line)}.hold:first-of-type{border-top:0}
footer{color:var(--dim);font-size:12px;margin:18px 2px 8px}
"""


def money(x):
    """A £ result, green when up and red when down (the only colours on the page)."""
    x = _num(x)
    if x is None:
        return "not known yet"
    cls = "up" if x > 0.004 else "down" if x < -0.004 else ""
    txt = M().signed_gbp(x) if cls else M().gbp(0)
    return '<span class="%s">%s</span>' % (cls, txt) if cls else txt


def _top_html(t, day):
    L = ['<h1>Strategy board</h1><div class="dim">%s &middot; Practice account</div>' % html.escape(M().day_name(day)),
         '<div class="card">']
    acct = M().gbp(t["account_gbp"]) if t["account_gbp"] is not None else "not known"
    L.append('<div class="row"><span>Account value</span><b>%s</b></div>' % acct)
    if t["day_change_gbp"] is not None:
        L.append('<div class="row"><span>Change today</span><b>%s</b></div>' % money(t["day_change_gbp"]))
    if t["cash_gbp"] is not None:
        share = (" (%d%%)" % round(100 * t["cash_gbp"] / t["account_gbp"])) if t["account_gbp"] else ""
        L.append('<div class="row"><span>Cash</span><b>%s%s</b></div>' % (M().gbp(t["cash_gbp"]), share))
    if t["buffer_gbp"] is not None:
        ok = t["cash_gbp"] is None or t["cash_gbp"] + 0.005 >= t["buffer_gbp"]
        L.append('<div class="row"><span>Kept aside (%g%% rule)</span><b>%s, %s</b></div>' % (
            t["buffer_pct"] or 10, M().gbp(t["buffer_gbp"]), "kept" if ok else "NOT kept"))
    if t["market_pct"] is not None:
        mp = t["market_pct"]
        L.append('<div class="dim" style="margin-top:6px">The US stock market (S&amp;P 500) %s today.</div>' % (
            "was flat" if abs(mp) < 0.05 else "rose %.1f%%" % mp if mp > 0 else "fell %.1f%%" % -mp))
    else:
        L.append('<div class="dim" style="margin-top:6px">Today\'s market move was not recorded.</div>')
    L.append("</div>")
    return "\n".join(L)


def _card_html(c, day):
    L = ['<div class="card"><h2>%s</h2><div class="dim">%s</div>' % (html.escape(c["title"]), html.escape(c["about"])),
         "<h3>Today (UK time)</h3>"]
    if not c["events"]:
        L.append('<div class="dim">Nothing happened today.</div>')
    else:
        L.append("<ul>")
        for e in c["events"]:
            extra = (", %s" % money(e["pl"])) if "pl" in e else ""
            L.append('<li><span class="t">%s</span><span>%s%s</span></li>' % (e["time"] or "", e["text"], extra))
        L.append("</ul>")
    L.append("<h3>Holding now</h3>")
    if not c["holding"]:
        L.append('<div class="dim">Nothing.</div>')
    for h in c["holding"]:
        bits = []
        if h["days"]:
            bits.append("day %d" % h["days"] + (" of 20" if c["strategy"] == "drift" else ""))
        if h["sell_date"]:
            bits.append("sells %s" % html.escape(M().short_day(h["sell_date"])))
        if h.get("status") == "selling":
            bits.append("you approved selling it")
        L.append('<div class="hold"><div class="row"><span><b style="text-align:left">%s</b></span><b>%s</b></div>'
                 '<div class="dim">%s<br>Why: %s</div></div>' % (
                     M().company(h["symbol"]), money(h["gain_gbp"]), " &middot; ".join(bits) or "&nbsp;",
                     html.escape(str(h["reason"] or "no reason written"), quote=False)))
    p = c["progress"]
    L.append("<h3>Progress so far</h3>")
    if not p["trades"]:
        L.append('<div class="dim">No completed trades yet%s.</div>' % (
            " (first buy %s)" % html.escape(M().short_day(p["since"])) if p["since"] else ""))
    else:
        L.append('<div class="row"><span>Completed trades (since %s)</span><b>%d</b></div>' % (
            html.escape(M().short_day(p["since"])), p["trades"]))
        L.append('<div class="row"><span>Made money / lost money</span><b>%d / %d</b></div>' % (p["wins"], p["losses"]))
        L.append('<div class="row"><span>Total after fees</span><b>%s</b></div>' % money(p["total_gbp"]))
        if p["unknown"]:
            L.append('<div class="dim">%d trade%s with no result from Trading 212 yet.</div>' % (
                p["unknown"], "" if p["unknown"] == 1 else "s"))
        if p["spy_gbp"] is not None:
            L.append('<div class="row"><span>Same money in the S&amp;P 500, same days%s</span><b>%s</b></div>' % (
                "" if p["spy_n"] == p["trades"] else " (%d of %d trades)" % (p["spy_n"], p["trades"]), money(p["spy_gbp"])))
        else:
            L.append('<div class="dim">The S&amp;P 500 over the same days was not recorded for these trades.</div>')
    L.append("</div>")
    return "\n".join(L)


def render(b):
    day = _date(b["day"])
    body = [_top_html(b["top"], day)]
    body += [_card_html(c, day) for c in b["cards"]]
    if b["intraday_off"]:
        body.append('<div class="card off"><h2 class="off">Day trading</h2>Switched off: it buys and sells nothing.</div>')
    if not b["review_ok"]:
        body.append('<div class="dim">The account could not be read this evening, so gains on holdings are not shown.</div>')
    body.append("<footer>Built from the agent's own saved records after the close. Report only: this page changes "
                "nothing. Practice account, not financial advice.</footer>")
    return ("<!DOCTYPE html>\n<html lang=\"en-GB\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<meta name=\"color-scheme\" content=\"light dark\">"
            "<title>Strategy board %s</title><style>%s</style></head><body>\n%s\n</body></html>\n"
            % (html.escape(b["day"]), CSS, "\n".join(body)))


def write(sd, cfg, day, review=None):
    """Build the board and save it. Returns its path."""
    p = path(sd, _date(day).isoformat())
    os.makedirs(os.path.dirname(p), exist_ok=True)
    M().use(sd)
    page = render(build(sd, cfg, day, review))
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(page)
    os.replace(tmp, p)
    return p


def run(sd, cfg, day, phone=None, say=None, push=None, review=None):
    """Build, save and send the board. Never raises. On a failure: one short message (through `push`)."""
    try:
        p = write(sd, cfg, day, review)
    except Exception as e:
        if say:
            say("board: could not be built (%s)" % str(e)[:200])
        if push:
            push(M().board_failed())
        return None
    if say:
        say("board: saved %s" % p)
    if phone is None:
        return p
    try:
        phone.send_document(p, M().board_caption())
        if say:
            say("board: sent to the phone")
    except Exception as e:
        if say:
            say("board: not sent (%s)" % (phone.scrub(e) if hasattr(phone, "scrub") else str(e)[:200]))
        if push:
            push(M().board_failed())
    return p


def main(argv=None, environ=None):
    ap = argparse.ArgumentParser(description="The evening strategy board (report only).")
    ap.add_argument("day", nargs="?", help="New York date, YYYY-MM-DD (default: today in New York)")
    ap.add_argument("--state-dir", default=os.path.join(ROOT, "trader"))
    ap.add_argument("--send", action="store_true", help="also send it to your phone")
    a = ap.parse_args(argv)
    cfg = _json(os.path.join(a.state_dir, "config.json"), {}) or {}
    day = _date(a.day) if a.day else _load("trader-alerts", "alerts").to_eastern(datetime.datetime.utcnow()).date()
    if day is None:
        sys.stderr.write("board: not a date: %s\n" % a.day)
        return 2
    p = write(a.state_dir, cfg, day)
    print(p)
    if a.send:
        ph = _load("trader-broker", "phone")
        phone = ph.open_phone(a.state_dir, environ)
        if phone is None:
            sys.stderr.write("board: no phone paired or no token, so not sent\n")
            return 1
        try:
            phone.send_document(p, M().board_caption())
        except ph.PhoneError as e:
            sys.stderr.write("board: not sent: %s\n" % e)
            return 1
        print("Sent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
