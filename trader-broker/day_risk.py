#!/usr/bin/env python3
"""Today's loss so far, for the daily loss limit (demo autopilot).

With "daily_loss_count": "open_risk" in trader/config.json, trader-risk counts against the daily
loss limit (max_daily_loss_gbp) only:
  - losses already realised today: each of today's entries that closed (fully or in part) at a net
    loss, from Trading 212's own fills, after its FX fees. A closed winner counts nothing (it frees
    its risk; its profit does not make room for more risk either), and
  - risk still open at the stops: for each of today's entries still held, the shares held x (the
    price paid - the stop it has now, plus the 0.15 % FX fee on the buy and on the sell). The stop
    it has now is the stop order at Trading 212, else the guard's software stop, else the planned
    stop. A stop raised to or above the price paid counts 0, never less.
  An entry the fills and positions do not show yet (not filled, or the history lags) counts its full
  planned max loss.

"daily_loss_count": "full_risk" (the default when the key is missing) keeps the old count: the full
max loss at the stop of every entry placed today, win or lose.

The autopilot writes trader/state/day-risk.json before each risk check. trader-risk uses it only when
it is for today and under DAY_RISK_MAX_AGE seconds old; otherwise it counts the old way and says so.

Read-only at Trading 212 (positions, pending orders, order history). Education and tooling, not
financial advice.
"""

import datetime
import importlib.util
import json
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FEE = 0.0015                 # Trading 212's FX fee on each conversion (t212.FX_FEE)
REUSE_FOR = 60               # seconds a fresh file is reused instead of asking Trading 212 again


