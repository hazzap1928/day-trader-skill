#!/usr/bin/env python3
"""Slippage on held trades (drift and news): the price actually got against the price expected. REPORT ONLY.

Every result in this pack assumes 0.05% slippage each way (on top of the 0.15% FX fee). This measures it on the
agent's own drift and news trades:

  buy   the fill price (the holdings page's buy price, from Trading 212) against the fresh quote taken just
        before the order (drift and news buys.jsonl, "price")
  sell  a stop sale: the fill against the stop price; a sale at the 15:50 close or an approved sale: the fill
        against the price the guard saw when it sent the order (t212-guard.json, kept for that day only, so
        this runs every evening); otherwise "no price to compare" (counted, not averaged)

Slippage is in % of the expected price, positive = worse for us (paid more, or got less).

The "quote just before the order" comes from Yahoo (unofficial, can lag), and drift buys at 09:33 New York, just
after the open, when prices move fast: part of a buy's figure can be the quote's delay, not the fill.

IMPORTANT: on the Practice (demo) account Trading 212 SIMULATES fills, so these figures may understate what a
real account would pay, above all on a fast move through a stop.

It reads only; it never places, cancels or changes an order and never edits a holdings page. Broker-neutral
apart from reading the guard's own file for the expected close-sale price.
Log: trader/state/slippage.jsonl (one row per buy and per sale, once).

    python3 trader-broker/hold_slippage.py show     # every row and the averages
"""

import argparse
import datetime
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ASSUMED_PCT = 0.05
NOTE = ("Practice account fills are simulated by Trading 212, so they may understate real slippage, "
        "above all when a share moves fast through its safety net.")

_MODS = {}


def _load(folder, name):
    key = (folder, name)
    if key not in _MODS:
        spec = importlib.util.spec_from_file_location("slip_%s_%s" % (folder.replace("-", "_"), name),
                                                      os.path.join(ROOT, folder, name + ".py"))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        _MODS[key] = m
    return _MODS[key]


def _num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v and v > 0 else None


def _lines(p):
    out = []
    try:
        with open(p, encoding="utf-8") as fh:
            for line in fh:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    return out


def _read(p):
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def log_path(sd):
    return os.path.join(sd, "state", "slippage.jsonl")


def quotes(sd):
    """{(strategy, symbol, day): the quote taken just before the buy} from the drift and news buy logs."""
    out = {}
    for strategy, p in (("drift", os.path.join(sd, "state", "drift", "buys.jsonl")),
                        ("news", os.path.join(sd, "state", "news", "buys.jsonl"))):
        for r in _lines(p):
            if r.get("status") == "bought" and _num(r.get("price")):
                out[(strategy, str(r.get("symbol")).upper(), str(r.get("day")))] = float(r["price"])
    return out


def seen_at_close(sd):
    """{(broker ticker, day): the price the guard saw when it sent a close or approved sale} (today's only)."""
    g = _read(os.path.join(sd, "state", "t212-guard.json"))
    out = {}
    eod = g.get("eod") or {}
    for x in eod.get("sold") or []:
        if _num((x or {}).get("price_seen")):
            out[(x.get("ticker"), str(eod.get("date")))] = float(x["price_seen"])
    for x in g.get("sold") or []:
        if _num((x or {}).get("price_seen")) and x.get("ticker"):
            day = str(x.get("date") or x.get("day") or "")
            if day:
                out.setdefault((x["ticker"], day), float(x["price_seen"]))
    return out


def slip(kind, expected, got):
    """% worse than expected: a buy above, or a sale below, the expected price is positive."""
    return round(100.0 * ((got / expected - 1) if kind == "buy" else (1 - got / expected)), 4)


