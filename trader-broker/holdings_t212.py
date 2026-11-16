#!/usr/bin/env python3
"""Trading 212's side of the holdings record (trader-broker/holdings.py): it turns the account into the
plain "view" the record works on, and runs the daily match.

READ-ONLY at Trading 212: positions, pending orders, the account summary and the order history. It never
places, cancels or changes an order, and a mismatch is reported, never fixed.

    python3 trader-broker/holdings_t212.py match     # the daily match now, as text (demo, read-only)
"""

import argparse
import datetime
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

t212 = None                      # trader-broker/t212.py; the autopilot and the tests set theirs
holdings = None                  # trader-broker/holdings.py
_MODS = {}

DEAD = ("failed", "declined")    # a buy record that bought nothing


def _load(folder, name):
    key = (folder, name)
    if key not in _MODS:
        spec = importlib.util.spec_from_file_location("hold_%s_%s" % (folder.replace("-", "_"), name),
                                                      os.path.join(ROOT, folder, name + ".py"))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        _MODS[key] = m
    return _MODS[key]


def _t212():
    global t212
    if t212 is None:
        t212 = _load("trader-broker", "t212")
    return t212


def _hold():
    global holdings
    if holdings is None:
        holdings = _load("trader-broker", "holdings")
    return holdings


def _sym(ticker):
    return str(ticker or "").split("_")[0].upper()


def _is_buy(o, T):
    side = str(o.get("side") or "").upper()
    return side == "BUY" or (not side and (T._num(o.get("quantity")) or 0) > 0)


def buy_records(state, env, ideas=None):
    """This pack's buys on this account, one per (order, day), the latest status of each, oldest first.
    `ideas`: {idea id: the news idea}, so a news page carries its reason, event and sources."""
    T = _t212()
    latest = {}
    for r in (state or {}).get("orders") or []:
        if (r.get("broker") != "trading212" or r.get("env") != env or str(r.get("side", "")).lower() != "buy"
                or r.get("kind") != "entry" or not r.get("t212_ticker")):
            continue
        latest["%s:%s" % (r.get("fingerprint"), r.get("date"))] = r
    out = []
    for key, r in latest.items():
        if r.get("status") in DEAD or r.get("status") == "sending":
            continue
        b = {"key": key, "symbol": str(r.get("symbol") or _sym(r["t212_ticker"])).upper(), "ref": r["t212_ticker"],
             "strategy": r.get("strategy") or "intraday", "date": r.get("date"), "qty": T._num(r.get("qty")),
             "entry": T._num(r.get("entry")), "stop": T._num(r.get("stop")), "sell_date": r.get("sell_date"),
             "cost_gbp": T._num(r.get("cost_gbp")), "max_loss_gbp": T._num(r.get("max_loss_gbp")),
             "idea": r.get("idea"), "status": r.get("status")}
        idea = (ideas or {}).get(r.get("idea")) if r.get("idea") else None
        if idea:
            b.update(reason=idea.get("why"), wrong_if=idea.get("wrong_if"), event=idea.get("event"),
                     event_date=idea.get("event_date"), sources=idea.get("sources"),
                     sell_rule=idea.get("sell_rule"), company=idea.get("company"))
        out.append(b)
    out.sort(key=lambda b: (str(b["date"]), b["symbol"]))
    return out


def view(positions, pending, summary, state, guard_state, env, rate, ideas=None):
    """The plain view of the account for holdings.py. Pure: no request is made here."""
    T = _t212()
    pos, stops, waiting = [], [], []
    for p in positions or []:
        ticker, qty = T._o_ticker(p), T._num(p.get("quantity")) or 0.0
        if ticker and qty > 0:
            pos.append({"symbol": _sym(ticker), "ref": ticker, "qty": qty,
                        "avg": T._num(p.get("averagePricePaid")) or T._num(p.get("averagePrice")),
                        "price": T._num(p.get("currentPrice"))})
    for ticker in sorted({T._o_ticker(o) for o in pending or [] if T._o_ticker(o)}):
        for o in T.guard_sell_orders(pending, ticker)[0]:
            stops.append({"symbol": _sym(ticker), "ref": ticker, "qty": abs(T._num(o.get("quantity")) or 0),
                          "stop": T._num(o.get("stopPrice")), "id": o.get("id")})
    for o in pending or []:
        if T._o_ticker(o) and _is_buy(o, T):
            left = abs(T._num(o.get("quantity")) or 0) - abs(T._num(o.get("filledQuantity")) or 0)
            if left > 1e-9:
                waiting.append({"symbol": _sym(T._o_ticker(o)), "ref": T._o_ticker(o), "qty": left})
    soft = {t: T._num(v.get("stop")) for t, v in ((guard_state or {}).get("software") or {}).items()
            if isinstance(v, dict) and T._num(v.get("stop"))}
    return {"broker": "trading212", "account": env, "usd_per_gbp": rate,
            "account_gbp": T._num((summary or {}).get("totalValue")),
            "cash_gbp": T._num(((summary or {}).get("cash") or {}).get("availableToTrade")),
            "positions": pos, "stops": stops, "soft_stops": soft, "waiting": waiting,
            "buys": buy_records(state, env, ideas), "sales": []}


