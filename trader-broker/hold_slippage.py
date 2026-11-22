#!/usr/bin/env python3
"""Slippage on held trades (drift and news): every real fill against the price expected, and against the real
market in the same minute. REPORT ONLY.

Every result in this pack assumes 0.05% slippage a side (on top of the 0.15% FX fee). The drift backtest shows
the edge over the S&P 500 is gone at 0.3% a side, so this measures it on each real fill:

  expected   buy: the fresh quote taken just before the order (drift and news buys.jsonl, "price").
             sell: a stop sale's stop price; a close or approved sale's price the guard saw (t212-guard.json, kept
             for that day only, so this runs every evening); otherwise "no price to compare".
  fill       the broker's own fill price and time (Trading 212's order history, through holdings_t212.fill_finder;
             the holdings page's average price when the history cannot be read).
  market     the real market in the minute of the fill: Yahoo 1-minute bars (kept about 30 days), its high, low and
             middle ((high + low) / 2).

  slip_pct          fill against expected, % (positive = worse for us: paid more, or got less)
  move_pct          the market's middle in the fill minute against expected: the market moving (timing)
  market_gap_pct    the fill against the market's middle in that minute: the broker's own pricing
  (slip_pct is about move_pct + market_gap_pct)

After 10 measured fills the evening run says once, on the phone, whether slippage is consistently above 0.3% a
side (average over 0.3% AND at least 7 in 10 fills over it); the Friday report shows every fill, the running
average and the same verdict.

IMPORTANT: on the Practice (demo) account Trading 212 SIMULATES fills ("OTC"), so these figures may not match
what a real account would pay. The Yahoo quote can lag at 09:33, just after the open.

It reads only; it never places, cancels or changes an order and never edits a holdings page. Broker-neutral:
the broker's fill lookup is passed in. Files: trader/state/fills.json (one row per buy and per sale),
trader/state/slippage.jsonl (each row as written or completed).

    python3 trader-broker/hold_slippage.py show     # every fill and the averages (no network)
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
BAR_PCT = 0.30                  # the level the owner asked about: drift's edge is gone at 0.3% a side
JUDGE_AT = 10                   # measured fills before judging
CONSISTENT_SHARE = 0.7          # "consistently": at least 7 in 10 fills over the bar, and the average over it
MINUTE_DAYS = 25                # calendar days the 1-minute data is looked for (Yahoo keeps about 30)
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


def _date(v):
    try:
        return datetime.date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


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


def state_path(sd):
    return os.path.join(sd, "state", "fills.json")


def log_path(sd):
    return os.path.join(sd, "state", "slippage.jsonl")


def load(sd):
    doc = _read(state_path(sd))
    if not isinstance(doc.get("rows"), dict):
        doc = {"rows": {}}
    return doc


def save(sd, doc):
    os.makedirs(os.path.dirname(state_path(sd)), exist_ok=True)
    tmp = "%s.%d.tmp" % (state_path(sd), os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, sort_keys=True)
    os.replace(tmp, state_path(sd))


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


def worse(kind, expected, got):
    """% worse than expected: a buy above, or a sale below, the expected price is positive."""
    return round(100.0 * ((got / expected - 1) if kind == "buy" else (1 - got / expected)), 4)


def _base_rows(sd, doc):
    """New rows for each drift or news buy that has filled and each sale that has closed (expected price set now)."""
    H = _load("trader-broker", "holdings")
    q, close = quotes(sd), seen_at_close(sd)
    pages = {}
    for p in H.load(sd).get("pages") or []:
        strategy = H.strategy_of(p)
        if strategy not in H.HOLD or p.get("status") == "waiting":
            continue
        pages[p["id"]] = p
        base = {"page": p.get("id"), "symbol": p.get("symbol"), "strategy": strategy,
                "ref": (p.get("broker_ref") or {}).get("ref"), "qty": p.get("qty")}
        if _num(p.get("buy_price")) and "%s:buy" % p["id"] not in doc["rows"]:
            exp = q.get((strategy, str(p.get("symbol")).upper(), str(p.get("buy_date"))))
            doc["rows"]["%s:buy" % p["id"]] = dict(base, kind="buy", day=p.get("buy_date"), expected=exp,
                                                   basis="the quote just before the order" if exp else "no price to compare",
                                                   fill_price=_num(p.get("buy_price")), fill_from="holdings page average")
        res = p.get("result") or {}
        if p.get("status") == "closed" and _num(res.get("price")) and "%s:sell" % p["id"] not in doc["rows"]:
            why = str(res.get("why") or "")
            exp, basis = None, "no price to compare"
            if why == "stop" and _num(p.get("stop")):
                exp, basis = float(p["stop"]), "the stop price"
            elif why in ("sold at the close", "sold because you approved it"):
                exp = close.get((base["ref"], str(res.get("date"))))
                basis = "the price the guard saw when it sold" if exp else basis
            doc["rows"]["%s:sell" % p["id"]] = dict(base, kind="sell", day=res.get("date"), why=why, expected=exp,
                                                    basis=basis, fill_price=_num(res.get("price")),
                                                    fill_from="holdings page result")
    return pages


def _market(row, minutes):
    """The fill minute's bar and the split of the slippage, from 1-minute bars {NY minute: (o, h, l, c, v)}."""
    tk = _load("trader-broker", "ticket")
    at = datetime.datetime.fromisoformat(str(row["fill_at"]).replace("Z", "+00:00"))
    m = tk.to_eastern(at.astimezone(datetime.timezone.utc).replace(tzinfo=None)).replace(second=0, microsecond=0)
    bar, used = None, None
    for back in range(0, 3):                      # that minute, or the last minute with trades just before it
        k = m - datetime.timedelta(minutes=back)
        if k in minutes:
            bar, used = minutes[k], k
            break
    if bar is None:
        return {"error": "no market trades in the minute of the fill (%s New York)" % m.strftime("%H:%M")}
    o, h, l, c = bar[:4]
    mid = (h + l) / 2.0
    out = {"minute": used.strftime("%H:%M"), "open": o, "high": h, "low": l, "close": c, "mid": round(mid, 4),
           "in_range": l - 1e-9 <= row["fill_price"] <= h + 1e-9,
           "market_gap_pct": worse(row["kind"], mid, row["fill_price"])}
    if _num(row.get("expected")):
        out["move_pct"] = worse(row["kind"], float(row["expected"]), mid)
    return out


