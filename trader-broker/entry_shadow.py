#!/usr/bin/env python3
"""What each drift and news buy would have cost at other times, or with a limit order. SHADOW ONLY.

Drift buys at 09:33 and news at 09:35 New York, at market, just after the open, when prices jump around most.
For every real buy, after that day's close, this works out from the day's 1-minute prices (Yahoo, kept about
30 days) what the same shares would have cost:

  at 09:45, 10:00 and 10:30   the market price at that minute (its first trade, the minute's open)
  with a limit order          a buy limit at the 09:32 price (the close of the 09:32 minute), sent when the real
                              order went in and good for the day: it fills at the first minute whose low reaches
                              the limit (at the limit, or at that minute's open when a LATER minute opens below it). If no
                              minute reaches it by 15:49 it would not have filled: the share's 15:50 price is kept
                              to show what was missed.

Each is compared two ways (version 1, fixed in advance):
  vs_market_pct   against the real market at the minute we actually filled (its middle): the timing alone.
                  Negative = cheaper. This is the fair comparison, because a later buy at Trading 212 would
                  carry Trading 212's own pricing too (see hold_slippage.py, "market_gap_pct").
  vs_fill_pct     against the price we actually paid (timing plus Trading 212's pricing on the real fill).

It never delays, places, changes or cancels anything; it only looks back at prices once the day is over. The
real buy times and rules are unchanged. Broker-neutral. Files: trader/state/entry-shadow.json, .jsonl.

    python3 trader-broker/entry_shadow.py show
"""

import argparse
import datetime
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
VERSION = 1
TIMES = ("09:45", "10:00", "10:30")
LIMIT_FROM = "09:32"
LAST = "15:49"
CLOSE_AT = "15:50"
ORDER_AT = {"drift": "09:33", "news": "09:35"}
MINUTE_DAYS = 25

_MODS = {}


