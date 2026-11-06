#!/usr/bin/env python3
"""Position sizing for trader-budget. Standard library only, no network.

Default mode: sizes ONE day trade from trader/plan.json (written by
trader-exit) to your budget and your max loss per trade, then writes one
bracket order (entry, stop-loss, take-profit) to trader/orders.json in the
shape of Alpaca's place_stock_order. It never places anything: trader-risk
checks the order and trader-broker asks you yes or no.

    python3 size.py                                  # trader/plan.json + trader/config.json
    python3 size.py --write                          # write trader/orders.json
    python3 size.py --limit                          # a limit order at the plan's entry
    python3 size.py --limit 364.00 --write           # a limit order at 364.00
    python3 size.py --now "2026-10-19 10:15" --json  # pin the clock (New York time)

How many shares. Whole shares only: Alpaca takes no fractions in a bracket.

    shares = floor of the smallest of
      max loss       max_loss_per_trade_usd / risk per share (entry minus stop)
      position cap   budget x max_position_pct / entry
      max order      max_order_usd / entry
      cash           (budget minus the cash buffer) / entry
                     (and no more than the cash in trader/portfolio.json, if you have one)

The entry it sizes at: a limit order's limit price; for a market order the
plan's entry, or the price in trader/prices.json if that is fresh (under 15
minutes old) and higher, because a price that ran up since the plan means a
bigger loss at the same stop. Zero shares (the stop is too far away for your
max loss) is a plain refusal: no order is written. So is a reward:risk under
min_reward_risk (the price ran toward the target since the plan).

Weights mode (kept from v1, no longer the headline): split a budget across a
list of stocks by weight.

    python3 size.py --weights AAPL=22.04,AXP=17.14,GOOGL=12.62,KO=10.86,BAC=9.20
    python3 size.py --targets my-weights.json --budget 5000 --write
    python3 size.py --weights ... --whole-shares --prices trader/prices.json

  1. cash buffer   budget x cash_buffer_pct stays in cash, never invested
  2. weights       renormalised to 100 over the stocks you keep (blocked
                   tickers are dropped first; --top N keeps the N biggest)
  3. cap           no stock gets more than max_position_pct of the budget;
                   anything over the cap is shared out to the others by
                   weight, again and again until nobody is over
  4. min order     a slice under min_order_usd is dropped and its weight
                   shared out, so you never place a $2 order
  5. rounding      whole dollars by default (--cents for cents); the rounding
                   dollar goes to the biggest position that is not capped
  6. whole shares  with --whole-shares or "fractional": false; needs prices

Money that cannot be invested is reported as "left idle", never hidden.
"""

import argparse
import datetime
import json
import math
import os
import sys

# v2 defaults: the contract's config.example.json. Missing keys fall back to these.
DEFAULTS = {
    "budget": None,
    "cash_buffer_pct": 10,
    "max_position_pct": 75,
    "max_order_usd": 4000,
    "min_order_usd": 5,
    "max_loss_per_trade_usd": 50,
    "max_daily_loss_usd": 150,
    "max_trades_per_day": 3,
    "max_orders_per_day": 10,
    "min_reward_risk": 1.0,
    "require_stop": True,
    "no_entries_before": "09:45",
    "no_entries_after": "15:30",
    "flat_by": "15:55",
    "fractional": False,
    "broker": "alpaca",
    "paper": True,
    "blocked": [],
    "allow_options": False,
    "allow_margin": False,
    "allow_short": False,
    "pdt_guard": False,
}
EPS = 1e-9
FRESH_MINUTES = 15
SETUP_WORDS = {"orb": "opening-range breakout", "flag": "flag", "bounce": "oversold bounce"}


# ---------------------------------------------------------------- money: USD or GBP config
# The same block is in trader-budget/size.py, trader-risk/check.py and
# trader-broker/ticket.py (tests/test_currency.py keeps them identical).
# Prices are in US dollars. A GBP config ("currency": "GBP") keeps its money
# limits in pounds under *_gbp names; they are converted to dollars at the
# GBP/USD rate in trader/fx.json (python3 trader-broker/t212.py fx) or --fx-rate.
# A GBP config with a dollar name in it (budget, *_usd) is refused, so a pound
# figure is never read as dollars.

GBP_KEYS = (("budget_gbp", "budget"), ("max_order_gbp", "max_order_usd"),
            ("min_order_gbp", "min_order_usd"), ("max_loss_per_trade_gbp", "max_loss_per_trade_usd"),
            ("max_daily_loss_gbp", "max_daily_loss_usd"))
FX_PATH = "trader/fx.json"
FX_MAX_HOURS = 12


class CurrencyError(Exception):
    pass


def config_currency(raw):
    """'USD' or 'GBP' for a config as written in the file. Refuses a mixed one."""
    raw = raw or {}
    cur = str(raw.get("currency") or "USD").strip().upper()
    if cur not in ("USD", "GBP"):
        raise CurrencyError("currency %r in trader/config.json: only USD or GBP" % raw.get("currency"))
    gbp = sorted(g for g, _ in GBP_KEYS if g in raw)
    usd = sorted(u for _, u in GBP_KEYS if u in raw)
    if cur == "USD" and gbp:
        raise CurrencyError("trader/config.json has %s but no \"currency\": \"GBP\"; add it so the pound "
                            "limits are read as pounds" % ", ".join(gbp))
    if cur == "GBP" and usd:
        raise CurrencyError("trader/config.json says \"currency\": \"GBP\" but has %s, a dollar name; use %s "
                            "(in pounds) instead" % (", ".join(usd), ", ".join(
                                g for g, u in GBP_KEYS if u in usd)))
    return cur


