#!/usr/bin/env python3
"""The holdings record: one page per holding, and the daily match against the broker. Standard library
only, no network, and NO broker code: it works on a plain "view" of the account that an adapter builds
(trader-broker/holdings_t212.py for Trading 212; another broker writes its own adapter).

  trader/state/holdings.json       the pages (one per buy), and what was last reported
  trader/state/holdings-log.jsonl  every change, added to and never rewritten

A page says why a share was bought and what sells it:

  id, symbol, company, strategy ("intraday", "drift", "news"), reason, wrong_if, event, event_date,
  sources, buy_date, buy_price, qty, cost_gbp, max_loss_gbp, stop, sell_rule {"date", "rule"},
  tracker_at_buy, status, result, broker, account, broker_ref {...the broker's own references...}

  status   waiting     the buy is not filled yet
           open        held
           sell_asked  a broken-reason sale is waiting for your Approve or Reject
           selling     a sale you approved: sold at the next close
           closed      sold; "result" says when, at what price, why, and the result in pounds after fees
           mismatch    the record and the broker disagree; reported, never fixed by itself

The view an adapter gives (prices in US dollars, money in pounds):

  {"broker", "account", "usd_per_gbp", "account_gbp",
   "positions": [{"symbol", "ref", "qty", "avg", "price"}],
   "stops":     [{"symbol", "ref", "qty", "stop", "id"}],      live sell stop orders
   "soft_stops": {ref: stop},                                  stops the guard holds in software
   "waiting":   [{"symbol", "ref", "qty"}],                    buys not filled yet
   "buys":      [{"key", "symbol", "ref", "strategy", "date", "qty", "entry", "stop", "sell_date",
                  "cost_gbp", "max_loss_gbp", "reason", ...}], this pack's own buy records
   "sales":     [{"ref", "qty", "exit", "date", "buy_date", "pl_gbp", "cost_gbp", "why"}]}

REPORT ONLY. match() and sync() never place, cancel or change an order, and a mismatch is never
"fixed": the page is marked and you are told. The one thing sync() fills in by itself is what the broker's
own fills prove: a page whose shares were sold is closed with that sale's result.

    python3 trader-broker/holdings.py show      # the pages, as text
"""

import argparse
import datetime
import json
import os
import sys
import time

SCHEMA = 1
STRATEGIES = ("intraday", "drift", "news")
HOLD = ("drift", "news")                  # held for days or weeks: never sold as a day trade
LIVE = ("waiting", "open", "sell_asked", "selling", "mismatch")
FEE = 0.0015                              # Trading 212's FX fee each way, for risk at the stop
QTY_EPS = 0.005

KINDS = {
    "no_record": "held at the broker with no page in the record",
    "not_held": "the record says it is held, the broker holds none and shows no sale",
    "qty": "the record and the broker disagree on the number of shares",
    "no_stop": "no stop order covers it",
    "part_stop": "its stop order covers only part of it",
    "soft_stop": "its stop is held by the guard in software, so it only works in market hours",
    "stray_stop": "a sell stop is waiting at the broker for a share that is not held",
    "unknown_label": "its strategy label is not one this pack knows, so it is kept and never sold by a rule",
    "no_sell_rule": "it has no readable sell date, so it is kept until you decide",
}


def _now():
    return time.time()


def _num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v else None


def path(sd):
    return os.path.join(sd, "state", "holdings.json")


def log_path(sd):
    return os.path.join(sd, "state", "holdings-log.jsonl")


def load(sd):
    try:
        with open(path(sd), encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        doc = None
    if not isinstance(doc, dict) or not isinstance(doc.get("pages"), list):
        doc = {"schema": SCHEMA, "since": None, "pages": [], "reported": {}}
    doc.setdefault("reported", {})
    return doc


def save(sd, doc):
    p = path(sd)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, p)


def log(sd, event, **fields):
    os.makedirs(os.path.dirname(log_path(sd)), exist_ok=True)
    with open(log_path(sd), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(dict(fields, event=event, at=int(_now())), sort_keys=True) + "\n")


def strategy_of(x):
    """The label as written, lower case. No label at all (records from before labels) is "intraday"."""
    return str((x or {}).get("strategy") or "intraday").strip().lower()


def known(strategy):
    return strategy in STRATEGIES


def is_hold(x):
    """Anything that is not a plain day trade: drift, news, and any label this pack does not know."""
    return strategy_of(x) != "intraday"