def _load(folder, name):
    spec = importlib.util.spec_from_file_location("day_risk_" + name, os.path.join(ROOT, folder, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _now():
    return time.time()


def path(state_dir):
    return os.path.join(state_dir, "state", "day-risk.json")


def count_of(cfg):
    """"open_risk" or "full_risk" (the default) from "daily_loss_count" in the config."""
    m = str((cfg or {}).get("daily_loss_count") or "full_risk").strip().lower()
    if m not in ("open_risk", "full_risk"):
        raise ValueError("daily_loss_count in trader/config.json must be \"open_risk\" or \"full_risk\"")
    return m


def entries_today(broker_state, today):
    """Today's placed intraday buys (one record per entry), from trader/state/broker.json. Drift buys are
    left out: they have their own risk cap (drift.max_open_risk_pct), not intraday's daily loss limit."""
    out = {}
    for r in (broker_state or {}).get("orders") or []:
        if r.get("status") != "placed" or str(r.get("date") or "") != today:
            continue
        if str(r.get("strategy") or "").strip().lower() == "drift":
            continue
        if (r.get("kind") or ("entry" if str(r.get("side", "")).lower() == "buy" else "")) != "entry":
            continue
        out[r.get("fingerprint")] = r
    return list(out.values())


def stop_now(ticker, pending, guard_state, planned):
    """(stop, where): the live stop order's price, else the guard's software stop, else the plan's."""
    best = None
    for o in pending or []:
        if (o.get("ticker") or (o.get("instrument") or {}).get("ticker")) != ticker:
            continue
        if str(o.get("type", "")).upper() not in ("STOP", "STOP_LIMIT"):
            continue
        if str(o.get("status", "")).upper() in ("REJECTED", "CANCELLED", "CANCELLING"):
            continue
        try:
            px = float(o.get("stopPrice"))
        except (TypeError, ValueError):
            continue
        best = px if best is None else max(best, px)
    if best is not None:
        return best, "stop order"
    soft = ((guard_state or {}).get("software") or {}).get(ticker) or {}
    if soft.get("stop") is not None:
        return float(soft["stop"]), "software stop"
    return planned, "planned stop"


def compute(entries, closed_today, positions, pending, guard_state, rate):
    """Pure: the day-risk document's entries and totals, in US dollars.
    closed_today: report.round_trips results whose buy and sell were both today."""
    held = {}
    for p in positions or []:
        t = p.get("ticker") or (p.get("instrument") or {}).get("ticker")
        try:
            q = float(p.get("quantity") or 0)
        except (TypeError, ValueError):
            q = 0
        if t and q > 0:
            held[t] = {"qty": q, "avg": p.get("averagePricePaid")}
    rows, realised_loss, open_risk = [], 0.0, 0.0
    for rec in entries:
        ticker = rec.get("t212_ticker")
        planned_stop = float(rec["stop"]) if rec.get("stop") is not None else None
        full = float(rec.get("max_loss") or 0)
        trips = [c for c in closed_today if c.get("ticker") == ticker]
        known = [c["pl"] for c in trips if c.get("pl") is not None]
        realised_gbp = sum(known) if known else 0.0
        unknown_pl = len(known) != len(trips)
        h = held.get(ticker)
        row = {"symbol": rec.get("symbol"), "ticker": ticker, "fingerprint": rec.get("fingerprint"),
               "full_risk_usd": round(full, 2), "realised_gbp": round(realised_gbp, 2) if trips else None}
        if not trips and not h:
            row.update(state="not seen yet", counted_usd=round(full, 2),
                       how="not in today's fills or positions yet: its full max loss counts")
            open_risk += full
            rows.append(row)
            continue
        loss = 0.0
        if unknown_pl:
            loss = full                       # a sale whose result Trading 212 did not give: count it all
            row["how"] = "a sale without a known result: its full max loss counts"
        elif realised_gbp < 0:
            loss = -realised_gbp * rate
        orisk = 0.0
        if h:
            try:
                entry = float(h["avg"]) if h.get("avg") is not None else float(rec.get("entry"))
            except (TypeError, ValueError):
                entry = None
            stop, where = stop_now(ticker, pending, guard_state, planned_stop)
            if entry is None or stop is None:
                orisk = full
                row["stop_from"] = "no price or stop: full max loss"
            else:
                orisk = h["qty"] * max(0.0, (entry - stop) + FEE * (entry + stop))
                row.update(stop_now=stop, stop_from=where, held=h["qty"], paid=entry)
            if unknown_pl:
                orisk = 0.0                   # already counted in full above
        realised_loss += loss
        open_risk += orisk
        row.update(state="open" if h and not trips else "part closed" if h else "closed",
                   realised_loss_usd=round(loss, 2), open_risk_usd=round(orisk, 2),
                   counted_usd=round(loss + orisk, 2))
        rows.append(row)
    return {"entries": rows, "realised_loss_usd": round(realised_loss, 2), "open_risk_usd": round(open_risk, 2),
            "counted_usd": round(realised_loss + open_risk, 2)}


def build(client, state_dir, today=None, report_mod=None):
    """Ask Trading 212 (read-only) and write trader/state/day-risk.json. Returns the document.
    A fresh file (under REUSE_FOR seconds, same day) is returned as it is. Raises on any failure:
    then no file is written and trader-risk counts the old way."""
    report_mod = report_mod or _load("trader-broker", "report")
    t212, tk = report_mod.t212, report_mod.tk
    now = _now()
    utc = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).replace(tzinfo=None, microsecond=0)
    ny = tk.to_eastern(utc)
    today = today or ny.date().isoformat()
    p = path(state_dir)
    old = t212._read_json(p, {}) or {}
    if old.get("date") == today and isinstance(old.get("built_at"), (int, float)) and 0 <= now - old["built_at"] < REUSE_FOR:
        return old
    rate = (t212._read_json(os.path.join(state_dir, "fx.json"), {}) or {}).get("usd_per_gbp")
    if not rate:
        raise ValueError("no GBP/USD rate in trader/fx.json")
    rate = float(rate)
    broker = t212._read_json(os.path.join(state_dir, "state", "broker.json"), {}) or {}
    entries = entries_today(broker, today)
    closed = []
    if entries:
        day = datetime.date.fromisoformat(today)
        since = report_mod.alerts.eastern_to_utc(datetime.datetime.combine(day, datetime.time(0, 0)))
        fills = report_mod.history(client, since)
        trips, _ = report_mod.round_trips(fills)
        closed = [c for c in trips if c.get("entry_at") and tk.to_eastern(c["entry_at"]).date().isoformat() == today]
        positions = client.get(t212.POSITIONS) or []
        pending = client.get(t212.ORDERS) or []
    else:
        positions, pending = [], []
    guard = t212._read_json(os.path.join(state_dir, "state", "t212-guard.json"), {}) or {}
    doc = dict(compute(entries, closed, positions, pending, guard, rate), date=today, built_at=now,
               usd_per_gbp=rate, method="open_risk",
               note="losses realised today + risk still open at the stops, in US dollars")
    d = os.path.dirname(p)
    os.makedirs(d, exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, p)
    return doc