def record(sd, today=None):
    """Add a row for each drift or news buy that has filled and each sale that has closed, once. Returns the new rows."""
    H = _load("trader-broker", "holdings")
    have = {(r.get("page"), r.get("kind")) for r in _lines(log_path(sd))}
    q, close = quotes(sd), seen_at_close(sd)
    new = []
    for p in H.load(sd).get("pages") or []:
        strategy = H.strategy_of(p)
        if strategy not in H.HOLD or p.get("status") == "waiting":
            continue
        base = {"page": p.get("id"), "symbol": p.get("symbol"), "strategy": strategy}
        got = _num(p.get("buy_price"))
        if got and (p.get("id"), "buy") not in have:
            exp = q.get((strategy, str(p.get("symbol")).upper(), str(p.get("buy_date"))))
            new.append(dict(base, kind="buy", day=p.get("buy_date"), expected=exp, got=got,
                            basis="the quote just before the order" if exp else "no price to compare",
                            slip_pct=slip("buy", exp, got) if exp else None))
        res = p.get("result") or {}
        sold = _num(res.get("price"))
        if p.get("status") == "closed" and sold and (p.get("id"), "sell") not in have:
            why = str(res.get("why") or "")
            exp, basis = None, "no price to compare"
            if why == "stop" and _num(p.get("stop")):
                exp, basis = float(p["stop"]), "the stop price"
            elif why in ("sold at the close", "sold because you approved it"):
                exp = close.get(((p.get("broker_ref") or {}).get("ref"), str(res.get("date"))))
                basis = "the price the guard saw when it sold" if exp else basis
            new.append(dict(base, kind="sell", day=res.get("date"), why=why, expected=exp, got=sold, basis=basis,
                            slip_pct=slip("sell", exp, sold) if exp else None))
    if new:
        os.makedirs(os.path.dirname(log_path(sd)), exist_ok=True)
        stamp = (today or datetime.date.today()).isoformat()
        with open(log_path(sd), "a", encoding="utf-8") as fh:
            for r in new:
                fh.write(json.dumps(dict(r, logged=stamp), sort_keys=True) + "\n")
    return new


def summary(sd):
    """{"buy": {...}, "sell": {...}, "stop": {...}} with n (measured), avg_pct, worst_pct, unmeasured, plus the note."""
    rows = _lines(log_path(sd))
    out = {"assumed_pct": ASSUMED_PCT, "note": NOTE}
    for label, pick in (("buy", lambda r: r.get("kind") == "buy"), ("sell", lambda r: r.get("kind") == "sell"),
                        ("stop", lambda r: r.get("kind") == "sell" and r.get("why") == "stop")):
        sel = [r for r in rows if pick(r)]
        got = [r["slip_pct"] for r in sel if r.get("slip_pct") is not None]
        out[label] = {"n": len(got), "unmeasured": len(sel) - len(got),
                      "avg_pct": round(sum(got) / len(got), 3) if got else None,
                      "worst_pct": round(max(got), 3) if got else None}
    return out


def lines(s):
    """The Friday report's text lines (the saved report)."""
    L = ["", "## Slippage on held trades (drift and news): price got against price expected; assumed %.2f%% a side"
         % s["assumed_pct"]]
    for label, name in (("buy", "buys (against the quote just before the order)"),
                        ("sell", "sales (against the stop or the price seen)"), ("stop", "of which stop sales")):
        g = s[label]
        L.append("- %s: %s%s" % (name, "none measured yet" if not g["n"] else "%d measured, average %+.2f%%, worst %+.2f%%"
                                  % (g["n"], g["avg_pct"], g["worst_pct"]),
                                  "" if not g["unmeasured"] else "; %d with no price to compare" % g["unmeasured"]))
    L.append("- note: " + s["note"])
    return L


def main(argv=None):
    ap = argparse.ArgumentParser(description="Slippage on held trades (report only).")
    ap.add_argument("cmd", choices=("show", "record"))
    ap.add_argument("--state-dir", default=os.path.join(ROOT, "trader"))
    a = ap.parse_args(argv)
    if a.cmd == "record":
        for r in record(a.state_dir):
            print("new: %s" % json.dumps(r, sort_keys=True))
    for r in _lines(log_path(a.state_dir)):
        print("%s %-4s %-6s %-5s expected %s got %s: %s (%s)" % (r.get("day"), r.get("kind"), r.get("symbol"),
                                                               r.get("strategy"), r.get("expected"), r.get("got"),
                                                               "n/a" if r.get("slip_pct") is None else "%+.2f%%" % r["slip_pct"],
                                                               r.get("basis")))
    print("\n".join(lines(summary(a.state_dir))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