def record(sd, today=None, fill_fn=None, minutes_fn=None, say=None):
    """Bring the fill record up to date: new rows, the broker's fill time and price, the market in that minute.
    Returns {"new": [...], "completed": [...], "flag": None | summary-when-first-judged}. Never raises for one row."""
    today = today or datetime.date.today()
    doc = load(sd)
    before = set(doc["rows"])
    _base_rows(sd, doc)
    new = [k for k in doc["rows"] if k not in before]
    done, cache = [], {}
    for key, row in sorted(doc["rows"].items()):
        day = _date(row.get("day"))
        fresh = day is not None and (today - day).days <= MINUTE_DAYS
        changed = False
        if fill_fn is not None and not row.get("fill_at") and fresh:
            try:
                f = fill_fn(row)
                if f and f.get("at") and _num(f.get("price")):
                    row.update(fill_at=f["at"], fill_price=float(f["price"]), fill_from="broker order history",
                               fill_method=f.get("method"), order_id=f.get("order_id"))
                    changed = True
            except Exception as e:
                row["fill_error"] = str(e)[:160]
        if row.get("fill_at") and minutes_fn is not None and not (row.get("market") or {}).get("mid") and fresh:
            try:
                ck = (row["symbol"], str(day))
                if ck not in cache:
                    cache[ck] = minutes_fn(row["symbol"], day)
                row["market"] = _market(row, cache[ck])
                changed = True
            except Exception as e:
                row["market"] = {"error": "1-minute prices could not be read: %s" % str(e)[:120]}
        elif not fresh and not (row.get("market") or {}).get("mid") and not (row.get("market") or {}).get("gone"):
            row["market"] = {"error": "the 1-minute prices are no longer available", "gone": True}
            changed = True
        if _num(row.get("expected")) and _num(row.get("fill_price")):
            row["slip_pct"] = worse(row["kind"], float(row["expected"]), float(row["fill_price"]))
        if changed or key in new:
            done.append(key)
    s = summary_of(doc)
    flag = None
    if s["judged"] and not doc.get("judged_at"):
        doc["judged_at"] = today.isoformat()
        flag = s
    save(sd, doc)
    if done:
        os.makedirs(os.path.dirname(log_path(sd)), exist_ok=True)
        with open(log_path(sd), "a", encoding="utf-8") as fh:
            for k in done:
                fh.write(json.dumps(dict(doc["rows"][k], key=k, logged=today.isoformat()), sort_keys=True) + "\n")
    if say and done:
        say("slippage: %d fill row(s) written or completed" % len(done))
    return {"new": new, "completed": done, "flag": flag}


def rows(sd):
    return [r for _, r in sorted(load(sd)["rows"].items(), key=lambda kv: (str(kv[1].get("day")), kv[0]))]