def _load(folder, name):
    key = (folder, name)
    if key not in _MODS:
        spec = importlib.util.spec_from_file_location("entsh_%s_%s" % (folder.replace("-", "_"), name),
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


def _hm(day, text):
    h, m = [int(x) for x in text.split(":")]
    return datetime.datetime.combine(day, datetime.time(h, m))


def path(sd):
    return os.path.join(sd, "state", "entry-shadow.json")


def load(sd):
    try:
        with open(path(sd), encoding="utf-8") as fh:
            doc = json.load(fh)
        if isinstance(doc.get("buys"), dict):
            return doc
    except (OSError, ValueError, AttributeError):
        pass
    return {"buys": {}}


def save(sd, doc):
    os.makedirs(os.path.dirname(path(sd)), exist_ok=True)
    tmp = "%s.%d.tmp" % (path(sd), os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, sort_keys=True)
    os.replace(tmp, path(sd))


def _at_or_after(minutes, t, within=5):
    for k in range(within + 1):
        m = t + datetime.timedelta(minutes=k)
        if m in minutes:
            return m, minutes[m]
    return None, None


def _at_or_before(minutes, t, within=5):
    for k in range(within + 1):
        m = t - datetime.timedelta(minutes=k)
        if m in minutes:
            return m, minutes[m]
    return None, None


def pct(a, b):
    return round(100.0 * (a / b - 1), 4)


def evaluate(row, minutes, day):
    """The shadow for one buy. row: hold_slippage's buy row (fill_price, fill_at, market, strategy).
    minutes: {NY minute: (o, h, l, c, v)} for the buy day. Pure. Raises ValueError when it cannot be done."""
    tk = _load("trader-broker", "ticket")
    fill = float(row["fill_price"])
    if row.get("fill_at"):
        at = datetime.datetime.fromisoformat(str(row["fill_at"]).replace("Z", "+00:00"))
        order_m = tk.to_eastern(at.astimezone(datetime.timezone.utc).replace(tzinfo=None)).replace(second=0, microsecond=0)
    else:
        order_m = _hm(day, ORDER_AT.get(row.get("strategy"), "09:33"))
    mk = row.get("market") or {}
    market = mk.get("mid")
    if market is None:
        m, bar = _at_or_before(minutes, order_m, 2)
        if bar is None:
            raise ValueError("no market prices around the buy (%s)" % order_m.strftime("%H:%M"))
        market = (bar[1] + bar[2]) / 2.0
    out = {"version": VERSION, "day": day.isoformat(), "symbol": row.get("symbol"), "strategy": row.get("strategy"),
           "page": row.get("page"), "fill_price": fill, "order_minute": order_m.strftime("%H:%M"),
           "market_at_fill": round(market, 4), "times": {}}
    for t in TIMES:
        m, bar = _at_or_after(minutes, _hm(day, t))
        if bar is None:
            out["times"][t] = {"error": "no trades at %s" % t}
            continue
        out["times"][t] = {"minute": m.strftime("%H:%M"), "price": bar[0], "vs_market_pct": pct(bar[0], market),
                           "vs_fill_pct": pct(bar[0], fill)}
    lm, lbar = _at_or_before(minutes, _hm(day, LIMIT_FROM))
    if lbar is None:
        out["limit"] = {"error": "no 09:32 price"}
        return out
    limit = lbar[3]
    lim = {"price": limit, "from_minute": lm.strftime("%H:%M"), "filled": False}
    for m in sorted(k for k in minutes if order_m <= k <= _hm(day, LAST)):
        o, h, l = minutes[m][:3]
        if l <= limit + 1e-9:
            got = o if (o <= limit and m > order_m) else limit    # the order minute's open came before the order
            lim.update(filled=True, minute=m.strftime("%H:%M"), fill_price=got, vs_market_pct=pct(got, market),
                       vs_fill_pct=pct(got, fill))
            break
    if not lim["filled"]:
        cm, cbar = _at_or_before(minutes, _hm(day, CLOSE_AT))
        if cbar is not None:
            lim.update(price_at_close=cbar[0], close_vs_limit_pct=pct(cbar[0], limit),
                       close_vs_fill_pct=pct(cbar[0], fill))
    out["limit"] = lim
    return out


def run(sd, today, minutes_fn, say=None):
    """The shadow for each real drift or news buy not done yet (its day over, its 1-minute prices still there).
    Returns the number written. Never raises for one buy; never changes an order."""
    S = _load("trader-broker", "hold_slippage")
    doc = load(sd)
    n = 0
    for r in S.rows(sd):
        day = _date(r.get("day"))
        if r.get("kind") != "buy" or not day or r["page"] in doc["buys"] or day > today:
            continue
        if (today - day).days > MINUTE_DAYS:
            doc["buys"][r["page"]] = {"error": "the 1-minute prices are no longer available", "day": str(day)}
            continue
        try:
            got = evaluate(r, minutes_fn(r["symbol"], day), day)
        except Exception as e:
            if say:
                say("entry shadow: %s %s not worked out (%s)" % (r.get("symbol"), day, str(e)[:120]))
            continue
        doc["buys"][r["page"]] = got
        with open(os.path.join(sd, "state", "entry-shadow.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(got, sort_keys=True) + "\n")
        n += 1
    save(sd, doc)
    if say and n:
        say("entry shadow: %d buy(s) worked out" % n)
    return n


def summary(sd):
    """Averages for the Friday report: each later time, and the limit order."""
    buys = [b for b in load(sd)["buys"].values() if b.get("times")]
    out = {"n": len(buys), "times": {}, "limit": {}}
    for t in TIMES:
        got = [b["times"][t] for b in buys if (b["times"].get(t) or {}).get("vs_market_pct") is not None]
        out["times"][t] = {"n": len(got),
                           "avg_vs_market_pct": round(sum(g["vs_market_pct"] for g in got) / len(got), 3) if got else None,
                           "avg_vs_fill_pct": round(sum(g["vs_fill_pct"] for g in got) / len(got), 3) if got else None,
                           "cheaper": sum(1 for g in got if g["vs_market_pct"] < 0)}
    lims = [b["limit"] for b in buys if "filled" in (b.get("limit") or {})]
    filled = [x for x in lims if x["filled"]]
    missed = [x for x in lims if not x["filled"] and x.get("close_vs_fill_pct") is not None]
    out["limit"] = {"n": len(lims), "filled": len(filled),
                    "avg_vs_fill_pct": round(sum(x["vs_fill_pct"] for x in filled) / len(filled), 3) if filled else None,
                    "avg_missed_close_vs_fill_pct": round(sum(x["close_vs_fill_pct"] for x in missed) / len(missed), 3)
                    if missed else None}
    return out


def lines(s):
    L = ["", "## Entry timing shadow (version %d): what each drift and news buy would have cost later or with a limit" % VERSION]
    if not s["n"]:
        return L + ["- no buys worked out yet"]
    for t in TIMES:
        g = s["times"][t]
        if g["n"]:
            L.append("- at %s: %+.2f%% against the market when we filled (%d of %d cheaper); %+.2f%% against what we paid"
                     % (t, g["avg_vs_market_pct"], g["cheaper"], g["n"], g["avg_vs_fill_pct"]))
    lm = s["limit"]
    if lm["n"]:
        L.append("- limit at the 09:32 price: filled %d of %d%s%s" % (
            lm["filled"], lm["n"], "" if lm["avg_vs_fill_pct"] is None else ", %+.2f%% against what we paid when it filled"
            % lm["avg_vs_fill_pct"], "" if lm["avg_missed_close_vs_fill_pct"] is None else
            "; when it did not fill, the 15:50 price was %+.2f%% against what we paid" % lm["avg_missed_close_vs_fill_pct"]))
    return L


def main(argv=None):
    ap = argparse.ArgumentParser(description="Entry timing shadow (report only).")
    ap.add_argument("cmd", choices=("show",))
    ap.add_argument("--state-dir", default=os.path.join(ROOT, "trader"))
    a = ap.parse_args(argv)
    for k, b in sorted(load(a.state_dir)["buys"].items()):
        print(k, json.dumps(b, sort_keys=True))
    print("\n".join(lines(summary(a.state_dir))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
