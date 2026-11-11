#!/usr/bin/env python3
"""Cash allocation for the demo autopilot (DEMO ONLY): a cap per trade, soft reservations, a free pool.

    python3 trader-broker/allocation.py show      # today's plan: reservations, free pool, max per trade

The money, from trader/config.json (budget_gbp is the account value, set each trading morning):

    investable     budget_gbp minus the cash buffer (cash_buffer_pct, 10)
    max per trade  budget_gbp x max_position_pct (22): trader-budget already caps every buy at this
    reservable     investable x (100 - free_pool_pct) / 100: the most reservations may ever hold
    free pool      investable minus what is reserved: unplanned trades buy from here

Reservations are a plan, never a buy. The morning briefing earmarks up to max_reservations blocks
of "max per trade" for stocks with a scheduled catalyst (earnings after yesterday's close or before
today's open). A reserved stock is bought only if every normal check passes when it triggers.
A reservation ends:
    used       its stock was bought (the buy may use the reserved cash)
    released   its stock triggered but did not pass the checks, or was skipped
    expired    window_minutes after its start (the start of the entry window, 09:45 New York), or at
               the end of the entry window, whichever is first
Until then its cash is held back from every OTHER buy (t212.free_cash's `reserved`).

max_open_positions (4): no new buy while that many positions are held or waiting to fill.

Settings, all in "allocation" in trader/config.json:
    {"enabled": true, "max_open_positions": 4, "free_pool_pct": 50, "max_reservations": 2,
     "window_minutes": 90}

Demo only: with "paper" not true (or live_enabled) it is off, whatever "enabled" says.
Every decision is logged in trader/state/evidence/allocation.jsonl. The plan is
trader/state/allocation.json. Education and tooling, not financial advice.
"""

import argparse
import datetime
import json
import math
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

DEFAULTS = {"enabled": False, "max_open_positions": 4, "free_pool_pct": 50, "max_reservations": 2,
            "window_minutes": 90}
ENTRY_FROM, ENTRY_TO = "09:45", "15:30"


class AllocationError(Exception):
    pass


def _now():
    return time.time()


# ---------------------------------------------------------------- settings

def settings(cfg):
    """The allocation settings, checked. Raises AllocationError on a bad value."""
    raw = (cfg or {}).get("allocation") or {}
    if not isinstance(raw, dict):
        raise AllocationError("allocation in trader/config.json must be an object")
    s = dict(DEFAULTS)
    s.update({k: v for k, v in raw.items() if k in DEFAULTS})
    if not isinstance(s["enabled"], bool):
        raise AllocationError("allocation.enabled must be true or false")
    for k, lo, hi in (("max_open_positions", 1, 20), ("max_reservations", 0, 10), ("window_minutes", 5, 390)):
        v = s[k]
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            raise AllocationError("allocation.%s must be a whole number from %d to %d" % (k, lo, hi))
    v = s["free_pool_pct"]
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 100:
        raise AllocationError("allocation.free_pool_pct must be a number from 0 to 100")
    s["free_pool_pct"] = float(v)
    return s


def active(cfg):
    """On only when enabled AND on the demo account: "paper" true and no live_enabled."""
    cfg = cfg or {}
    if cfg.get("paper") is not True or cfg.get("live_enabled"):
        return False
    try:
        return settings(cfg)["enabled"]
    except AllocationError:
        return False


def _num(x):
    try:
        return None if x is None or isinstance(x, bool) else float(x)
    except (TypeError, ValueError):
        return None


def figures(cfg):
    """The money in GBP: budget, buffer, investable, max per trade, reservable."""
    s = settings(cfg)
    budget = _num((cfg or {}).get("budget_gbp")) or 0.0
    if budget <= 0:
        raise AllocationError("no budget_gbp in trader/config.json")
    buf = budget * (_num(cfg.get("cash_buffer_pct")) or 0.0) / 100.0
    investable = budget - buf
    cap_pct = _num(cfg.get("max_position_pct")) or 100.0
    per_trade = min(budget * cap_pct / 100.0, investable)
    reservable = investable * (100.0 - s["free_pool_pct"]) / 100.0
    return {"budget_gbp": round(budget, 2), "buffer_gbp": round(buf, 2), "investable_gbp": round(investable, 2),
            "per_trade_gbp": _down(per_trade), "reservable_gbp": round(reservable, 2),
            "max_position_pct": cap_pct, "free_pool_pct": s["free_pool_pct"],
            "max_open_positions": s["max_open_positions"]}