def sell_date(page):
    """The page's sell date as a date, or None when there is none that can be read."""
    try:
        return datetime.date.fromisoformat(str((page.get("sell_rule") or {}).get("date") or "")[:10])
    except ValueError:
        return None


def live(doc, broker=None, account=None):
    return [p for p in doc.get("pages") or [] if p.get("status") in LIVE
            and (broker is None or p.get("broker") == broker) and (account is None or p.get("account") == account)]


def find(doc, page_id):
    for p in doc.get("pages") or []:
        if p.get("id") == page_id:
            return p
    return None


def page_from_buy(b, broker, account):
    """A new page from one of the pack's buy records (see the view's "buys")."""
    strategy = strategy_of(b)
    sd_ = b.get("sell_date")
    if strategy == "intraday":
        rule = {"date": b.get("date"), "rule": "a day trade: sold at the 15:50 New York close the same day"}
        reason = b.get("reason") or "Opening-range breakout (a day trade)"
    elif strategy == "drift":
        rule = {"date": sd_, "rule": "sold at the close on its sell date (the 20th trading day held)"}
        reason = b.get("reason") or "Beat its earnings forecast (the post-earnings drift rule)"
    else:
        rule = {"date": sd_, "rule": b.get("sell_rule") or "sold at the close on its sell date"}
        reason = b.get("reason") or "no reason was written down"
    return {"id": "%s-%s-%s" % (b.get("date"), b.get("symbol"), str(b.get("key"))[:8]),
            "key": b.get("key"), "symbol": b.get("symbol"), "company": b.get("company"),
            "strategy": strategy, "reason": reason, "wrong_if": b.get("wrong_if"),
            "event": b.get("event"), "event_date": b.get("event_date"), "sources": b.get("sources") or [],
            "idea": b.get("idea"), "buy_date": b.get("date"), "buy_price": _num(b.get("entry")),
            "qty": _num(b.get("qty")), "cost_gbp": _num(b.get("cost_gbp")),
            "max_loss_gbp": _num(b.get("max_loss_gbp")), "stop": _num(b.get("stop")), "sell_rule": rule,
            "tracker_at_buy": b.get("tracker_at_buy"), "status": "waiting", "result": None,
            "broker": broker, "account": account, "broker_ref": {"ref": b.get("ref"), "buy_key": b.get("key")}}


def _ref(page):
    return (page.get("broker_ref") or {}).get("ref")


def sync(doc, view, today):
    """Bring the pages up to date with the view. Returns [(event, page)] for the log.
    New buy records get a page; a filled buy becomes "open"; shares the broker's fills show as sold close
    the page with that result. Nothing else is changed: a disagreement is left for match() to report."""
    broker, account = view.get("broker"), view.get("account")
    if not doc.get("since"):
        doc["since"] = today
    events = []
    have = {p.get("key") for p in doc["pages"] if p.get("broker") == broker and p.get("account") == account}
    held = {p["ref"]: p for p in view.get("positions") or []}
    waiting = {w["ref"] for w in view.get("waiting") or []}
    for b in view.get("buys") or []:
        if b.get("key") in have:
            continue
        if str(b.get("date") or "") < str(doc["since"]) and b.get("ref") not in held and b.get("ref") not in waiting:
            continue                          # from before the record began and long gone
        page = page_from_buy(b, broker, account)
        doc["pages"].append(page)
        have.add(b.get("key"))
        events.append(("page", page))
    sales = view.get("sales") or []
    for page in live(doc, broker, account):
        ref = _ref(page)
        pos = held.get(ref)
        if pos is not None:
            if page["status"] in ("waiting", "mismatch"):
                page["status"] = "open"
                events.append(("open", page))
            if _num(pos.get("avg")) and not page.get("filled"):
                page["buy_price"], page["filled"] = _num(pos["avg"]), True
            continue
        mine = [s for s in sales if s.get("ref") == ref and str(s.get("buy_date") or "") >= str(page.get("buy_date") or "")]
        if mine:
            last = max(mine, key=lambda s: str(s.get("date") or ""))
            pls = [s.get("pl_gbp") for s in mine]
            page["status"] = "closed"
            page["result"] = {"date": last.get("date"), "price": _num(last.get("exit")), "why": last.get("why"),
                              "pl_gbp": None if any(x is None for x in pls) else round(sum(pls), 2),
                              "qty": round(sum(_num(s.get("qty")) or 0 for s in mine), 4)}
            events.append(("closed", page))
        elif ref in waiting or (page["status"] == "waiting" and page.get("buy_date") == today):
            continue                          # the buy has not filled yet
        elif page["status"] != "mismatch":
            page["status"] = "mismatch"
            events.append(("mismatch", page))
    return events