def _utc_text(s):
    import datetime as _dt
    try:
        return _dt.datetime.strptime(str(s).strip(), "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None


def read_fx(path, now_utc):
    """(USD per GBP, where it came from) from trader/fx.json, if it is fresh."""
    import datetime as _dt
    import json as _json
    try:
        with open(path, encoding="utf-8") as fh:
            doc = _json.load(fh)
    except FileNotFoundError:
        raise CurrencyError("your limits are in GBP and there is no GBP/USD rate at %s: run "
                            "python3 trader-broker/t212.py fx (or pass --fx-rate 1.31)" % path)
    except ValueError as e:
        raise CurrencyError("%s is not valid JSON: %s" % (path, e))
    try:
        rate = float(doc.get("usd_per_gbp"))
    except (TypeError, ValueError, AttributeError):
        raise CurrencyError("%s has no usd_per_gbp number" % path)
    at = _utc_text(doc.get("fetched_at"))
    if at is None:
        raise CurrencyError("%s has no fetched_at time" % path)
    now_utc = now_utc or _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    if now_utc - at > _dt.timedelta(hours=FX_MAX_HOURS) or at - now_utc > _dt.timedelta(minutes=5):
        raise CurrencyError("the GBP/USD rate in %s is from %s UTC, more than %d hours old: run "
                            "python3 trader-broker/t212.py fx" % (path, doc.get("fetched_at"), FX_MAX_HOURS))
    return rate, "%s, fetched %s UTC" % (doc.get("source") or path, doc.get("fetched_at"))


def money_config(raw, fx_rate=None, fx_path=FX_PATH, now_utc=None):
    """The config with every money limit in dollars, plus how it got there.

    USD: returned as it is. GBP: each *_gbp limit becomes its dollar name at the
    rate (limits rounded down to the cent, the minimum order rounded up), a
    missing one becomes None so no dollar default fills the gap, and "money"
    says what was converted."""
    import math as _m
    raw = dict(raw or {})
    cur = config_currency(raw)
    if cur == "USD":
        raw["currency"] = "USD"
        raw.setdefault("money", {"currency": "USD"})
        return raw
    if fx_rate is not None:
        rate, src = float(fx_rate), "--fx-rate"
    else:
        rate, src = read_fx(fx_path, now_utc)
    if not 0.5 < rate < 3.0:
        raise CurrencyError("a GBP/USD rate of %r looks wrong (USD per GBP, e.g. 1.31)" % rate)
    out = dict(raw, currency="GBP")
    conv = {}
    for g, u in GBP_KEYS:
        v = raw.get(g)
        if v is None:
            out[u] = None
            continue
        try:
            gbp = float(v)
        except (TypeError, ValueError):
            raise CurrencyError("%s must be a number of pounds, got %r" % (g, v))
        usd = gbp * rate
        out[u] = (_m.ceil(usd * 100 - 1e-6) if u == "min_order_usd" else _m.floor(usd * 100 + 1e-6)) / 100.0
        conv[u] = {"gbp": gbp, "usd": out[u], "from": g}
    out["money"] = {"currency": "GBP", "usd_per_gbp": rate, "rate_from": src, "converted": conv}
    return out


def gbp_label(c, usd_key):
    """'$39.30 (GBP 30)' for a converted limit, '$50' for a dollar one."""
    v = c.get(usd_key)
    if v is None:
        return "none"
    s = "{:,.2f}".format(float(v))
    s = "$" + (s[:-3] if s.endswith(".00") else s)
    conv = ((c.get("money") or {}).get("converted") or {}).get(usd_key)
    if conv:
        g = "{:,.2f}".format(conv["gbp"])
        s += " (GBP %s)" % (g[:-3] if g.endswith(".00") else g)
    return s


def money_line(c):
    """One line for a report, or '' for a dollar config."""
    m = c.get("money") or {}
    if m.get("currency") != "GBP":
        return ""
    parts = ["%s %s" % (v["from"].replace("_gbp", "").replace("_", " "), gbp_label(c, k))
             for k, v in m.get("converted", {}).items()]
    return ("Your limits are in GBP, at %.4f USD per GBP (%s): %s."
            % (m["usd_per_gbp"], m["rate_from"], "; ".join(parts)))


class SizeError(Exception):
    pass


# ---------------------------------------------------------------- time (New York, no tz files)

def _nth_sunday(year, month, n):
    d = datetime.date(year, month, 1)
    d += datetime.timedelta(days=(6 - d.weekday()) % 7)
    return d + datetime.timedelta(weeks=n - 1)


def eastern_offset(utc):
    """Hours from UTC for New York: -4 in daylight time, -5 otherwise.
    DST runs from 2:00 a.m. on the second Sunday of March to 2:00 a.m. on the
    first Sunday of November (US rule since 2007)."""
    y = utc.year
    start = datetime.datetime.combine(_nth_sunday(y, 3, 2), datetime.time(7, 0))   # 2:00 EST
    end = datetime.datetime.combine(_nth_sunday(y, 11, 1), datetime.time(6, 0))    # 2:00 EDT
    naive = utc.replace(tzinfo=None)
    return -4 if start <= naive < end else -5


def to_eastern(utc):
    return utc.replace(tzinfo=None) + datetime.timedelta(hours=eastern_offset(utc))


def eastern_to_utc(local):
    for off in (-4, -5):
        utc = local - datetime.timedelta(hours=off)
        if eastern_offset(utc) == off:
            return utc
    return local + datetime.timedelta(hours=5)


def parse_now(text):
    """ISO time -> naive UTC. A time without an offset is taken as New York time."""
    if not text:
        return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)
    t = text.strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise SizeError("--now wants a time like \"2026-10-19 10:15\" (New York) or ...Z (UTC)")
    if dt.tzinfo is None:
        return eastern_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def parse_utc(text):
    """"2026-10-19T14:52:13Z" -> naive UTC, or None."""
    if not text:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = (dt - dt.utcoffset()).replace(tzinfo=None)
    return dt


def utc_iso(now_utc=None):
    now_utc = now_utc or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    return now_utc.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- inputs