def read_view(client, sd, rate, ideas=None):
    """view() from Trading 212 (read-only) and the state files."""
    T = _t212()
    positions = client.get(T.POSITIONS) or []
    pending = client.get(T.ORDERS) or []
    summary = client.get(T.SUMMARY) or {}
    state = T._read_json(os.path.join(sd, "state", "broker.json"), {}) or {}
    guard = T._read_json(os.path.join(sd, "state", "t212-guard.json"), {}) or {}
    return view(positions, pending, summary, state, guard, client.env, rate, ideas)


def sales_since(client, since_day, report_mod, guard_state=None):
    """Closed round trips from Trading 212's own fills since `since_day` (a New York date), as the view's
    "sales". Read-only."""
    T = _t212()
    tk = T.ticket_module()
    since = report_mod.alerts.eastern_to_utc(datetime.datetime.combine(since_day, datetime.time(0, 0)))
    trips, _ = report_mod.round_trips(report_mod.history(client, since))
    eod = (guard_state or {}).get("eod") or {}
    out = []
    for c in trips:
        if not c.get("entry_at"):
            continue
        s = c.get("sell") or {}
        day = tk.to_eastern(c["exit_at"]).date().isoformat()
        if s.get("from") and s["from"] != "API":
            why = "sold in the Trading 212 app"
        elif str(s.get("type") or "").upper() in ("STOP", "STOP_LIMIT"):
            why = "stop"
        elif eod.get("date") == day and any((x or {}).get("ticker") == c["ticker"] for x in eod.get("sold") or []):
            why = "sold at the close"
        else:
            why = "sold by the guard"
        out.append({"ref": c["ticker"], "qty": c["qty"], "exit": c["exit"], "date": day,
                    "buy_date": tk.to_eastern(c["entry_at"]).date().isoformat(), "pl_gbp": c.get("pl"),
                    "cost_gbp": c.get("cost_gbp"), "why": why})
    return out


def daily_match(client, sd, rate, today, report_mod=None, ideas=None):
    """The daily match: read the account, bring the pages up to date, list what disagrees.
    Returns {"view", "events", "mismatches", "new", "risk"}. Read-only at Trading 212; writes only
    trader/state/holdings.json and holdings-log.jsonl."""
    T, H = _t212(), _hold()
    v = read_view(client, sd, rate, ideas)
    doc = H.load(sd)
    held = {p["ref"] for p in v["positions"]}
    waiting = {w["ref"] for w in v["waiting"]}
    gone = [p for p in H.live(doc, v["broker"], v["account"])
            if (p.get("broker_ref") or {}).get("ref") not in held | waiting]
    gone += [b for b in v["buys"] if b["ref"] not in held | waiting
             and str(b.get("date") or "") >= str(doc.get("since") or today)]
    if gone and report_mod is not None:
        days = [str(x.get("buy_date") or x.get("date") or today)[:10] for x in gone]
        guard = T._read_json(os.path.join(sd, "state", "t212-guard.json"), {}) or {}
        v["sales"] = sales_since(client, datetime.date.fromisoformat(min(days)), report_mod, guard)
    events = H.sync(doc, v, today)
    mism = H.match(doc, v, today)
    new = H.fresh(doc, mism)
    H.save(sd, doc)
    for ev, page in events:
        H.log(sd, ev, id=page.get("id"), symbol=page.get("symbol"), strategy=page.get("strategy"),
              status=page.get("status"), result=page.get("result"))
    for m in new:
        H.log(sd, "mismatch_reported", kind=m["kind"], symbol=m["symbol"], record=m["record"], broker=m["broker"])
    try:
        risk = H.open_risk(v)
    except ValueError:
        risk = None
    return {"view": v, "events": events, "mismatches": mism, "new": new, "risk": risk}


def text(res):
    H = _hold()
    lines = ["HOLDINGS MATCH (report only): %d position%s at the broker, %d disagreement%s" % (
        len(res["view"]["positions"]), "" if len(res["view"]["positions"]) == 1 else "s",
        len(res["mismatches"]), "" if len(res["mismatches"]) == 1 else "s")]
    lines += ["  !!! " + H.mismatch_line(m) for m in res["mismatches"]]
    if res.get("risk"):
        lines.append("  open risk at the stops: GBP %.2f" % res["risk"]["open_risk_gbp"])
    return "\n".join(lines)


def main(argv=None, environ=None):
    ap = argparse.ArgumentParser(description="The daily holdings match against Trading 212 (demo, read-only).")
    ap.add_argument("cmd", choices=["match"])
    ap.add_argument("--state-dir", default="trader")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    T = _t212()
    env = os.environ if environ is None else environ
    try:
        key, secret = T.credentials(env)
        client = T.Client("demo", key, secret, state_dir=a.state_dir)
        rate = (T._read_json(os.path.join(a.state_dir, "fx.json"), {}) or {}).get("usd_per_gbp") or T.fetch_gbpusd()[0]
        report = _load("trader-broker", "report")
        today = T._today_ny(T.ticket_module())
        res = daily_match(client, a.state_dir, float(rate), today, report)
    except T.T212Error as e:
        print("holdings match: could not check today (%s)" % e, file=sys.stderr)
        return 1
    print(json.dumps({k: res[k] for k in ("mismatches", "risk")}, indent=2) if a.json else text(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