def match(doc, view, today):
    """Every disagreement between the record and the broker, and every holding without full protection.
    Returns [{"kind", "symbol", "ref", "record", "broker", "key"}], worst first. Pure: changes nothing."""
    broker, account = view.get("broker"), view.get("account")
    out = []

    def add(kind, symbol, ref, record, at_broker):
        out.append({"kind": kind, "symbol": symbol, "ref": ref, "record": record, "broker": at_broker,
                    "what": KINDS[kind], "key": "%s:%s:%s:%s" % (kind, ref, record, at_broker)})

    pages = {}
    for p in live(doc, broker, account):
        pages.setdefault(_ref(p), []).append(p)
    stops, soft = {}, view.get("soft_stops") or {}
    for s in view.get("stops") or []:
        stops[s["ref"]] = stops.get(s["ref"], 0.0) + abs(_num(s.get("qty")) or 0)
    held_refs, waiting = set(), {w["ref"] for w in view.get("waiting") or []}
    for pos in view.get("positions") or []:
        ref, qty, sym = pos["ref"], _num(pos.get("qty")) or 0.0, pos.get("symbol")
        if qty <= 0:
            continue
        held_refs.add(ref)
        mine = pages.get(ref, [])
        if not mine:
            add("no_record", sym, ref, "no page", "%g held" % qty)
        else:
            want = sum(_num(p.get("qty")) or 0 for p in mine)
            if abs(want - qty) > QTY_EPS:
                add("qty", sym, ref, "%g shares" % want, "%g held" % qty)
            for p in mine:
                if not known(p.get("strategy")):
                    add("unknown_label", sym, ref, "label %r" % p.get("strategy"), "%g held" % qty)
                elif p.get("strategy") in HOLD and sell_date(p) is None:
                    add("no_sell_rule", sym, ref, "no sell date", "%g held" % qty)
        covered = stops.get(ref, 0.0)
        if covered + QTY_EPS < qty:
            if soft.get(ref) is not None:
                add("soft_stop", sym, ref, "stop %s" % soft[ref], "no stop order")
            elif covered > QTY_EPS:
                add("part_stop", sym, ref, "%g held" % qty, "a stop for %g" % covered)
            else:
                add("no_stop", sym, ref, "%g held" % qty, "no stop order")
    for ref, mine in pages.items():
        if ref in held_refs:
            continue
        for p in mine:
            if p.get("status") == "mismatch":
                add("not_held", p.get("symbol"), ref, "%g shares bought %s" % (_num(p.get("qty")) or 0, p.get("buy_date")),
                    "none held")
    for s in view.get("stops") or []:
        if s["ref"] not in held_refs and s["ref"] not in waiting:
            add("stray_stop", s.get("symbol"), s["ref"], "nothing held", "a stop for %g at %s" % (
                abs(_num(s.get("qty")) or 0), s.get("stop")))
    order = list(KINDS)
    out.sort(key=lambda m: (order.index(m["kind"]), str(m["symbol"])))
    return out


def fresh(doc, mismatches):
    """The mismatches not told yet. Remembers them, and forgets the ones that have gone away."""
    now = {m["key"] for m in mismatches}
    seen = doc.setdefault("reported", {})
    new = [m for m in mismatches if m["key"] not in seen]
    for k in [k for k in seen if k not in now]:
        seen.pop(k)
    for m in new:
        seen[m["key"]] = int(_now())
    return new