def _down(x):
    return math.floor(x * 100 + 1e-6) / 100.0


def _hm_add(hm, minutes):
    h, m = [int(x) for x in hm.split(":")]
    t = h * 60 + m + int(minutes)
    return "%02d:%02d" % (t // 60, t % 60)


# ---------------------------------------------------------------- files

def path(sd):
    return os.path.join(sd, "state", "allocation.json")


def _log_path(sd):
    d = os.path.join(sd, "state", "evidence")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "allocation.jsonl")


def log(sd, day, event, **fields):
    """One allocation decision: reserved, used, released, expired, skipped, or plan."""
    rec = dict(fields, day=day, event=event, at=_now())
    with open(_log_path(sd), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, sort_keys=True) + "\n")
    return rec


def load_log(sd):
    p = _log_path(sd)
    out = []
    if os.path.exists(p):
        with open(p, encoding="utf-8") as fh:
            for line in fh:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    return out


def _read(p):
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _write(p, doc):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, sort_keys=True)
    os.replace(tmp, p)


def load_plan(sd, day):
    """Today's plan, or None (missing, broken, or another day's)."""
    doc = _read(path(sd))
    if not isinstance(doc, dict) or doc.get("day") != day or not isinstance(doc.get("reservations"), list):
        return None
    return doc


# ---------------------------------------------------------------- the plan