def _avg(xs):
    return round(sum(xs) / len(xs), 3) if xs else None


def summary_of(doc):
    rs = [r for _, r in sorted(doc["rows"].items(), key=lambda kv: (str(kv[1].get("day")), kv[0]))]
    measured = [r for r in rs if r.get("slip_pct") is not None]
    out = {"assumed_pct": ASSUMED_PCT, "bar_pct": BAR_PCT, "note": NOTE, "rows": rs, "n": len(measured),
           "unmeasured": len(rs) - len(measured)}
    for label, pick in (("buy", lambda r: r["kind"] == "buy"), ("sell", lambda r: r["kind"] == "sell"),
                        ("stop", lambda r: r["kind"] == "sell" and r.get("why") == "stop")):
        sel = [r for r in rs if pick(r)]
        got = [r["slip_pct"] for r in sel if r.get("slip_pct") is not None]
        out[label] = {"n": len(got), "unmeasured": len(sel) - len(got), "avg_pct": _avg(got),
                      "worst_pct": round(max(got), 3) if got else None}
    out["avg_pct"] = _avg([r["slip_pct"] for r in measured])
    out["avg_market_gap_pct"] = _avg([(r.get("market") or {})["market_gap_pct"] for r in rs
                                      if (r.get("market") or {}).get("market_gap_pct") is not None])
    out["avg_move_pct"] = _avg([(r.get("market") or {})["move_pct"] for r in rs
                                if (r.get("market") or {}).get("move_pct") is not None])
    over = sum(1 for r in measured if r["slip_pct"] > BAR_PCT)
    out["over_bar"] = over
    out["judged"] = len(measured) >= JUDGE_AT
    out["consistent"] = (out["judged"] and out["avg_pct"] > BAR_PCT and over >= CONSISTENT_SHARE * len(measured))
    return out


def summary(sd):
    return summary_of(load(sd))


def row_line(r):
    m = r.get("market") or {}
    return "%s %-4s %-6s %-5s expected %s, fill %s%s: %s against expected; market %s%s (%s)" % (
        r.get("day"), r.get("kind"), r.get("symbol"), r.get("strategy"), r.get("expected"), r.get("fill_price"),
        " at %s" % r["fill_at"] if r.get("fill_at") else "",
        "n/a" if r.get("slip_pct") is None else "%+.2f%%" % r["slip_pct"],
        "gap %+.2f%%, move %s" % (m["market_gap_pct"], "n/a" if m.get("move_pct") is None else "%+.2f%%" % m["move_pct"])
        if m.get("market_gap_pct") is not None else (m.get("error") or "not looked up yet"),
        " (%s %.2f-%.2f)" % (m.get("minute"), m.get("low"), m.get("high")) if m.get("mid") else "", r.get("basis"))


def lines(s):
    """The Friday report's text lines (the saved report): every fill, the averages, the verdict."""
    L = ["", "## Slippage on held trades (drift and news): every fill; assumed %.2f%% a side; the level that matters %.2f%%"
         % (s["assumed_pct"], s["bar_pct"])]
    for r in s["rows"]:
        L.append("- " + row_line(r))
    if not s["rows"]:
        L.append("- no fills yet")
    L.append("- running average against expected: %s over %d measured fill(s) (%d over %.2f%%); split: market moving %s, "
             "the broker's own pricing %s" % ("n/a" if s["avg_pct"] is None else "%+.2f%%" % s["avg_pct"], s["n"], s["over_bar"],
                                             s["bar_pct"], "n/a" if s["avg_move_pct"] is None else "%+.2f%%" % s["avg_move_pct"],
                                             "n/a" if s["avg_market_gap_pct"] is None else "%+.2f%%" % s["avg_market_gap_pct"]))
    L.append("- verdict: %s" % verdict_words(s))
    L.append("- note: " + s["note"])
    return L


def verdict_words(s):
    if not s["judged"]:
        return "%d of %d fills measured: too few to judge" % (s["n"], JUDGE_AT)
    return ("CONSISTENTLY ABOVE %.2f%% a side" if s["consistent"] else "not consistently above %.2f%% a side") % s["bar_pct"]


def main(argv=None):
    ap = argparse.ArgumentParser(description="Slippage on held trades (report only).")
    ap.add_argument("cmd", choices=("show", "record"))
    ap.add_argument("--state-dir", default=os.path.join(ROOT, "trader"))
    a = ap.parse_args(argv)
    if a.cmd == "record":
        D = _load("trader-broker", "drift")
        record(a.state_dir, minutes_fn=D.minute_bars, say=print)
    print("\n".join(lines(summary(a.state_dir))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