def read_json(path, what):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise SizeError("no %s at %s" % (what, path))
    except json.JSONDecodeError as e:
        raise SizeError("%s is not valid JSON: %s" % (path, e))


def load_config(path, fx_rate=None, fx_path=FX_PATH, now_utc=None):
    """DEFAULTS, then your config with every money limit in dollars (a GBP one is converted)."""
    raw = read_json(path, "config") if path and os.path.exists(path) else {}
    try:
        mc = money_config(raw, fx_rate, fx_path, now_utc)
    except CurrencyError as e:
        raise SizeError(str(e))
    cfg = dict(DEFAULTS)
    cfg.update(mc)
    return cfg


def converted(cfg):
    """DEFAULTS plus a config that load_config (or money_config) has already put in dollars.
    A GBP config that was not converted is refused, never read as dollars."""
    cfg = dict(cfg or {})
    try:
        if not cfg.get("money"):
            if config_currency(cfg) == "GBP":
                raise CurrencyError("this GBP config was not converted to dollars; load it with load_config")
            cfg = money_config(cfg)
    except CurrencyError as e:
        raise SizeError(str(e))
    c = dict(DEFAULTS)
    c.update(cfg)
    return c


def fractional_ok(c):
    """Fractional shares in a day-trade bracket: only for Trading 212 with "fractional": true.
    (Alpaca takes no fractions in a bracket.) Trading 212 itself has the last word per stock."""
    return bool(c.get("fractional")) and str(c.get("broker") or "").lower() in ("trading212", "t212")


