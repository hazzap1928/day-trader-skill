#!/usr/bin/env python3
"""Closing report (read-only): today's trades from Trading 212's own fills, after fees.

    python3 trader-broker/report.py              # build it, save it, send it to your phone
    python3 trader-broker/report.py --no-send    # build and save it only

The autopilot sends it about 5 minutes after the end-of-day close (15:50 New York).
For each trade it uses Trading 212's order history: the fill prices, the FX rate on
each fill and the fees it charged. P&L is in GBP after fees, buys matched to sells
first in, first out. Exit reasons come from the order type and what the guard
recorded. The Time Machine's odds come from the autopilot's tickets.

Read-only: it only GETs. Education and tooling, not financial advice.
"""

import argparse
import datetime
import importlib.util
import json
import os
import sys
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
HISTORY_DAYS = 21          # how far back to look for the buy behind a sell
MAX_PAGES = 12


def _load(folder, name):
    spec = importlib.util.spec_from_file_location("report_" + name, os.path.join(ROOT, folder, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


t212 = _load("trader-broker", "t212")
tk = t212.ticket_module()
phone_mod = t212.phone_module()
alerts = _load("trader-alerts", "alerts")


def _now():
    return time.time()


def _utc():
    return datetime.datetime.fromtimestamp(_now(), datetime.timezone.utc).replace(tzinfo=None, microsecond=0)


def _ts(text):
    try:
        return datetime.datetime.strptime(str(text)[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None


def _gbp(x):
    return "no data" if x is None else "£{:,.2f}".format(x)


def _signed(x):
    return "no data" if x is None else ("+" if x >= 0 else "-") + "£{:,.2f}".format(abs(x))


# ---------------------------------------------------------------- Trading 212's fills

def history(client, since_utc):
    """Every fill since since_utc, oldest first: [{ticker, side, qty, price, gross_gbp, fees_gbp, at,
    type, from, order_id}]. Raises T212Error when the history cannot be read."""
    fills, query = [], {"limit": 50}
    for _ in range(MAX_PAGES):
        doc = client.get(t212.HISTORY, query) or {}
        items = doc.get("items") or []
        oldest = None
        for it in items:
            o, f = it.get("order") or {}, it.get("fill") or {}
            at = _ts(f.get("filledAt") or o.get("createdAt") or "")
            oldest = at if oldest is None or (at and at < oldest) else oldest
            q = abs(t212._num(f.get("quantity")) or 0)
            px = t212._num(f.get("price"))
            if not f or not q or not px or at is None or at < since_utc:
                continue
            w = f.get("walletImpact") or {}
            rate = t212._num(w.get("fxRate")) or None
            fees = sum(abs(t212._num(x.get("quantity")) or 0) for x in w.get("taxes") or [])
            fills.append({"ticker": o.get("ticker") or (o.get("instrument") or {}).get("ticker"),
                          "side": str(o.get("side") or ("BUY" if (t212._num(o.get("quantity")) or 0) > 0 else "SELL")).upper(),
                          "qty": q, "price": px, "gross_gbp": (q * px / rate) if rate else None, "fees_gbp": fees,
                          "at": at, "type": str(o.get("type") or "").upper(), "from": o.get("initiatedFrom"),
                          "order_id": o.get("id"), "stop_price": t212._num(o.get("stopPrice"))})
        nxt = doc.get("nextPagePath")
        if not nxt or (oldest is not None and oldest < since_utc):
            break
        q = urllib.parse.parse_qs(urllib.parse.urlparse(nxt).query)
        query = {k: v[0] for k, v in q.items()}
    fills.sort(key=lambda x: x["at"])
    return fills


def round_trips(fills):
    """Sells matched to earlier buys, first in first out. Returns (closed, open_lots)."""
    lots, closed = {}, []
    for f in fills:
        t = f["ticker"]
        if f["side"] == "BUY":
            lots.setdefault(t, []).append(dict(f, left=f["qty"]))
            continue
        need, cost, buy_px_q, buys = f["qty"], 0.0, 0.0, []
        known = f["gross_gbp"] is not None
        while need > 1e-9 and lots.get(t):
            lot = lots[t][0]
            take = min(need, lot["left"])
            share = take / lot["qty"]
            if lot["gross_gbp"] is None:
                known = False
            else:
                cost += (lot["gross_gbp"] + lot["fees_gbp"]) * share
            buy_px_q += lot["price"] * take
            buys.append(lot)
            lot["left"] -= take
            need -= take
            if lot["left"] <= 1e-9:
                lots[t].pop(0)
        matched = f["qty"] - need
        if matched <= 1e-9:
            closed.append({"ticker": t, "qty": f["qty"], "entry": None, "exit": f["price"], "pl": None,
                           "exit_at": f["at"], "entry_at": None, "sell": f, "note": "no buy found in the history"})
            continue
        proceeds = (f["gross_gbp"] - f["fees_gbp"]) * matched / f["qty"] if known else None
        closed.append({"ticker": t, "qty": matched, "entry": buy_px_q / matched, "exit": f["price"],
                       "pl": (proceeds - cost) if known else None, "cost_gbp": cost if known else None, "fees": f["fees_gbp"] + sum(b["fees_gbp"] for b in buys),
                       "entry_at": buys[0]["at"], "exit_at": f["at"], "sell": f})
    open_lots = {t: ls for t, ls in lots.items() if ls}
    return closed, open_lots


# ---------------------------------------------------------------- what each trade was

def exit_reason(trade, guard_state, ticket_stop, day):
    s = trade["sell"]
    t = trade["ticker"]
    eod = guard_state.get("eod") or {}
    trail = (guard_state.get("trail") or {}).get(t) or {}
    if s.get("from") and s["from"] != "API":
        return "sold in the Trading 212 app"
    if s["type"] in ("STOP", "STOP_LIMIT"):
        if trail.get("stop") and ticket_stop and float(trail["stop"]) > float(ticket_stop) + 1e-9:
            return "trailing stop"
        return "stop"
    if s["type"] == "MARKET":
        if eod.get("date") == day and any(x.startswith(t + ":") for x in eod.get("closed") or []):
            return "end-of-day close"
        if trail.get("half_sold"):
            return "half sold at the target"
        return "market sell by the guard (stop crossed)"
    return s["type"].lower() or "unknown"


def ticket_for(symbol, day, broker_state, pilot_state):
    """The placement record (stop, target) and the autopilot ticket (Time Machine odds) for a symbol."""
    rec = None
    for r in (broker_state or {}).get("orders") or []:
        if r.get("symbol") == symbol and r.get("kind") == "entry" and r.get("status") in ("placed", "stop_placed", "stop_not_placed"):
            if r.get("date", "") <= day:
                rec = r
    tm = None
    for x in (pilot_state or {}).get("tickets") or []:
        if x.get("symbol") == symbol and x.get("tm"):
            tm = x["tm"]
    return rec, tm


def outcome(trade, rec):
    """target, stop or close: what the trade did, for the Time Machine comparison."""
    reason = trade.get("reason") or ""
    tgt = t212._num((rec or {}).get("target"))
    if "target" in reason or (tgt and trade["exit"] >= tgt - 1e-9):
        return "target"
    if "stop" in reason:
        return "stop"
    return "close"


# ---------------------------------------------------------------- the report

def build(client, state_dir):
    now = _utc()
    ny = tk.to_eastern(now)
    day = ny.date()
    week_start = day - datetime.timedelta(days=day.weekday())
    since = alerts.eastern_to_utc(datetime.datetime.combine(day - datetime.timedelta(days=HISTORY_DAYS),
                                                            datetime.time(0, 0)))
    r = {"generated": now, "day": day, "week_start": week_start, "errors": []}
    try:
        fills = history(client, since)
    except t212.T212Error as e:
        fills = None
        r["errors"].append("order history: %s" % t212.scrub(e, client._secrets))
    guard = t212._read_json(os.path.join(state_dir, "state", "t212-guard.json"), {}) or {}
    broker = t212._read_json(os.path.join(state_dir, "state", "broker.json"), {}) or {}
    pilot = t212._read_json(os.path.join(state_dir, "state", "autopilot.json"), {}) or {}
    today, week, bought_today = [], [], []
    if fills is not None:
        closed, open_lots = round_trips(fills)
        for c in closed:
            d = tk.to_eastern(c["exit_at"]).date()
            sym = (c["ticker"] or "").split("_")[0].upper()
            rec, tm = ticket_for(sym, d.isoformat(), broker, pilot)
            c.update(symbol=sym, reason=exit_reason(c, guard, t212._num((rec or {}).get("stop")), d.isoformat()),
                     stop=t212._num((rec or {}).get("stop")), target=t212._num((rec or {}).get("target")), tm=tm)
            c["outcome"] = outcome(c, rec)
            risk = (c["entry"] - c["stop"]) if (c.get("entry") and c.get("stop")) else None
            c["r"] = ((c["exit"] - c["entry"]) / risk) if risk and risk > 0 else None
            if d >= week_start:
                week.append(c)
            if d == day:
                today.append(c)
        for f in fills:
            if f["side"] == "BUY" and tk.to_eastern(f["at"]).date() == day:
                bought_today.append(f)
    r.update(today=today, week=week, bought_today=bought_today, fills_ok=fills is not None)
    try:
        s = client.get(t212.SUMMARY) or {}
        r["total"] = t212._num(s.get("totalValue"))
        r["cash"] = t212._num((s.get("cash") or {}).get("availableToTrade"))
    except t212.T212Error as e:
        r["total"] = r["cash"] = None
        r["errors"].append("account: %s" % t212.scrub(e, client._secrets))
    try:
        r["open"] = [p for p in client.get(t212.POSITIONS) or [] if (t212._num(p.get("quantity")) or 0) > 0]
    except t212.T212Error as e:
        r["open"] = None
        r["errors"].append("positions: %s" % t212.scrub(e, client._secrets))
    r["alerts"] = guard_alerts(state_dir, alerts.eastern_to_utc(datetime.datetime.combine(day, datetime.time(0, 0))))
    r["unclear"] = guard.get("unclear") or {}
    return r


def guard_alerts(state_dir, since_utc):
    path = os.path.join(state_dir, "logs", "guard-alerts.jsonl")
    out = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    a = json.loads(line)
                    at = datetime.datetime.strptime(a["at"], "%Y-%m-%dT%H:%M:%SZ")
                except (ValueError, KeyError):
                    continue
                if at >= since_utc:
                    out.append(a.get("text", ""))
    return out


def totals(trades):
    known = [t["pl"] for t in trades if t.get("pl") is not None]
    wins = sum(1 for p in known if p > 0)
    return {"n": len(trades), "pl": sum(known) if known else (0.0 if not trades else None),
            "wins": wins, "rate": (100.0 * wins / len(known)) if known else None,
            "unknown": len(trades) - len(known)}


def tm_line(trades):
    """Today's results against the Time Machine's odds."""
    with_tm = [t for t in trades if t.get("tm") and t["tm"].get("p_target") is not None]
    if not trades:
        return "No closed trades today, so nothing to compare."
    if not with_tm:
        return "No Time Machine odds were recorded for today's trades (placed by hand, or before the autopilot kept them)."
    pred = 100.0 * sum(t["tm"]["p_target"] for t in with_tm) / len(with_tm)
    sides = [t for t in with_tm if t["outcome"] in ("target", "stop")]
    hit = sum(1 for t in sides if t["outcome"] == "target")
    closes = len(with_tm) - len(sides)
    actual = ("%d of %d reached the target first (%.0f %%)" % (hit, len(sides), 100.0 * hit / len(sides))
              if sides else "none reached the target or the stop")
    rs = [t["r"] for t in with_tm if t.get("r") is not None]
    return ("The Time Machine gave the target first %.0f %% on average over %d trade%s; %s%s. Average result %s. "
            "One day is far too few trades to judge the model." % (
                pred, len(with_tm), "" if len(with_tm) == 1 else "s", actual,
                "" if not closes else ", %d closed at the end of the day" % closes,
                ("%+.2fR" % (sum(rs) / len(rs))) if rs else "no data"))


def full_text(r):
    L = ["# Closing report, %s (New York)" % r["day"].strftime("%A %d %B %Y"), "",
         "Demo account. From Trading 212's own fills; P&L in GBP after FX fees. Read-only.", ""]
    for e in r["errors"]:
        L.append("- no data: %s" % e)
    L.append("## Today's trades")
    if not r["fills_ok"]:
        L.append("- no data (the order history did not load)")
    elif not r["today"]:
        L.append("- No trades closed today.")
    for t in r["today"]:
        L.append("- %s: %s shares, in $%s, out $%s, %s; P&L %s%s" % (
            t["symbol"], t212._qty(t["qty"]), t212._fmt(t["entry"]), t212._fmt(t["exit"]), t["reason"],
            _signed(t["pl"]), "" if t.get("r") is None else " (%+.2fR)" % t["r"]))
        if t.get("tm"):
            L.append("  - Time Machine: target first %.1f %%, stop first %.1f %%; it %s" % (
                100 * t["tm"]["p_target"], 100 * t["tm"]["p_stop"],
                {"target": "reached the target", "stop": "hit the stop", "close": "closed at the end of the day"}[t["outcome"]]))
    for f in r["bought_today"]:
        if not any(t["ticker"] == f["ticker"] and t["entry_at"] == f["at"] for t in r["today"]):
            L.append("- Bought today, still open: %s %s at $%s" % (
                f["ticker"].split("_")[0], t212._qty(f["qty"]), t212._fmt(f["price"])))
    d, w = totals(r["today"]), totals(r["week"])
    L += ["", "## Totals",
          "- Today: %d trade%s, %s, win rate %s" % (d["n"], "" if d["n"] == 1 else "s", _signed(d["pl"]),
                                                   "no data" if d["rate"] is None else "%.0f %% (%d of %d)" % (d["rate"], d["wins"], d["n"] - d["unknown"])),
          "- This week (from %s): %d trade%s, %s, win rate %s" % (
              r["week_start"].strftime("%a %d %b"), w["n"], "" if w["n"] == 1 else "s", _signed(w["pl"]),
              "no data" if w["rate"] is None else "%.0f %% (%d of %d)" % (w["rate"], w["wins"], w["n"] - w["unknown"])),
          "- Account value: %s (cash %s)" % (_gbp(r.get("total")), _gbp(r.get("cash"))),
          "", "## Still open and alerts"]
    if r["open"] is None:
        L.append("- Positions: no data")
    elif not r["open"]:
        L.append("- Nothing open.")
    for p in r["open"] or []:
        L.append("- STILL OPEN: %s %s, now $%s" % (t212._o_ticker(p), t212._qty(p.get("quantity")),
                                                  t212._fmt(p.get("currentPrice"))))
    L.append("- Guard alerts today: %s" % (len(r["alerts"]) or "none"))
    L += ["  - " + a for a in r["alerts"]]
    if r["unclear"]:
        L.append("- UNCLEAR guard orders: " + "; ".join("%s (%s)" % kv for kv in sorted(r["unclear"].items())))
    L += ["", "## Against the Time Machine", tm_line(r["today"]), "",
          "Education and tooling, not financial advice."]
    return "\n".join(L) + "\n"


def short_text(r, path):
    d, w = totals(r["today"]), totals(r["week"])
    lines = ["CLOSING REPORT (demo) %s: today %s over %d trade%s%s; week %s; account %s." % (
        r["day"].strftime("%a %d %b"), _signed(d["pl"]) if r["fills_ok"] else "no data", d["n"],
        "" if d["n"] == 1 else "s", "" if d["rate"] is None else ", %.0f %% won" % d["rate"],
        _signed(w["pl"]) if r["fills_ok"] else "no data", _gbp(r.get("total")))]
    for t in r["today"][:6]:
        lines.append("- %s %s: $%s -> $%s, %s, %s" % (t["symbol"], t212._qty(t["qty"]), t212._fmt(t["entry"]),
                                                      t212._fmt(t["exit"]), t["reason"], _signed(t["pl"])))
    if len(r["today"]) > 6:
        lines.append("- and %d more" % (len(r["today"]) - 6))
    if r["open"]:
        lines.append("!!! STILL OPEN: %s" % ", ".join("%s %s" % (t212._o_ticker(p), t212._qty(p.get("quantity")))
                                                      for p in r["open"]))
    if r["alerts"] or r["unclear"]:
        lines.append("!!! %d guard alert%s today%s" % (len(r["alerts"]), "" if len(r["alerts"]) == 1 else "s",
                                                       ", and UNCLEAR orders" if r["unclear"] else ""))
    for e in r["errors"]:
        lines.append("no data: " + e)
    tm = tm_line(r["today"]).split(" One day")[0]
    lines.append(tm if tm.endswith(".") else tm + ".")
    lines.append("Full report: %s" % (os.path.relpath(path, ROOT) if path.startswith(ROOT + os.sep) else path))
    return "\n".join(lines)


def save(r, state_dir):
    d = os.path.join(state_dir, "reports")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "%s.md" % r["day"].isoformat())
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(full_text(r))
    return path


def send(client, state_dir, phone=None, environ=None):
    """Build, save and send. Returns (sent, path, short text)."""
    r = build(client, state_dir)
    path = save(r, state_dir)
    text = short_text(r, path)
    ok = phone_mod.push(state_dir, text, environ=environ, phone=phone)
    return ok, path, text


def main(argv=None, environ=None):
    ap = argparse.ArgumentParser(description="Closing report (read-only) to your phone and trader/reports/.")
    ap.add_argument("--state-dir", default=os.path.join(ROOT, "trader"))
    ap.add_argument("--no-send", action="store_true")
    a = ap.parse_args(argv)
    environ = os.environ if environ is None else environ
    try:
        env = t212.pick_env(False, environ)
        key, secret = t212.credentials(environ)
    except t212.T212Error as e:
        sys.stderr.write("report: %s\n" % e)
        return 2
    client = t212.Client(env, key, secret, state_dir=a.state_dir, log=lambda m: None)
    if a.no_send:
        r = build(client, a.state_dir)
        path = save(r, a.state_dir)
        print(short_text(r, path))
        return 0
    ok, path, text = send(client, a.state_dir, environ=environ)
    print(text)
    if not ok:
        sys.stderr.write("report: not sent to the phone; saved %s\n" % path)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