def open_risk(view):
    """Pounds that every holding could lose at its stop, together (gaps can lose more). One row each.
    The stop is the live stop order, else the guard's software stop, else the stop its buy planned;
    with no stop at all the whole value counts. A buy waiting to fill counts its planned max loss."""
    rate = _num(view.get("usd_per_gbp"))
    if not rate or rate <= 0:
        raise ValueError("no GBP/USD rate to work the open risk out with")
    planned, buys = {}, {}
    for b in view.get("buys") or []:
        buys[b.get("ref")] = b
        if _num(b.get("stop")):
            planned[b.get("ref")] = _num(b["stop"])
    best = {}
    for s in view.get("stops") or []:
        px = _num(s.get("stop"))
        if px:
            best[s["ref"]] = max(best.get(s["ref"], 0.0), px)
    soft = view.get("soft_stops") or {}
    rows, total = [], 0.0
    for pos in view.get("positions") or []:
        ref, qty = pos["ref"], _num(pos.get("qty")) or 0.0
        if qty <= 0:
            continue
        avg = _num(pos.get("avg")) or _num(pos.get("price"))
        stop, how = best.get(ref), "stop order"
        if stop is None and _num(soft.get(ref)):
            stop, how = _num(soft[ref]), "software stop"
        if stop is None and planned.get(ref):
            stop, how = planned[ref], "planned stop"
        if stop is None or not avg:
            px = _num(pos.get("price")) or avg or 0.0
            risk, how = qty * px / rate, "no stop: its whole value"
        else:
            risk = qty * max(0.0, (avg - stop) + FEE * (avg + stop)) / rate
        total += risk
        rows.append({"symbol": pos.get("symbol"), "ref": ref, "qty": qty, "stop": stop, "how": how,
                     "risk_gbp": round(risk, 2), "strategy": strategy_of(buys.get(ref))})
    held = {r["ref"] for r in rows}
    for w in view.get("waiting") or []:
        if w["ref"] in held:
            continue
        b = buys.get(w["ref"]) or {}
        risk = _num(b.get("max_loss_gbp"))
        if risk is None:
            risk = (abs(_num(w.get("qty")) or 0) * (_num(b.get("entry")) or 0.0)) / rate
        total += risk
        rows.append({"symbol": w.get("symbol"), "ref": w["ref"], "qty": abs(_num(w.get("qty")) or 0), "stop": _num(b.get("stop")),
                     "how": "buy waiting to fill", "risk_gbp": round(risk, 2), "strategy": strategy_of(b)})
    return {"rows": rows, "open_risk_gbp": round(total, 2)}


def cost_by_strategy(view):
    """Pounds at cost held (and waiting to fill) under each strategy label."""
    rate = _num(view.get("usd_per_gbp")) or 0.0
    buys = {b.get("ref"): b for b in view.get("buys") or []}
    out, seen = {}, set()
    for pos in view.get("positions") or []:
        qty, avg = _num(pos.get("qty")) or 0.0, _num(pos.get("avg")) or _num(pos.get("price")) or 0.0
        if qty <= 0 or not rate:
            continue
        seen.add(pos["ref"])
        s = strategy_of(buys.get(pos["ref"]))
        out[s] = out.get(s, 0.0) + qty * avg / rate * (1 + FEE)
    for w in view.get("waiting") or []:
        if w["ref"] in seen:
            continue
        b = buys.get(w["ref"]) or {}
        s = strategy_of(b)
        out[s] = out.get(s, 0.0) + (_num(b.get("cost_gbp")) or 0.0)
    return {k: round(v, 2) for k, v in out.items()}


# ---------------------------------------------------------------- text (the log and the command line)

def page_line(p):
    rule = p.get("sell_rule") or {}
    res = p.get("result") or {}
    tail = ""
    if p.get("status") == "closed":
        tail = "; sold %s (%s), result %s" % (res.get("date"), res.get("why") or "?",
                                              "no data" if res.get("pl_gbp") is None else "GBP %+.2f" % res["pl_gbp"])
    return "%-8s %-9s %-10s bought %s, %s shares at $%s, stop %s, sells %s: %s%s" % (
        p.get("symbol"), p.get("strategy"), p.get("status"), p.get("buy_date"),
        "%g" % (p.get("qty") or 0), "%.2f" % p["buy_price"] if p.get("buy_price") else "?",
        "$%.2f" % p["stop"] if p.get("stop") else "none", rule.get("date") or "no date", p.get("reason"), tail)


def mismatch_line(m):
    return "%s: %s (record: %s; broker: %s)" % (m["symbol"], m["what"], m["record"], m["broker"])


def main(argv=None):
    ap = argparse.ArgumentParser(description="The holdings record (read-only here).")
    ap.add_argument("cmd", choices=["show"])
    ap.add_argument("--state-dir", default="trader")
    ap.add_argument("--all", action="store_true", help="closed pages too")
    a = ap.parse_args(argv)
    doc = load(a.state_dir)
    pages = doc["pages"] if a.all else live(doc)
    if not pages:
        print("no %spages in %s" % ("" if a.all else "open ", path(a.state_dir)))
        return 0
    for p in pages:
        print(page_line(p))
    return 0


if __name__ == "__main__":
    sys.exit(main())
