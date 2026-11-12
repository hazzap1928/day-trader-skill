#!/usr/bin/env python3
"""Post-earnings drift, stage 3 of 4: BUYING and managing drift positions on the demo account.
OFF unless "drift": {"buy_enabled": true} (and "paper": true). With it off, nothing here buys.

Each trading morning at drift.buy_at (09:33 New York), before the intraday window opens at 09:45, the
autopilot works through today's stage 2 shortlist (trader/state/drift/shortlist.json, buy_day today):

    skipped, and logged with the reason in the drift signal log, when:
      buy_enabled is off (nothing is logged then), the account is not demo, or /pause is on
      the shortlist is missing, failed or not for today, or the autopilot started after 09:45 ("missed")
      it is on blocked_tickers, or either strategy holds it or is waiting to buy it
      no slot is free (drift.slots), or drift's share of the investable cash is used (max_share_pct)
      there is no cash reservation for it (see below), or the free cash does not cover it
      it is already at or under its stop at the open: yesterday's close minus atr_mult x ATR
      its loss at the stop would take drift's total open risk over max_open_risk_pct of the account

    bought otherwise: a market order through t212.py place (every placing check), labelled "drift" with
    sell_date = the 20th trading day held (the buy day is day 1), and a REAL Trading 212 stop, good till
    cancelled, whatever stop_mode says. The stop is atr_mult (3) x the 14-day ATR under the entry. This
    stop is NOT from the tested research, which held with no stop: it is a setting.

Size: the smaller of one slot (the drift pool / slots) and max_loss_per_trade (1% of the account) at
the stop. The drift pool is max_share_pct (50%) of the investable cash (the account less the 10% buffer).

Cash: drift never buys without a cash reservation in trader/state/allocation.json (allocation.py). The
07:30 briefing reserves one slot per shortlisted stock first; if that plan is missing or has none, the
buy step reserves them itself; if it cannot, that day's drift buys are skipped and logged.

Drift positions do not count toward intraday's max_open_positions, its trades a day, or its daily
loss limit. They are sold at the 15:50 close on their sell date (or the next close the Mac is on,
with an alert), or earlier by their stop. Turning buy_enabled off stops new buys only.

Demo only. Education and tooling, not financial advice.
"""

import argparse
import datetime
import importlib.util
import io
import json
import math
import os
import time
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RESERVE_FROM, RESERVE_TO = "09:30", "10:15"   # a drift reservation's window (drift buys release theirs when done)
LATEST_START = "09:45"                        # a buy step that starts later is "missed": never bought late
SOURCE = "drift_buy"


class DriftBuyError(Exception):
    pass