def make_plan(cfg, day, candidates, drift=None, drift_used_gbp=0.0):
    """The day's plan from ranked candidates [{"symbol", "reason"}]: up to max_reservations blocks of
    "max per trade", never more in all than the reservable share. Pure: writes nothing.
    drift: post-earnings drift reservations (trader-broker/drift_buy.py) that go FIRST; with them, and with
    drift_used_gbp (what drift positions already cost), the intraday reservable share is taken from
    what drift does not use: (investable - drift used - drift reserved) x (100 - free_pool_pct) / 100."""
    s = settings(cfg)
    f = figures(cfg)
    start = str(cfg.get("no_entries_before") or ENTRY_FROM)
    last = str(cfg.get("no_entries_after") or ENTRY_TO)
    end = min(_hm_add(start, s["window_minutes"]), last)
    drift = [dict(r) for r in drift or []]
    drift_reserved = sum(r["amount_gbp"] for r in drift)
    reservable = f["reservable_gbp"]
    if drift or drift_used_gbp:
        left = max(f["investable_gbp"] - (drift_used_gbp or 0.0) - drift_reserved, 0.0)
        reservable = min(reservable, left * (100.0 - s["free_pool_pct"]) / 100.0)
    n = min(s["max_reservations"], int((reservable + 0.005) // f["per_trade_gbp"]) if f["per_trade_gbp"] else 0)
    blocked = {str(b).upper() for b in (cfg.get("blocked_tickers") or []) + (cfg.get("blocked") or [])}
    res, seen = list(drift), {r["symbol"] for r in drift}
    for c in candidates or []:
        sym = str(c.get("symbol") or "").upper()
        if len(res) - len(drift) >= n:
            break
        if not sym or sym in seen or sym in blocked:
            continue
        seen.add(sym)
        res.append({"symbol": sym, "reason": c.get("reason") or "scheduled catalyst", "start": start, "end": end,
                    "amount_gbp": f["per_trade_gbp"], "status": "active", "why": None})
    reserved = round(sum(r["amount_gbp"] for r in res), 2)
    return {"day": day, "made_at": _now(), "figures": f, "reservations": res, "reserved_gbp": reserved,
            "free_pool_gbp": round(f["investable_gbp"] - reserved, 2)}


def save_plan(sd, plan):
    """Write the day's plan, unless one exists for that day already (a re-run never resets it).
    Returns the plan in force."""
    old = load_plan(sd, plan["day"])
    if old is not None:
        return old
    _write(path(sd), plan)
    log(sd, plan["day"], "plan", investable_gbp=plan["figures"]["investable_gbp"],
        per_trade_gbp=plan["figures"]["per_trade_gbp"], reserved_gbp=plan["reserved_gbp"],
        free_pool_gbp=plan["free_pool_gbp"])
    for r in plan["reservations"]:
        log(sd, plan["day"], "reserved", symbol=r["symbol"], amount_gbp=r["amount_gbp"], reason=r["reason"],
            start=r["start"], end=r["end"])
    return plan


def refresh(sd, day, now_hm):
    """Expire every active reservation whose window has ended (New York HH:MM). Returns the plan or None."""
    plan = load_plan(sd, day)
    if plan is None:
        return None
    changed = False
    for r in plan["reservations"]:
        if r.get("status") == "active" and now_hm >= r.get("end", "99:99"):
            r["status"], r["why"] = "expired", "its window ended at %s New York without a buy" % r["end"]
            log(sd, day, "expired", symbol=r["symbol"], amount_gbp=r["amount_gbp"], why=r["why"])
            changed = True
    if changed:
        _write(path(sd), plan)
    return plan


def reservation_for(plan, symbol):
    """The active intraday reservation for `symbol`, or None (a drift one is drift_buy.drift_reservation's)."""
    for r in (plan or {}).get("reservations") or []:
        if r.get("symbol") == str(symbol).upper() and r.get("status") == "active" and r.get("pool") != "drift":
            return r
    return None


def reserved_for_others(sd, day, symbol, now_hm):
    """GBP held back from a buy of `symbol`: every active reservation for another stock."""
    plan = refresh(sd, day, now_hm)
    sym = str(symbol or "").upper()
    return round(sum(r["amount_gbp"] for r in (plan or {}).get("reservations") or []
                     if r.get("status") == "active" and r.get("symbol") != sym), 2)


def settle(sd, day, symbol, status, why):
    """End `symbol`'s active reservation: "used" or "released". Returns it, or None if it had none."""
    plan = load_plan(sd, day)
    r = reservation_for(plan, symbol)
    if r is None:
        return None
    r["status"], r["why"] = status, why
    _write(path(sd), plan)
    log(sd, day, status, symbol=r["symbol"], amount_gbp=r["amount_gbp"], why=why)
    return r


def pool_of(plan, symbol):
    """Which cash a buy of `symbol` uses: "reserved:SYM" with an active reservation, else "free"."""
    r = reservation_for(plan, symbol)
    return "reserved:%s" % r["symbol"] if r else "free"


# ---------------------------------------------------------------- words

def _gbp(x):
    return "£{:,.0f}".format(x) if x is not None else "?"


def plan_text(plan):
    """The morning line: max per trade, reservations, free pool, max positions."""
    f = plan["figures"]
    act = [r for r in plan["reservations"] if r.get("status", "active") == "active"]
    res = ", ".join("%s %s (%s, %s to %s NY)" % (_gbp(r["amount_gbp"]), r["symbol"], r["reason"], r["start"], r["end"])
                    for r in act) or "none"
    return ("Allocation (demo): max per trade %s. Reserved: %s. Free pool: %s. Max %d positions at once. "
            "Reservations are a plan, not a buy." % (_gbp(f["per_trade_gbp"]), res, _gbp(plan["free_pool_gbp"]),
                                                     f["max_open_positions"]))


def catalysts(earnings_today, earnings_prev, allowed, ranks, held=()):
    """Ranked candidates: stocks on the list that reported after the previous close or report before
    today's open. ranks: {symbol: average dollars traded a day} (higher first)."""
    held = {str(h).upper() for h in held}
    allow = {str(a).upper() for a in allowed or []}
    out = []
    for rows, when, reason in ((earnings_prev, "after the close", "earnings after yesterday's close"),
                               (earnings_today, "before the open", "earnings before the open")):
        for e in rows or []:
            sym = str(e.get("symbol") or "").upper()
            if e.get("when") == when and sym in allow and sym not in held:
                out.append({"symbol": sym, "reason": reason})
    return sorted(out, key=lambda c: -(ranks.get(c["symbol"]) or 0))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Today's cash allocation plan (demo).")
    ap.add_argument("--state-dir", default=os.path.join(ROOT, "trader"))
    sub = ap.add_subparsers(dest="cmd")
    sub.required = True
    sub.add_parser("show", help="print today's plan")
    a = ap.parse_args(argv)
    plan = _read(path(a.state_dir))
    if not plan:
        print("no allocation plan yet")
        return 0
    print(plan_text(plan))
    for r in plan.get("reservations") or []:
        print("  %s: %s%s" % (r["symbol"], r["status"], (" (%s)" % r["why"]) if r.get("why") else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