def shares_step(usd, per_share, step):
    """Shares that fit in usd at per_share, to the step (1 or 0.01), in exact integer cents."""
    if step >= 1:
        return shares_for(usd, per_share)
    u, p = int(round(float(usd) * 100)), int(round(float(per_share) * 100))
    if p <= 0 or u <= 0:
        return 0
    return (u * 100 // p) / 100.0


def qty_text(q):
    q = round(float(q), 2)
    return str(int(q)) if q == int(q) else ("%.2f" % q).rstrip("0")


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def parse_weights(data):
    """Accept a weights file, a {ticker: weight} dict or a list of rows.

    Returns [{"ticker", "name", "weight"}] with weight as a float."""
    if isinstance(data, dict) and "weights" in data:
        data = data["weights"]
    rows = []
    if isinstance(data, dict):
        for t, w in data.items():
            rows.append({"ticker": t, "name": "", "weight": w})
    elif isinstance(data, list):
        for i, r in enumerate(data):
            if not isinstance(r, dict):
                raise SizeError("weights[%d] is not an object" % i)
            t = r.get("ticker") or r.get("symbol")
            w = r.get("weight", r.get("weight_pct", r.get("pct")))
            if not t or w is None:
                raise SizeError("weights[%d] needs a ticker and a weight" % i)
            rows.append({"ticker": t, "name": r.get("name", ""), "weight": w})
    else:
        raise SizeError("weights must be a list or an object")
    out = []
    for r in rows:
        try:
            w = float(r["weight"])
        except (TypeError, ValueError):
            raise SizeError("weight for %s is not a number: %r" % (r["ticker"], r["weight"]))
        out.append({"ticker": str(r["ticker"]).strip().upper(), "name": r["name"] or "", "weight": w})
    return out


def weights_from_arg(text):
    """AAPL=22.04,AXP=17.14 -> {ticker: weight}"""
    out = {}
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise SizeError("--weights wants TICKER=WEIGHT pairs, got %r" % part)
        t, w = part.split("=", 1)
        out[t.strip().upper()] = w.strip()
    return out


def load_prices(path):
    if not path:
        return {}
    data = read_json(path, "prices")
    prices = data.get("prices", data) if isinstance(data, dict) else {}
    out = {}
    for t, p in prices.items():
        try:
            out[str(t).upper()] = float(p)
        except (TypeError, ValueError):
            continue
    return out


def price_time(doc, sym):
    """When this symbol's price is from: the quote's own time (detail[sym].time), else when it
    was fetched (detail[sym].fetched), else the file's as_of."""
    det = ((doc or {}).get("detail") or {}).get(sym) or {}
    for key in ("time", "fetched"):
        t = parse_utc(det.get(key))
        if t:
            return t
    return parse_utc((doc or {}).get("as_of"))


def fresh_price(doc, sym, now_utc, minutes=FRESH_MINUTES):
    """(price, fetched_utc, fresh) for sym from a prices.json doc, or (None, None, False)."""
    if not isinstance(doc, dict):
        return None, None, False
    p = _f((doc.get("prices") or {}).get(sym))
    if p is None or p <= 0:
        return None, None, False
    t = price_time(doc, sym)
    if t is None:
        return p, None, False
    age = (now_utc - t).total_seconds()
    return p, t, -60 <= age <= minutes * 60


# ---------------------------------------------------------------- money helpers

def round_cent(x):
    """Round a price to the cent (half up)."""
    return math.floor(float(x) * 100 + 0.5 + EPS) / 100.0


def shares_for(usd, per_share):
    """Whole shares that fit in usd at per_share, in exact integer cents."""
    u, p = int(round(float(usd) * 100)), int(round(float(per_share) * 100))
    if p <= 0 or u <= 0:
        return 0
    return u // p


def _price(x):
    """358.32 -> "358.32": Alpaca wants prices as strings."""
    return "%.2f" % round_cent(x)


def _num(x):
    """1250 -> "1250", 1118.13 -> "1118.13" (Alpaca wants these as strings)."""
    x = round(float(x), 2)
    if abs(x - round(x)) < 0.005:
        return str(int(round(x)))
    return "%.2f" % x


def money(x):
    x = float(x)
    if abs(x - round(x)) < 0.005:
        return "$" + "{:,.0f}".format(round(x))
    return "$" + "{:,.2f}".format(x)


# ---------------------------------------------------------------- plan mode (the default)

def size_trade(plan, cfg, budget=None, limit=None, price=None, cash=None, today=None):
    """Pure function: one plan + your rules -> the sizing (a dict). qty may be 0.

    limit  the limit price for a limit order (None = market)
    price  a FRESH latest price; a market order is sized at the higher of it and the plan's entry
    cash   the cash in trader/portfolio.json, if there is one
    today  New York date (YYYY-MM-DD); a plan for another day is refused"""
    c = converted(cfg)
    if not isinstance(plan, dict):
        raise SizeError("the plan is not a JSON object")
    sym = str(plan.get("symbol") or "").strip().upper()
    if not sym:
        raise SizeError("the plan has no symbol (run trader-exit)")
    side = str(plan.get("side") or "buy").strip().lower()
    if side != "buy":
        raise SizeError("this pack sizes long (buy) day trades only; the plan says %r" % side)
    if today and plan.get("date") and str(plan["date"]) != today:
        raise SizeError("the plan is for %s and today is %s in New York: make a new plan with trader-exit"
                        % (plan["date"], today))
    entry_plan, stop, target = _f(plan.get("entry")), _f(plan.get("stop")), _f(plan.get("target"))
    for name, v in (("entry", entry_plan), ("stop", stop), ("target", target)):
        if v is None or v <= 0:
            raise SizeError("the plan needs a positive %s (run trader-exit); a bracket order needs entry, "
                            "stop and target" % name)
    blocked = {str(b).upper() for b in (c.get("blocked") or [])}
    if sym in blocked:
        raise SizeError("%s is on your blocked list" % sym)

    gbp = c.get("currency") == "GBP"
    if gbp and budget is not None:
        raise SizeError("your config is in GBP: set budget_gbp in trader/config.json instead of --budget")
    budget = float(budget if budget is not None else (c.get("budget") or 0))
    if budget <= 0:
        raise SizeError("no budget: set \"budget_gbp\" in trader/config.json" if gbp else
                        "no budget: pass --budget or set \"budget\" in trader/config.json")
    buf_pct = float(c.get("cash_buffer_pct") or 0)
    if not 0 <= buf_pct < 100:
        raise SizeError("cash_buffer_pct must be between 0 and 100")
    step = 0.01 if fractional_ok(c) else 1

    if limit is not None:
        ref, ref_from = round_cent(limit), "limit"
    elif price is not None and round_cent(price) > round_cent(entry_plan):
        ref, ref_from = round_cent(price), "price"
    else:
        ref, ref_from = round_cent(entry_plan), "plan"
    stop, target = round_cent(stop), round_cent(target)
    if stop >= ref:
        raise SizeError("the stop %s is not below the entry %s; a long trade needs its stop underneath"
                        % (_price(stop), _price(ref)))
    if target <= ref:
        raise SizeError("the target %s is not above the entry %s: the move has already happened. "
                        "Skip it or make a new plan with trader-exit" % (_price(target), _price(ref)))
    risk_c = int(round((ref - stop) * 100))
    reward_c = int(round((target - ref) * 100))
    risk = risk_c / 100.0

    cash_buffer = round(budget * buf_pct / 100.0, 2)
    available = budget - cash_buffer
    cash_note = "budget %s minus the %s cash buffer" % (money(budget), money(cash_buffer))
    if cash is not None and cash - cash_buffer < available - 0.005:
        available = cash - cash_buffer
        cash_note = "cash %s in trader/portfolio.json minus the %s buffer" % (money(cash), money(cash_buffer))
    available = max(round(available, 2), 0.0)

    limits = []
    max_loss = _f(c.get("max_loss_per_trade_usd"))
    if max_loss:
        limits.append({"name": "max loss", "usd": max_loss, "per": risk, "shares": shares_step(max_loss, risk, step),
                       "how": "%s max loss / %s risk a share" % (gbp_label(c, "max_loss_per_trade_usd"), _price(risk))})
    cap_pct = _f(c.get("max_position_pct"))
    if cap_pct:
        cap = round(budget * cap_pct / 100.0, 2)
        limits.append({"name": "position cap", "usd": cap, "per": ref, "shares": shares_step(cap, ref, step),
                       "how": "%s%% of %s = %s / %s" % (_num(cap_pct), money(budget), money(cap), _price(ref))})
    max_order = _f(c.get("max_order_usd"))
    if max_order:
        limits.append({"name": "max order", "usd": max_order, "per": ref, "shares": shares_step(max_order, ref, step),
                       "how": "%s / %s" % (gbp_label(c, "max_order_usd"), _price(ref))})
    limits.append({"name": "cash", "usd": available, "per": ref, "shares": shares_step(available, ref, step),
                   "how": "%s = %s / %s" % (cash_note, money(available), _price(ref))})

    qty = max(0, min(l["shares"] for l in limits))
    bound = [l["name"] for l in limits if l["shares"] == qty]
    for l in limits:
        l["binds"] = l["name"] == bound[0]
    cost = round(qty * ref, 2)
    loss = round(qty * risk_c / 100.0, 2)
    refusal = None
    if qty == 0:
        if bound[0] == "max loss":
            refusal = ("one share risks %s (entry %s minus stop %s), more than your %s max loss per trade. "
                       "No order. Skip this trade, or plan a closer stop with trader-exit."
                       % (money(risk), _price(ref), _price(stop), gbp_label(c, "max_loss_per_trade_usd")))
        else:
            refusal = ("%s share of %s costs %s, more than your %s limit (%s). No order."
                       % ("0.01 of a" if step < 1 else "one", sym, money(ref), bound[0], limits[[l["name"] for l in limits].index(bound[0])]["how"]))
    elif _f(c.get("min_reward_risk")) and reward_c < _f(c["min_reward_risk"]) * risk_c - EPS:
        refusal = ("reward:risk %.2f (target %s, entry %s, stop %s) is under your minimum %s, so trader-risk "
                   "would block it. No order. Skip it, or make a new plan with trader-exit."
                   % (reward_c / float(risk_c), _price(target), _price(ref), _price(stop), _num(c["min_reward_risk"])))
    elif cost < float(c.get("min_order_usd") or 0) - 0.005:
        refusal = ("%s share%s of %s is %s, under your %s minimum order. No order."
                   % (qty_text(qty), "" if qty == 1 else "s", sym, money(cost), gbp_label(c, "min_order_usd")))
    return {
        "symbol": sym, "side": "buy", "setup": plan.get("setup", ""), "date": plan.get("date"),
        "entry_time": plan.get("entry_time", ""), "entry_plan": round_cent(entry_plan),
        "entry": ref, "entry_from": ref_from, "stop": stop, "target": target,
        "risk_per_share": risk, "reward_risk": round(reward_c / float(risk_c), 2),
        "stop_label": plan.get("stop_label", ""), "target_label": plan.get("target_label", ""),
        "budget": budget, "cash_buffer_pct": buf_pct, "cash_buffer": cash_buffer, "cash_available": available,
        "max_loss_per_trade_usd": max_loss, "limits": limits, "qty": qty, "bound_by": bound[0],
        "cost": cost, "max_loss": loss, "flat_by": plan.get("flat_by") or c.get("flat_by"),
        "order_type": "limit" if limit is not None else "market", "refusal": refusal,
        "share_step": step, "money": c.get("money"), "money_line": money_line(c),
        "max_loss_label": gbp_label(c, "max_loss_per_trade_usd"),
    }


def bracket_order(s):
    """The one proposed order, Alpaca place_stock_order fields (strings) plus why and plan."""
    if s["refusal"] or s["qty"] <= 0:
        return None
    o = {"symbol": s["symbol"], "side": "buy", "qty": qty_text(s["qty"]), "type": s["order_type"],
         "time_in_force": "day"}
    if s["order_type"] == "limit":
        o["limit_price"] = _price(s["entry"])
    setup = SETUP_WORDS.get(s["setup"], s["setup"] or "day trade")
    why = ("%s on %s: %s shares, bound by your %s (%s x %s = %s at the stop)"
           % (setup, s["symbol"], qty_text(s["qty"]), {"max loss": "%s max loss per trade" % s.get("max_loss_label", money(s["max_loss_per_trade_usd"] or 0)),
                                             "position cap": "position cap", "max order": "max order",
                                             "cash": "cash after the buffer"}[s["bound_by"]],
              qty_text(s["qty"]), _price(s["risk_per_share"]), money(s["max_loss"])))
    o.update({
        "order_class": "bracket",
        "take_profit_limit_price": _price(s["target"]),
        "stop_loss_stop_price": _price(s["stop"]),
        "why": why,
        "plan": {"entry": s["entry"], "stop": s["stop"], "target": s["target"],
                 "risk_per_share": s["risk_per_share"], "max_loss": s["max_loss"], "setup": s["setup"],
                 "entry_time": s["entry_time"], "flat_by": s["flat_by"]},
    })
    return o


def plan_report(s, order, notes=()):
    lines = []
    setup = SETUP_WORDS.get(s["setup"], s["setup"] or "setup")
    lines.append("%s buy, %s, entry %s%s" % (s["symbol"], setup, _price(s["entry_plan"]),
                                            (" at " + s["entry_time"]) if s["entry_time"] else ""))
    lines.append("Stop %s%s. Target %s%s." % (_price(s["stop"]), (": " + s["stop_label"]) if s["stop_label"] else "",
                                             _price(s["target"]),
                                             (": " + s["target_label"]) if s["target_label"] else ""))
    for n in notes:
        lines.append(n)
    src = {"plan": "the plan's entry", "price": "the latest price, above the plan's entry",
           "limit": "your limit price"}[s["entry_from"]]
    lines.append("Sized at %s (%s). Risk %s a share (entry %s minus stop %s). Reward:risk %.2f."
                 % (_price(s["entry"]), src, _price(s["risk_per_share"]), _price(s["entry"]), _price(s["stop"]),
                    s["reward_risk"]))
    lines.append("")
    if s.get("money_line"):
        lines.append(s["money_line"])
    lines.append("Limits (%s; the smallest one wins):"
                 % ("to 0.01 of a share" if s.get("share_step", 1) < 1 else "whole shares"))
    for l in s["limits"]:
        lines.append("  %-13s %-58s %6s shares%s" % (l["name"], l["how"], qty_text(l["shares"]),
                                                      "  <- binds" if l["binds"] else ""))
    lines.append("")
    if order is None:
        lines.append("No order: " + s["refusal"])
        lines.append("Education and tooling, not financial advice.")
        return "\n".join(lines)
    lines.append("Size: %s shares of %s = %s at %s. Max loss at the stop: %s x %s = %s."
                 % (qty_text(s["qty"]), s["symbol"], money(s["cost"]), _price(s["entry"]), qty_text(s["qty"]),
                    _price(s["risk_per_share"]), money(s["max_loss"])))
    lines.append("A stop is an order, not a promise: in a fast market it can fill below %s." % _price(s["stop"]))
    lines.append("")
    lines.append("Proposed bracket order (place_stock_order arguments, nothing has been placed):")
    how = "limit %s" % order["limit_price"] if order["type"] == "limit" else "market"
    lines.append("  buy %s %s, %s, day, take-profit %s, stop-loss %s"
                 % (order["qty"], order["symbol"], how, order["take_profit_limit_price"],
                    order["stop_loss_stop_price"]))
    if s["flat_by"]:
        lines.append("Flat by %s New York: anything still open then gets closed (trader-alerts rings, "
                     "trader-broker flatten)." % s["flat_by"])
    lines.append("")
    lines.append("Next: trader-risk checks it, then trader-broker asks you yes or no.")
    lines.append("Education and tooling, not financial advice.")
    return "\n".join(lines)


# ---------------------------------------------------------------- weights mode (v1)

def normalise(rows, blocked=(), top=None):
    """Drop blocked and non-positive weights, keep the top N, renormalise to 100."""
    blocked = {str(b).upper() for b in (blocked or [])}
    dropped, keep, seen = [], [], set()
    for r in rows:
        if r["ticker"] in seen:
            raise SizeError("%s is listed twice in the weights" % r["ticker"])
        seen.add(r["ticker"])
        if r["ticker"] in blocked:
            dropped.append({"ticker": r["ticker"], "reason": "on your blocked list"})
        elif r["weight"] <= 0:
            dropped.append({"ticker": r["ticker"], "reason": "weight is zero or negative"})
        else:
            keep.append(dict(r))
    keep.sort(key=lambda r: -r["weight"])
    if top:
        for r in keep[top:]:
            dropped.append({"ticker": r["ticker"], "reason": "outside the top %d" % top})
        keep = keep[:top]
    total = sum(r["weight"] for r in keep)
    for r in keep:
        r["source_weight"] = r["weight"]
        r["weight"] = 100.0 * r["weight"] / total if total else 0.0
    return keep, dropped


def allocate(rows, investable, cap):
    """Exact (unrounded) dollars per ticker with the cap enforced.

    Proportional to weight; any stock over the cap is pinned at the cap and
    the rest of the money is shared over the others, repeated until no stock
    is over. Returns ({ticker: dollars}, set of capped tickers)."""
    alloc, capped = {}, set()
    active = [r for r in rows]
    money_left = investable
    while active:
        wsum = sum(r["weight"] for r in active)
        trial = {r["ticker"]: money_left * r["weight"] / wsum for r in active}
        over = [r for r in active if trial[r["ticker"]] > cap + EPS]
        if not over:
            alloc.update(trial)
            break
        for r in over:
            alloc[r["ticker"]] = cap
            capped.add(r["ticker"])
            money_left -= cap
        active = [r for r in active if r["ticker"] not in capped]
        if money_left <= EPS:
            for r in active:
                alloc[r["ticker"]] = 0.0
            break
    return alloc, capped


def round_to_unit(exact, capped, order, cap, unit):
    """Round to whole dollars (unit 1) or cents (unit 0.01) and keep the total.

    The total placed is the exact total rounded DOWN to the unit, so it never
    spends money you do not have. Each slice is rounded to the nearest unit,
    then the difference (usually one unit) goes to the biggest stock that is
    not capped, then the next, never pushing anyone over the cap."""
    def units(x):
        return int(math.floor(x / unit + EPS))

    cap_u = units(cap)
    total_u = units(sum(exact.values()))
    out = {}
    for t, v in exact.items():
        out[t] = cap_u if t in capped else int(math.floor(v / unit + 0.5 + EPS))
    diff = total_u - sum(out.values())
    takers = [t for t in order if t not in capped]
    guard = 0
    while diff != 0 and takers and guard < 100000:
        moved = False
        for t in takers:
            if diff == 0:
                break
            if diff > 0 and out[t] + 1 <= cap_u:
                out[t] += 1
                diff -= 1
                moved = True
            elif diff < 0 and out[t] > 0:
                out[t] -= 1
                diff += 1
                moved = True
        if not moved:
            break
        guard += 1
    return {t: round(u * unit, 2) for t, u in out.items()}


def whole_shares(dollars, prices, order, cap, cash):
    """Whole shares: floor each slice, then buy one more share at a time for the
    stock furthest below its slice while cash lasts and the cap holds."""
    shares = {}
    for t in order:
        shares[t] = int(math.floor(dollars[t] / prices[t] + EPS)) if dollars[t] > 0 else 0
    spent = sum(shares[t] * prices[t] for t in order)
    left = cash - spent
    while True:
        best, gap = None, None
        for t in order:
            p = prices[t]
            if p > left + EPS or (shares[t] + 1) * p > cap + EPS:
                continue
            g = dollars[t] - shares[t] * p
            if best is None or g > gap + EPS:
                best, gap = t, g
        if best is None:
            break
        shares[best] += 1
        left -= prices[best]
    return shares


def size(rows, cfg, budget=None, prices=None, whole=None, cents=False, top=None):
    """Weights mode. Pure function: weights + rules -> the full sizing result (a dict)."""
    try:
        if config_currency(cfg) == "GBP":
            if not cfg.get("money"):
                raise CurrencyError("this GBP config was not converted to dollars; load it with load_config")
            if budget is not None:
                raise CurrencyError("your config is in GBP: set budget_gbp in trader/config.json "
                                    "instead of --budget")
    except CurrencyError as e:
        raise SizeError(str(e))
    budget = float(budget if budget is not None else (cfg.get("budget") or 0))
    if budget <= 0:
        raise SizeError("no budget: pass --budget or set \"budget\" in trader/config.json")
    buf_pct = float(cfg.get("cash_buffer_pct", 10))
    cap_pct = float(cfg.get("max_position_pct", 25))
    min_order = float(cfg.get("min_order_usd", 5) or 0)
    if not 0 <= buf_pct < 100:
        raise SizeError("cash_buffer_pct must be between 0 and 100")
    if not 0 < cap_pct <= 100:
        raise SizeError("max_position_pct must be above 0 and at most 100")
    if whole is None:
        whole = not cfg.get("fractional", True)
    prices = prices or {}

    keep, dropped = normalise(rows, cfg.get("blocked"), top)
    if not keep:
        raise SizeError("nothing to size: no weights left after the blocked list")

    cash_buffer = round(budget * buf_pct / 100.0, 2)
    investable = round(budget - cash_buffer, 2)
    cap = round(budget * cap_pct / 100.0, 2)
    unit = 0.01 if (cents or whole) else 1.0

    if whole:
        missing = [r["ticker"] for r in keep if not prices.get(r["ticker"])]
        if missing:
            raise SizeError("whole shares need a price for: %s (add them to trader/prices.json, "
                            "e.g. with trader-alerts: alerts.py quote %s --write)"
                            % (", ".join(missing), " ".join(missing)))

    while True:
        live = [dict(r) for r in keep]
        tot = sum(r["source_weight"] for r in live)
        for r in live:
            r["weight"] = 100.0 * r["source_weight"] / tot
        exact, capped = allocate(live, investable, cap)
        order = [r["ticker"] for r in sorted(live, key=lambda r: -exact[r["ticker"]])]
        dollars = round_to_unit(exact, capped, order, cap, unit)
        shares = None
        if whole:
            shares = whole_shares(dollars, prices, order, cap, sum(dollars.values()))
            dollars = {t: round(shares[t] * prices[t], 2) for t in order}
        small = [r for r in live if dollars[r["ticker"]] < min_order - EPS]
        if not small:
            break
        worst = min(small, key=lambda r: (r["source_weight"], r["ticker"]))
        if whole and shares is not None and shares[worst["ticker"]] == 0 and prices.get(worst["ticker"], 0) > 0:
            reason = ("one share costs $%s, more than its slice; it cannot be bought in whole shares"
                      % _num(prices[worst["ticker"]]))
        else:
            reason = "its slice ($%s) is under your $%s minimum order" % (
                _num(dollars[worst["ticker"]]), _num(min_order))
        dropped.append({"ticker": worst["ticker"], "reason": reason})
        keep = [r for r in keep if r["ticker"] != worst["ticker"]]
        if not keep:
            raise SizeError("every slice is under your minimum order; raise the budget or hold fewer stocks")

    positions = []
    for r in sorted(live, key=lambda r: -r["weight"]):
        t = r["ticker"]
        p = {"ticker": t, "name": r["name"], "weight": round(r["weight"], 2),
             "source_weight": r["source_weight"], "dollars": dollars[t],
             "capped": t in capped}
        if prices.get(t):
            p["price"] = prices[t]
            p["shares"] = shares[t] if whole else round(dollars[t] / prices[t], 4)
        positions.append(p)
    invested = round(sum(p["dollars"] for p in positions), 2)
    return {
        "budget": budget, "cash_buffer_pct": buf_pct, "cash_buffer": cash_buffer,
        "investable": investable, "max_position_pct": cap_pct, "cap": cap,
        "min_order_usd": min_order, "whole_shares": bool(whole),
        "positions": positions, "invested": invested,
        "idle": round(investable - invested, 2), "dropped": dropped,
    }


def orders_from(result, source=""):
    """Weights mode: proposed buy orders, one per position, in place_stock_order shape."""
    out = []
    for p in result["positions"]:
        if p["dollars"] <= 0:
            continue
        why = "%.2f%% of the split" % p["weight"]
        if source:
            why = "%s, weights from %s" % (why, source)
        if p["capped"]:
            why += "; capped at %s%% of the %s budget" % (_num(result["max_position_pct"]), money(result["budget"]))
        o = {"symbol": p["ticker"], "side": "buy"}
        if result["whole_shares"]:
            o["qty"] = str(int(p["shares"]))
        else:
            o["notional"] = _num(p["dollars"])
        o.update({"type": "market", "time_in_force": "day", "why": why})
        out.append(o)
    return out


def report(result, orders, source=""):
    """Weights mode table."""
    r = result
    lines = []
    if source:
        lines.append("Weights from: %s" % source)
    lines.append("Budget %s, cash buffer %s%% = %s, investing %s, cap %s%% = %s per stock"
                 % (money(r["budget"]), _num(r["cash_buffer_pct"]), money(r["cash_buffer"]),
                    money(r["investable"]), _num(r["max_position_pct"]), money(r["cap"])))
    lines.append("")
    has_price = any("price" in p for p in r["positions"])
    head = "%-7s %-22s %8s %11s" % ("ticker", "name", "weight", "dollars")
    if has_price:
        head += " %10s %10s" % ("price", "shares")
    lines.append(head + "  note")
    for p in r["positions"]:
        row = "%-7s %-22s %7.2f%% %11s" % (p["ticker"], (p["name"] or "")[:22], p["weight"], money(p["dollars"]))
        if has_price:
            row += " %10s %10s" % (money(p["price"]) if "price" in p else "", p.get("shares", ""))
        if p["capped"]:
            row += "  capped at %s%%" % _num(r["max_position_pct"])
        lines.append(row)
    lines.append("%-7s %-22s %8s %11s" % ("", "invested", "", money(r["invested"])))
    lines.append("%-7s %-22s %8s %11s" % ("", "cash buffer", "", money(r["cash_buffer"])))
    lines.append("%-7s %-22s %8s %11s" % ("", "left idle", "", money(r["idle"])))
    if r["idle"] >= 1:
        lines.append("")
        lines.append("Left idle %s: %s" % (money(r["idle"]), (
            "whole shares do not divide evenly" if r["whole_shares"]
            else "every stock is at its cap; add stocks or raise max_position_pct")))
    for d in r["dropped"]:
        lines.append("Dropped %s: %s" % (d["ticker"], d["reason"]))
    lines.append("")
    lines.append("Proposed orders (place_stock_order arguments, nothing has been placed):")
    for o in orders:
        amount = ("notional \"%s\"" % o["notional"]) if "notional" in o else ("qty \"%s\"" % o["qty"])
        lines.append("  %s %s %s, %s, %s" % (o["side"], o["symbol"], amount, o["type"], o["time_in_force"]))
    lines.append("")
    lines.append("These buys have no stop. trader-risk blocks them while \"require_stop\" is true and applies "
                 "your day-trade limits to every buy.")
    lines.append("Next: trader-risk checks every order, then trader-broker asks you yes or no for each one.")
    lines.append("Education and tooling, not financial advice.")
    return "\n".join(lines)


# ---------------------------------------------------------------- orders file

def write_orders(path, orders, cfg, source="", now_utc=None):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    doc = {
        "created_by": "trader-budget",
        "created_at": utc_iso(now_utc),
        "status": "proposed",
        "paper": bool(cfg.get("paper", True)),
        "note": ("Proposals only. Nothing is placed until trader-risk passes each order "
                 "and you say yes to it in the chat."),
        "source": source,
        "orders": orders,
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------- main

def run_plan(a, cfg):
    now = parse_now(a.now)
    today = to_eastern(now).date().isoformat()
    plan = read_json(a.plan, "plan (run trader-exit first, or use --weights for a split)")
    limit = None
    if a.limit is not None:
        if a.limit == "plan":
            limit = _f((plan or {}).get("entry"))
        else:
            limit = _f(a.limit)
            if limit is None or limit <= 0:
                raise SizeError("--limit wants a price, like --limit 364.00")
    sym = str((plan or {}).get("symbol") or "").upper()
    notes, price = [], None
    ppath = a.prices or ("trader/prices.json" if os.path.exists("trader/prices.json") else None)
    if ppath and limit is None and sym:
        pdoc = read_json(ppath, "prices")
        p, t, fresh = fresh_price(pdoc, sym, now)
        if p is not None and fresh:
            price = p
            if round_cent(p) > round_cent(_f(plan.get("entry")) or 0):
                notes.append("%s last %s (as of %s New York) is above the plan's entry %s, so the risk "
                             "is bigger: sized at %s." % (sym, _price(p), to_eastern(t).strftime("%H:%M"),
                                                          _price(plan.get("entry")), _price(p)))
        elif p is not None:
            notes.append("%s in %s is %s: not used (older than %d minutes). Sized at the plan's entry."
                         % (sym, ppath, ("from " + to_eastern(t).strftime("%Y-%m-%d %H:%M") + " New York")
                            if t else "undated", FRESH_MINUTES))
    cash = None
    if a.portfolio and os.path.exists(a.portfolio):
        pf = read_json(a.portfolio, "portfolio")
        cash = _f(pf.get("cash")) if isinstance(pf, dict) else None
    s = size_trade(plan, cfg, a.budget, limit, price, cash, today)
    order = bracket_order(s)
    if order and a.write:
        src = "trader/plan.json (%s %s %s)" % (s["symbol"], s["setup"] or "plan", s["date"] or today)
        write_orders(a.write, [order], cfg, src, now)
    if a.json:
        print(json.dumps({"sizing": s, "orders": [order] if order else [], "notes": notes}, indent=2))
    else:
        print(plan_report(s, order, notes))
        if order and a.write:
            print("Wrote 1 proposed bracket order to %s" % a.write)
    return 0 if order else 1


def run_weights(a, cfg):
    source = ""
    if a.weights:
        rows = parse_weights(weights_from_arg(a.weights))
    else:
        data = read_json(a.targets, "weights file")
        rows = parse_weights(data)
        if isinstance(data, dict):
            source = data.get("source", "")
    prices = load_prices(a.prices)
    if not prices and os.path.exists("trader/prices.json") and not a.prices:
        prices = load_prices("trader/prices.json")
    whole = True if a.whole_shares else None
    result = size(rows, cfg, a.budget, prices, whole, a.cents, a.top)
    orders = orders_from(result, source)
    if a.write:
        write_orders(a.write, orders, cfg, source, parse_now(a.now))
    if a.json:
        print(json.dumps({"result": result, "orders": orders}, indent=2))
    else:
        print(report(result, orders, source))
        if a.write:
            print("Wrote %d proposed orders to %s" % (len(orders), a.write))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Size a day trade to your budget and max loss (trader-budget).")
    ap.add_argument("--plan", default="trader/plan.json", help="the trade plan (default trader/plan.json)")
    ap.add_argument("--config", default="trader/config.json", help="rules (default trader/config.json)")
    ap.add_argument("--budget", type=float, help="override the budget in the config (USD configs only)")
    ap.add_argument("--fx", default=FX_PATH, help="GBP/USD rate file for a GBP config (default trader/fx.json)")
    ap.add_argument("--fx-rate", type=float, help="USD per GBP, e.g. 1.31, instead of the rate file")
    ap.add_argument("--limit", nargs="?", const="plan", metavar="PRICE",
                    help="a limit order instead of market, at the plan's entry or at PRICE")
    ap.add_argument("--prices", help="prices JSON (default trader/prices.json if it exists)")
    ap.add_argument("--portfolio", default="trader/portfolio.json",
                    help="your account; its cash caps the size if it exists")
    ap.add_argument("--now", help="pretend it is this New York time, e.g. \"2026-10-19 10:15\"")
    ap.add_argument("--write", nargs="?", const="trader/orders.json", metavar="PATH",
                    help="write the proposed order (default trader/orders.json)")
    ap.add_argument("--json", action="store_true", help="print JSON instead of the report")
    g = ap.add_argument_group("weights mode (split a budget across a list)")
    g.add_argument("--weights", help="inline weights: AAPL=22.04,AXP=17.14")
    g.add_argument("--targets", help="a weights file: {\"weights\": [{\"ticker\", \"weight\"}]}")
    g.add_argument("--top", type=int, help="keep only the N biggest weights")
    g.add_argument("--whole-shares", action="store_true", help="whole shares only (needs --prices)")
    g.add_argument("--cents", action="store_true", help="size to the cent instead of whole dollars")
    a = ap.parse_args(argv)
    try:
        cfg = load_config(a.config, a.fx_rate, a.fx, parse_now(a.now) if a.now else None)
        if a.weights or a.targets:
            return run_weights(a, cfg)
        if a.top or a.whole_shares or a.cents:
            raise SizeError("--top, --whole-shares and --cents work with --weights or --targets")
        return run_plan(a, cfg)
    except SizeError as e:
        print("size.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