def _load(folder, name):
    spec = importlib.util.spec_from_file_location("drift_buy_" + name, os.path.join(ROOT, folder, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_MODS = {}


def mod(folder, name):
    if (folder, name) not in _MODS:
        _MODS[(folder, name)] = _load(folder, name)
    return _MODS[(folder, name)]


# the caller (the autopilot, the briefing, the report) sets these to its own instances, so one set of fakes reaches all
t212 = None
allocation = None
drift = None


def _t212():
    global t212
    if t212 is None:
        t212 = mod("trader-broker", "t212")
    return t212


def _alloc():
    global allocation
    if allocation is None:
        allocation = mod("trader-broker", "allocation")
    return allocation


def _drift():
    global drift
    if drift is None:
        drift = mod("trader-broker", "drift")
    return drift


def _now():
    return time.time()


# ---------------------------------------------------------------- settings and money

def settings(cfg):
    return _drift().settings(cfg)


def active(cfg):
    """True only when buy_enabled is true on a demo config ("paper": true, no live_enabled)."""
    cfg = cfg or {}
    if cfg.get("paper") is not True or cfg.get("live_enabled"):
        return False
    try:
        return settings(cfg)["buy_enabled"]
    except Exception:
        return False


def _num(x):
    try:
        return None if x is None or isinstance(x, bool) else float(x)
    except (TypeError, ValueError):
        return None


def _down(x):
    return math.floor(x * 100 + 1e-6) / 100.0


def figures(cfg):
    """GBP: budget (the account value), investable (less the buffer), the drift pool, one slot, the open-risk cap."""
    s = settings(cfg)
    budget = _num((cfg or {}).get("budget_gbp")) or 0.0
    if budget <= 0:
        raise DriftBuyError("no budget_gbp in trader/config.json")
    investable = budget * (1 - (_num(cfg.get("cash_buffer_pct")) or 0.0) / 100.0)
    pool = investable * s["max_share_pct"] / 100.0
    return {"budget_gbp": round(budget, 2), "investable_gbp": round(investable, 2), "pool_gbp": round(pool, 2),
            "slots": s["slots"], "slot_gbp": _down(pool / s["slots"]),
            "risk_cap_gbp": round(budget * s["max_open_risk_pct"] / 100.0, 2)}


# ---------------------------------------------------------------- trading days

def trading_days_between(a, b):
    """Trading days from a to b, both counted (a buy on day a is on day 1)."""
    al = _drift().alerts()
    a, b = _drift()._date(a), _drift()._date(b)
    n, d = 0, a
    while d <= b:
        n += 1 if al.trading_day(d)[0] else 0
        d += datetime.timedelta(days=1)
    return n


def _day(d):
    return _drift()._date(d).strftime("%a %d %b")


# ---------------------------------------------------------------- what drift holds

def holdings(state, positions, pending, env, guard_state, rate, today, cfg=None):
    """Drift positions held, plus drift buys still waiting to fill. One dict each:
    symbol, ticker, qty, avg, price, stop, protected (a real stop order covers it), stop_how, sell_date,
    entry_day, day_n, gain_gbp, cost_gbp, risk_gbp, waiting."""
    T = _t212()
    hold = settings(cfg or {})["hold_days"] if cfg else 20
    soft = (guard_state or {}).get("software") or {}
    rows, seen = [], set()
    for p in positions or []:
        ticker = T._o_ticker(p)
        qty = _num(p.get("quantity")) or 0.0
        if not ticker or qty <= 0:
            continue
        rec = T.position_record(state, ticker, env)
        if not T.is_drift(rec):
            continue
        seen.add(ticker)
        stops, _ = T.guard_sell_orders(pending, ticker)
        covered = sum(abs(_num(o.get("quantity")) or 0) for o in stops)
        live = max([_num(o.get("stopPrice")) or 0 for o in stops] or [0]) or None
        sw = soft.get(ticker) or {}
        if stops and covered + 1e-9 >= qty:
            stop, how, protected = live, "stop order", True
        elif sw.get("stop") is not None:
            stop, how, protected = _num(sw["stop"]), "SOFTWARE STOP: NOT PROTECTED OVERNIGHT", False
        else:
            stop = live or _num(rec.get("stop"))
            how, protected = ("stop covers only part: NOT PROTECTED OVERNIGHT" if stops
                              else "NO STOP ORDER: NOT PROTECTED OVERNIGHT"), False
        avg = _num(p.get("averagePricePaid")) or _num(rec.get("entry"))
        price = _num(p.get("currentPrice"))
        gain = _num((p.get("walletImpact") or {}).get("unrealizedProfitLoss"))
        if gain is None and avg and price and rate:
            gain = qty * (price - avg) / rate
        fee = T.FX_FEE
        risk = qty * max(0.0, (avg - stop) + fee * (avg + stop)) / rate if (avg and stop and rate) else \
            (_num(rec.get("max_loss_gbp")) or 0.0)
        entry_day = str(rec.get("date") or "")[:10]
        rows.append({"symbol": ticker.split("_")[0].upper(), "ticker": ticker, "qty": qty, "avg": avg,
                     "price": price, "stop": stop, "stop_how": how, "protected": protected,
                     "sell_date": rec.get("sell_date"), "entry_day": entry_day,
                     "day_n": trading_days_between(entry_day, today) if entry_day else None, "hold_days": hold,
                     "gain_gbp": round(gain, 2) if gain is not None else None,
                     "cost_gbp": round(qty * avg / rate * (1 + fee), 2) if (avg and rate) else
                     (_num(rec.get("cost_gbp")) or 0.0),
                     "risk_gbp": round(risk, 2), "waiting": False, "fingerprint": rec.get("fingerprint")})
    for o in pending or []:
        ticker = T._o_ticker(o)
        side = str(o.get("side") or "").upper()
        if not ticker or ticker in seen or not (side == "BUY" or (not side and (_num(o.get("quantity")) or 0) > 0)):
            continue
        rec = T.position_record(state, ticker, env)
        if not T.is_drift(rec):
            continue
        seen.add(ticker)
        rows.append({"symbol": ticker.split("_")[0].upper(), "ticker": ticker, "qty": abs(_num(o.get("quantity")) or 0),
                     "avg": None, "price": None, "stop": _num(rec.get("stop")), "stop_how": "buy waiting to fill",
                     "protected": False, "sell_date": rec.get("sell_date"), "entry_day": rec.get("date"),
                     "day_n": 1, "hold_days": hold, "gain_gbp": None, "cost_gbp": _num(rec.get("cost_gbp")) or 0.0,
                     "risk_gbp": _num(rec.get("max_loss_gbp")) or 0.0, "waiting": True,
                     "fingerprint": rec.get("fingerprint")})
    return rows


def _signed(x):
    return "no data" if x is None else ("+" if x >= 0 else "-") + "£{:,.2f}".format(abs(x))


def holding_line(r):
    if r["waiting"]:
        return "%s %s: buy waiting to fill (drift), sells %s" % (
            r["symbol"], _t212()._qty(r["qty"]), _day(r["sell_date"]) if r.get("sell_date") else "?")
    return "%s %s: day %s of %d, %s, stop %s (%s), sells %s" % (
        r["symbol"], _t212()._qty(r["qty"]), r["day_n"] if r["day_n"] is not None else "?", r["hold_days"],
        _signed(r["gain_gbp"]), ("$%.2f" % r["stop"]) if r["stop"] else "none", r["stop_how"],
        _day(r["sell_date"]) if r.get("sell_date") else "? (no sell date: sold at the next close)")


def holding_lines(rows, head="Drift holdings (demo, held up to 20 trading days):"):
    if not rows:
        return []
    return [head] + ["  " + holding_line(r) for r in rows]


def unprotected(rows):
    return [r for r in rows if not r["waiting"] and not r["protected"]]


def read_holdings(client, sd, cfg, rate, today):
    """holdings() from Trading 212 (read-only) and the state files."""
    T = _t212()
    positions = client.get(T.POSITIONS) or []
    pending = client.get(T.ORDERS) or []
    state = T._read_json(os.path.join(sd, "state", "broker.json"), {}) or {}
    guard = T._read_json(os.path.join(sd, "state", "t212-guard.json"), {}) or {}
    return holdings(state, positions, pending, client.env, guard, rate, today, cfg)


def drift_tickers(state, env):
    """Every Trading 212 ticker whose latest buy record is drift: for leaving drift out of intraday's counts."""
    T = _t212()
    latest = {}
    for r in (state or {}).get("orders") or []:
        if (r.get("broker") == "trading212" and r.get("env") == env and r.get("t212_ticker")
                and str(r.get("side", "")).lower() == "buy" and r.get("kind") == "entry" and r.get("status") != "failed"):
            latest[r["t212_ticker"]] = r
    return {t for t, r in latest.items() if T.is_drift(r)}


# ---------------------------------------------------------------- cash reservations (allocation.json)

def drift_reservation(plan, sym, status="active"):
    for r in (plan or {}).get("reservations") or []:
        if r.get("pool") == "drift" and r.get("symbol") == str(sym).upper() and (status is None or r.get("status") == status):
            return r
    return None


def plan_reservations(cfg, symbols, used_gbp, held_n):
    """The drift reservations for a morning plan: one slot per symbol, best first, while a slot is free and
    the drift pool has room. Pure. Returns ([reservation], skipped [(symbol, why)])."""
    f = figures(cfg)
    room = f["pool_gbp"] - used_gbp
    free = f["slots"] - held_n
    out, skipped = [], []
    for sym in symbols:
        sym = str(sym).upper()
        if free <= 0:
            skipped.append((sym, "no free drift slot (%d of %d in use)" % (f["slots"] - free, f["slots"])))
            continue
        if room + 0.005 < f["slot_gbp"]:
            skipped.append((sym, "the drift pool is used: GBP %.2f left of GBP %.2f (max_share_pct)"
                            % (max(room, 0), f["pool_gbp"])))
            continue
        out.append({"symbol": sym, "reason": "drift buy", "start": RESERVE_FROM, "end": RESERVE_TO,
                    "amount_gbp": f["slot_gbp"], "status": "active", "why": None, "pool": "drift"})
        room -= f["slot_gbp"]
        free -= 1
    return out, skipped


def ensure_reservations(sd, cfg, day, symbols, used_gbp, held_n):
    """Make sure each of `symbols` (best first) has an active drift reservation in today's allocation plan,
    adding them (or a drift-only plan) when the 07:30 plan is missing or has none.
    Returns (plan, {symbol: why not reserved}, error). error set: no plan could be used; buy nothing."""
    A = _alloc()
    if not A.active(cfg):
        return None, {}, "the cash allocation (allocation.enabled, demo) is off, so there is no reservation"
    try:
        plan = A.load_plan(sd, day)
        made = False
        if plan is None:
            plan = {"day": day, "made_at": _now(), "figures": A.figures(cfg), "reservations": [], "reserved_gbp": 0.0,
                    "free_pool_gbp": A.figures(cfg)["investable_gbp"], "made_by": "drift buy step (no morning plan)"}
            made = True
        have = [s for s in symbols if drift_reservation(plan, s)]
        others_used = sum(r["amount_gbp"] for r in plan["reservations"]
                          if r.get("pool") == "drift" and r.get("status") == "active")
        want = [s for s in symbols if s not in have]
        new, skipped = plan_reservations(cfg, want, used_gbp + others_used, held_n + len(have))
        for r in new:
            plan["reservations"].append(r)
            A.log(sd, day, "reserved", symbol=r["symbol"], amount_gbp=r["amount_gbp"], reason=r["reason"],
                  start=r["start"], end=r["end"], pool="drift", by="drift buy step")
        if new or made:
            plan["reserved_gbp"] = round(sum(r["amount_gbp"] for r in plan["reservations"] if r.get("status") == "active"), 2)
            A._write(A.path(sd), plan)
            if made:
                A.log(sd, day, "plan", investable_gbp=plan["figures"]["investable_gbp"], reserved_gbp=plan["reserved_gbp"],
                      by="drift buy step: the 07:30 plan was missing")
        check = A.load_plan(sd, day)
        if check is None:
            return None, {}, "the allocation plan could not be read back after writing it"
        return check, dict(skipped), None
    except Exception as e:
        return None, {}, "the cash reservation could not be made (%s)" % str(e)[:120]


def settle(sd, day, sym, status, why):
    """End the drift reservation for `sym` ("used" or "released")."""
    A = _alloc()
    plan = A.load_plan(sd, day)
    r = drift_reservation(plan, sym)
    if r is None:
        return None
    r["status"], r["why"] = status, why
    A._write(A.path(sd), plan)
    A.log(sd, day, status, symbol=r["symbol"], amount_gbp=r["amount_gbp"], why=why, pool="drift")
    return r


def release_all(sd, day, why):
    A = _alloc()
    plan = A.load_plan(sd, day)
    for r in (plan or {}).get("reservations") or []:
        if r.get("pool") == "drift" and r.get("status") == "active":
            settle(sd, day, r["symbol"], "released", why)


# ---------------------------------------------------------------- the signal log

def log_buy(sd, sym, day, **fields):
    """The buy decision on the drift signal log row (shortlisted for today), and in trader/state/drift/buys.jsonl."""
    D = _drift()
    rec = dict(fields, symbol=sym, day=str(day), at=_now())
    rows = D.load_signals(sd)
    for r in rows:
        if D.key(r.get("symbol")) == D.key(sym) and r.get("entry_day") == str(day):
            r["buy"] = rec
    if rows:
        D.save_signals(sd, rows)
    d = os.path.join(sd, "state", "drift")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "buys.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, sort_keys=True) + "\n")
    return rec


def load_jsonl(path):
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


# ---------------------------------------------------------------- the morning buy

def _stop_from(bars, day, s, quote):
    """(stop, planned stop from yesterday's close, ATR) or raises DriftBuyError."""
    D = _drift()
    a = D.atr(bars, day, s["atr_days"])
    if not a:
        raise DriftBuyError("not enough daily bars for a %d-day ATR" % s["atr_days"])
    before = [d for d in sorted(bars) if d < D._date(day)]
    prev_close = bars[before[-1]][3]
    return round(quote - s["atr_mult"] * a, 2), round(prev_close - s["atr_mult"] * a, 2), a


def buy_today(client, sd, cfg, ny, rate, quote, paused, say, push, place=None):
    """The morning drift buys. Returns {"status", "bought": [...], "skipped": [(sym, why)]}.
    quote(sym) -> a fresh price or None. place(args, confirm) -> t212.cmd_place's result."""
    T = _t212()
    out = {"status": "off", "bought": [], "skipped": []}
    if not active(cfg):
        return out
    if client.env != "demo":
        out["status"] = "refused: drift buying is demo only"
        return out
    s = settings(cfg)
    day = ny.date()
    dayi = day.isoformat()
    D = _drift()
    sl = D.read_shortlist(sd)
    if not sl or sl.get("buy_day") != dayi or sl.get("error"):
        out["status"] = "no shortlist for today (%s)" % ("none" if not sl else sl.get("error") or "it is for %s" % sl.get("buy_day"))
        say("drift buy: " + out["status"])
        return out
    names = [x["symbol"] for x in sl.get("shortlist") or []]

    def skip(sym, why, **kw):
        out["skipped"].append((sym, why))
        log_buy(sd, sym, day, status="skipped", why=why, **kw)

    if not names:
        out["status"] = "nothing on today's shortlist"
        return out
    if ny.strftime("%H:%M") >= LATEST_START:
        for sym in names:
            skip(sym, "missed: the buy step did not run before %s New York (the Mac or the autopilot was off); "
                      "the rule buys at the open, so it is never bought late" % LATEST_START)
        out["status"] = "missed"
        say("DRIFT (demo): today's buys were MISSED (%s): the autopilot was not running at %s New York. "
            "Nothing was bought late." % (", ".join(names), s["buy_at"]))
        push(_msgs(sd).drift_missed(s["buy_at"]))
        return out
    if paused:
        for sym in names:
            skip(sym, "paused from your phone (/pause)")
        out["status"] = "paused"
        return out
    try:
        positions = client.get(T.POSITIONS) or []
        pending = client.get(T.ORDERS) or []
    except T.T212Error as e:
        for sym in names:
            skip(sym, "could not read the account (%s)" % T.scrub(e, client._secrets))
        out["status"] = "no account data"
        return out
    state = T._read_json(os.path.join(sd, "state", "broker.json"), {}) or {}
    guard = T._read_json(os.path.join(sd, "state", "t212-guard.json"), {}) or {}
    rows = holdings(state, positions, pending, client.env, guard, rate, day, cfg)
    try:
        f = figures(cfg)
    except DriftBuyError as e:
        for sym in names:
            skip(sym, str(e))
        out["status"] = "no budget"
        return out
    blocked = {D.key(b) for b in T.blocked_tickers(cfg)}
    busy = {D.key(T._o_ticker(p).split("_")[0]) for p in positions if (_num(p.get("quantity")) or 0) > 0 and T._o_ticker(p)}
    busy |= {D.key(T._o_ticker(o).split("_")[0]) for o in pending if T._o_ticker(o)
             and (str(o.get("side") or "").upper() == "BUY" or (not o.get("side") and (_num(o.get("quantity")) or 0) > 0))}
    ok = []
    for sym in names:
        if D.key(sym) in blocked:
            skip(sym, "on blocked_tickers")
        elif D.key(sym) in busy:
            skip(sym, "already held or waiting to fill (intraday or drift)")
        else:
            ok.append(sym)
    if not ok:
        out["status"] = "nothing left to buy"
        return out
    used = sum(r["cost_gbp"] for r in rows)
    plan, not_reserved, err = ensure_reservations(sd, cfg, dayi, ok, used, len(rows))
    if err:
        for sym in ok:
            skip(sym, "no cash reservation, so nothing is bought: " + err)
        out["status"] = "no reservation"
        say("DRIFT (demo): no drift buys today: %s. Logged; nothing was bought." % err)
        push(_msgs(sd).drift_none(err))
        return out
    risk_now = sum(r["risk_gbp"] for r in rows)
    fractional = bool(cfg.get("fractional"))
    step = 0.01 if fractional else 1
    for sym in ok:
        if sym in not_reserved or not drift_reservation(plan, sym):
            skip(sym, not_reserved.get(sym) or "no cash reservation for it")
            continue
        try:
            px = quote(sym)
            if not px:
                raise DriftBuyError("no fresh quote")
            bars = D.daily_bars(sym, day - datetime.timedelta(days=45), day)
            stop, planned, atr = _stop_from(bars, day, s, float(px))
        except Exception as e:
            settle(sd, dayi, sym, "released", "no price or ATR")
            skip(sym, "no price or ATR (%s)" % str(e)[:100])
            continue
        px = float(px)
        if px <= planned:
            settle(sd, dayi, sym, "released", "below its stop at the open")
            skip(sym, "already at or under its stop at the open: $%.2f against yesterday's close minus %gxATR, $%.2f"
                 % (px, s["atr_mult"], planned), price=px, planned_stop=planned)
            continue
        if stop <= 0:
            settle(sd, dayi, sym, "released", "no usable stop")
            skip(sym, "the %gxATR stop is not above zero" % s["atr_mult"])
            continue
        slot_qty = math.floor(f["slot_gbp"] * rate / (px * (1 + T.FX_FEE)) / step + 1e-9) * step
        slot_qty = round(slot_qty, 2) if fractional else int(slot_qty)
        try:
            if slot_qty <= 0:
                raise T.T212Error("one slot (GBP %.2f) does not buy a single share at $%.2f" % (f["slot_gbp"], px), 2)
            sz = T.size_entry(px, stop, slot_qty, rate, 1e12, cfg, fractional)
        except T.T212Error as e:
            settle(sd, dayi, sym, "released", "does not size")
            skip(sym, "does not size: %s" % e)
            continue
        if risk_now + sz["max_loss_gbp"] > f["risk_cap_gbp"] + 0.005:
            settle(sd, dayi, sym, "released", "open-risk cap")
            skip(sym, "drift's open risk would be GBP %.2f + GBP %.2f, over the GBP %.2f cap (max_open_risk_pct)"
                 % (risk_now, sz["max_loss_gbp"], f["risk_cap_gbp"]))
            continue
        try:
            sell_date = T.drift_sell_date(day, s["hold_days"] - 1)
        except T.T212Error as e:
            settle(sd, dayi, sym, "released", "no sell date")
            skip(sym, "no sell date: %s" % e)
            continue
        res, why = place_one(client, sd, cfg, sym, sz["qty"], px, stop, sell_date, dayi, place)
        if res is None:
            settle(sd, dayi, sym, "released", why[:80])
            skip(sym, why)
            continue
        settle(sd, dayi, sym, "used", "bought (drift)")
        risk_now += sz["max_loss_gbp"]
        info = {"symbol": sym, "qty": sz["qty"], "price": px, "stop": stop, "sell_date": sell_date,
                "cost_gbp": sz["cost_gbp"], "max_loss_gbp": sz["max_loss_gbp"], "atr": round(atr, 4),
                "order_id": T._ident((res.get("entry") or {}).get("id")), "stop_ok": res.get("stop_ok", True),
                "note": res.get("note")}
        out["bought"].append(info)
        log_buy(sd, sym, day, status="bought", **{k: v for k, v in info.items() if k != "symbol"})
        say(buy_text(info, s))
        push(_msgs(sd).drift_bought(info))
    release_all(sd, dayi, "the drift buy step is done")
    out["status"] = "done"
    return out


def _msgs(sd):
    """trader-broker/messages.py: the plain-English phone messages."""
    m = _t212().phone_module().messages()
    m.use(sd)
    return m


def buy_text(i, s):
    return ("DRIFT BUY (demo): %s %s at about $%.2f, cost about £%.2f with the FX fee. Stop $%.2f (%gxATR, good till "
            "cancelled%s). Sell date %s (the close of day %d held). Max loss at the stop about £%.2f.%s" % (
                _t212()._qty(i["qty"]), i["symbol"], i["price"], i["cost_gbp"], i["stop"], s["atr_mult"],
                "" if i.get("stop_ok", True) else ": !!! NOT IN PLACE, see the guard alert !!!", _day(i["sell_date"]),
                s["hold_days"], i["max_loss_gbp"], (" " + i["note"]) if i.get("note") else ""))


def place_one(client, sd, cfg, sym, qty, px, stop, sell_date, day, place=None):
    """One drift buy through t212.cmd_place (every placing check, the cash check against the reservations
    for other stocks). Returns (result, None) or (None, why)."""
    T = _t212()
    tk = T.ticket_module()
    d = os.path.join(sd, "state", "drift")
    os.makedirs(d, exist_ok=True)
    order = {"symbol": sym, "side": "buy", "qty": str(qty), "type": "market", "time_in_force": "day",
             "stop_loss_stop_price": stop, "plan": {"entry": px, "stop": stop, "strategy": "drift",
                                                    "sell_date": sell_date}}
    fp = tk.fingerprint(order)
    T._write_json(os.path.join(d, "orders.json"), {"orders": [order]})
    T._write_json(os.path.join(d, "risk.json"), {"date": day, "results": [
        {"index": 1, "fingerprint": fp, "verdict": "PASS", "kind": "entry", "reasons": [],
         "by": "trader-broker/drift_buy.py (drift's own limits; intraday's trader-risk does not apply)"}]})
    reserved = _alloc().reserved_for_others(sd, day, sym, "09:30")

    def confirm(index, o, sz):
        if tk.fingerprint(o) != fp:
            return False, "", "not the drift order that passed drift's checks"
        return True, "drift autopilot (demo) %s" % T._utc_now().strftime("%H:%M:%SZ"), ""

    args = argparse.Namespace(ticket_id=fp, resend=False, fx_rate=None, state_dir=sd, json=False, live=False,
                              cash_check=True, reserved_gbp=reserved, strategy="drift", sell_date=sell_date,
                              orders_path=os.path.join(d, "orders.json"), risk_path=os.path.join(d, "risk.json"))
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            res = (place or T.cmd_place)(client, args, confirm=confirm)
    except T.NoCash as e:
        return None, "no free cash: %s" % str(e).replace("NOT PLACED: no free cash: ", "")
    except T.T212Error as e:
        if getattr(e, "code", None) == 3 or str(e).startswith("ENTRY PLACED"):
            note = T.scrub(str(e), client._secrets)[:300]
            # bought, but its stop is not in: loud now; the guard puts it back, or holds it in software
            # (a market sell at the stop, market hours only) and alarms again if Trading 212 refuses it
            T.guard_alarm(client, "DRIFT %s: bought, but its stop at $%.2f is NOT IN PLACE at Trading 212, so it is "
                                  "NOT PROTECTED OVERNIGHT until it is. %s" % (sym, stop, note))
            return {"entry": {}, "stop_ok": False, "note": note}, None
        return None, "not placed: %s" % T.scrub(str(e), client._secrets)[:300]
    if res is None:
        return None, "place did not send it"
    res["stop_ok"] = bool(res.get("stop"))
    return res, None


# ---------------------------------------------------------------- sales: DRIFT SELL

def trades_path(sd):
    return os.path.join(sd, "state", "drift", "trades.jsonl")


def closed_since(client, sd, cfg, today, report_mod, rate=None, spy=None):
    """Drift positions sold since they were bought and not reported yet: one DRIFT SELL text each, and a row in
    trader/state/drift/trades.jsonl. Read-only at Trading 212 (positions and the order history)."""
    T = _t212()
    D = _drift()
    s = settings(cfg)
    state = T._read_json(os.path.join(sd, "state", "broker.json"), {}) or {}
    done = {r.get("fingerprint") for r in load_jsonl(trades_path(sd))}
    latest = {}
    for r in state.get("orders") or []:
        if (r.get("env") == client.env and str(r.get("side", "")).lower() == "buy" and r.get("kind") == "entry"
                and T.is_drift(r) and r.get("status") in T.GUARD_STATUSES + ("placed",)):
            latest[r.get("fingerprint")] = r
    todo = [r for fp, r in latest.items() if fp not in done]
    if not todo:
        return []
    held = {T._o_ticker(p) for p in client.get(T.POSITIONS) or [] if (_num(p.get("quantity")) or 0) > 0}
    todo = [r for r in todo if r.get("t212_ticker") not in held]
    if not todo:
        return []
    first = min(D._date(r["date"]) for r in todo)
    since = report_mod.alerts.eastern_to_utc(datetime.datetime.combine(first, datetime.time(0, 0)))
    fills = report_mod.history(client, since)
    trips, _ = report_mod.round_trips(fills)
    guard = T._read_json(os.path.join(sd, "state", "t212-guard.json"), {}) or {}
    tk = T.ticket_module()
    out = []
    for r in todo:
        mine = [c for c in trips if c["ticker"] == r["t212_ticker"] and c.get("entry_at")
                and tk.to_eastern(c["entry_at"]).date().isoformat() >= r["date"]]
        if not mine:
            continue                                   # not sold yet (a buy that never filled stays quiet)
        exit_at = max(c["exit_at"] for c in mine)
        exit_day = tk.to_eastern(exit_at).date()
        pl = None if any(c.get("pl") is None for c in mine) else sum(c["pl"] for c in mine)
        cost = None if any(c.get("cost_gbp") is None for c in mine) else sum(c["cost_gbp"] for c in mine)
        sd_date = D._date(r["sell_date"]) if r.get("sell_date") else None
        eod = guard.get("eod") or {}
        by_close = (eod.get("date") == exit_day.isoformat()
                    and any(x.get("ticker") == r["t212_ticker"] for x in eod.get("sold") or []))
        if mine[-1]["sell"].get("from") and mine[-1]["sell"]["from"] != "API":
            reason = "sold in the Trading 212 app"
        elif by_close and sd_date and exit_day > sd_date:
            reason = "sell date %s, sold late (overdue)" % sd_date.isoformat()
        elif by_close:
            reason = "sell date"
        else:
            reason = "stop"
        days = trading_days_between(r["date"], exit_day)
        ret = (pl / cost) if (pl is not None and cost) else None
        spy_ret = None
        try:
            bars = spy if spy is not None else D.daily_bars(s["tracker"], D._date(r["date"]) - datetime.timedelta(days=5),
                                                            exit_day)
            e, x = D._date(r["date"]), exit_day
            if e in bars and x in bars:
                spy_ret = D.net_return(bars[e][0], bars[x][3], s["tracker_cost"])
        except Exception:
            spy_ret = None
        row = {"fingerprint": r.get("fingerprint"), "symbol": r.get("symbol"), "ticker": r["t212_ticker"],
               "buy_day": r["date"], "exit_day": exit_day.isoformat(), "sell_date": r.get("sell_date"),
               "reason": reason, "days": days, "pl_gbp": round(pl, 2) if pl is not None else None,
               "cost_gbp": round(cost, 2) if cost is not None else None,
               "ret_pct": round(100 * ret, 3) if ret is not None else None,
               "spy_pct": round(100 * spy_ret, 3) if spy_ret is not None else None,
               "vs_spy_pct": round(100 * (ret - spy_ret), 3) if (ret is not None and spy_ret is not None) else None,
               "logged_at": _now()}
        d = os.path.join(sd, "state", "drift")
        os.makedirs(d, exist_ok=True)
        with open(trades_path(sd), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
        out.append((row, sell_text(row, s)))
    return out


def sell_text(row, s):
    if row["pl_gbp"] is None:
        res = "result not known yet (Trading 212 gave no fees for a fill)"
    else:
        res = "%s after costs (%+.2f%%)" % (_signed(row["pl_gbp"]), row["ret_pct"] or 0)
    spy = ("; SPY %+.2f%% over the same days, so %+.2f points %s" % (
        row["spy_pct"], row["vs_spy_pct"], "ahead" if row["vs_spy_pct"] >= 0 else "behind")
        if row.get("vs_spy_pct") is not None else "; SPY: no data")
    return "DRIFT SELL (demo): %s, %s. %s%s. Held %d trading day%s (buy day %s, sold %s)." % (
        row["symbol"], row["reason"], res, spy, row["days"], "" if row["days"] == 1 else "s",
        _day(row["buy_day"]), _day(row["exit_day"]))


# ---------------------------------------------------------------- the Friday report

def friday_lines(sd, cfg, today=None):
    """The drift section of the Friday evidence report: real trades and shadow signals on separate lines,
    with drift's own running rule. None when neither the scan nor buying is on."""
    s = settings(cfg)
    if not (s["scan_enabled"] or s["buy_enabled"]):
        return None
    real = load_jsonl(trades_path(sd))
    shadow = [r for r in _drift().load_signals(sd) if (r.get("results") or {}).get(str(s["hold_days"]), {}).get("ret_net_pct") is not None]
    L = ["", "## Post-earnings drift (demo)",
         "Pass rule: a positive average per closed trade after costs AND beating SPY over the same holding periods "
         "(average vs SPY above 0). Under %d closed trades it is too few trades to judge." % s["min_trades"]]
    L.append("- " + _judge_line("Real drift trades", [r["ret_pct"] for r in real if r.get("ret_pct") is not None],
                                [r["vs_spy_pct"] for r in real if r.get("vs_spy_pct") is not None], s["min_trades"],
                                extra="buying is %s" % ("ON" if active(cfg) else "OFF")))
    h = str(s["hold_days"])
    L.append("- " + _judge_line("Shadow signals (%s-day virtual results, every qualifying signal)" % h,
                                [r["results"][h]["ret_net_pct"] for r in shadow],
                                [r["results"][h]["vs_tracker_pct"] for r in shadow], s["min_trades"]))
    return L


def friday_phone(sd, cfg, today=None):
    """The drift part of the Friday phone message (messages.friday_drift), or None when drift is off."""
    s = settings(cfg)
    if not (s["scan_enabled"] or s["buy_enabled"]):
        return None
    real = load_jsonl(trades_path(sd))
    h = str(s["hold_days"])
    shadow = [r for r in _drift().load_signals(sd) if (r.get("results") or {}).get(h, {}).get("ret_net_pct") is not None]

    def judge(rets, vs):
        n = len(rets)
        if not n:
            return None
        avg, avg_vs = sum(rets) / n, (sum(vs) / len(vs) if vs else None)
        ok = avg > 0 and avg_vs is not None and avg_vs > 0
        status = ("on track" if ok else "not on track") if n >= s["min_trades"] else \
            "too few to judge yet (%d of %d)" % (n, s["min_trades"])
        return {"n": n, "avg": avg, "vs": avg_vs, "status": status}
    return _msgs(sd).friday_drift(
        judge([r["ret_pct"] for r in real if r.get("ret_pct") is not None],
              [r["vs_spy_pct"] for r in real if r.get("vs_spy_pct") is not None]),
        judge([r["results"][h]["ret_net_pct"] for r in shadow], [r["results"][h]["vs_tracker_pct"] for r in shadow]))


def _judge_line(label, rets, vs, min_n, extra=None):
    label = "%s (%s)" % (label, extra) if extra else label
    n = len(rets)
    if not n:
        return "%s: none closed yet: too few trades to judge." % label
    avg, avg_vs = sum(rets) / n, (sum(vs) / len(vs) if vs else None)
    ok = avg > 0 and avg_vs is not None and avg_vs > 0
    lean = "ON TRACK" if ok else "NOT ON TRACK"
    status = lean if n >= min_n else "too few trades to judge (%d of %d; reads %s so far)" % (n, min_n, lean)
    return "%s: %d closed, average %+.2f%% after costs, average vs SPY %s: %s." % (
        label, n, avg, "no data" if avg_vs is None else "%+.2f points" % avg_vs, status)
