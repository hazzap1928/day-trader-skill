#!/usr/bin/env python3
"""Budget from the account value (demo and live).

    "account_budget": {"enabled": true, "budget_pct": 90, "max_loss_pct": 1, "daily_loss_pct": 3,
                       "max_budget_gbp": null}

Each trading morning, the account's total value is read from Trading 212 and written into
trader/config.json:
  budget_gbp              the account value (trader-budget keeps cash_buffer_pct of it in cash, so
                          budget_pct of it can be invested)
  cash_buffer_pct         100 - budget_pct  (the cash kept)
  max_loss_per_trade_gbp  max_loss_pct of the account value
  max_daily_loss_gbp      daily_loss_pct of the account value at the start of the trading day

With max_budget_gbp set, the amount invested never goes over it, and the loss limits are worked out
from the account the cap allows (max_budget_gbp / budget_pct), not the whole account. That keeps a
1% risk meaning 1% of the money actually at work.

Every tool already reads those keys, so nothing else changes. Run by run-guard.sh before the guard
starts (demo and live), and by the autopilot before each open (demo).

    python3 trader-broker/t212.py budget            # show today's figures
    python3 trader-broker/t212.py budget --apply    # write them into trader/config.json

Education and tooling, not financial advice.
"""

import datetime
import json
import math
import os

DEFAULTS = {"enabled": False, "budget_pct": 90.0, "max_loss_pct": 1.0, "daily_loss_pct": 3.0, "max_budget_gbp": None}
LIMITS = {"budget_pct": (10, 100), "max_loss_pct": (0.1, 5), "daily_loss_pct": (0.5, 10)}


class BudgetError(Exception):
    pass


def settings(cfg):
    """The account_budget settings, checked, or None when it is off."""
    raw = (cfg or {}).get("account_budget")
    if not raw or not raw.get("enabled"):
        return None
    s = dict(DEFAULTS)
    s.update({k: v for k, v in raw.items() if k in DEFAULTS})
    for k, (lo, hi) in LIMITS.items():
        v = s[k]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
            raise BudgetError("account_budget.%s must be a number from %g to %g" % (k, lo, hi))
        s[k] = float(v)
    cap = s["max_budget_gbp"]
    if cap in ("", None):
        s["max_budget_gbp"] = None
    elif isinstance(cap, bool) or not isinstance(cap, (int, float)) or cap <= 0:
        raise BudgetError("account_budget.max_budget_gbp must be a positive amount in pounds, or empty")
    if str((cfg or {}).get("currency", "")).upper() != "GBP":
        raise BudgetError("account_budget works with a GBP config (\"currency\": \"GBP\")")
    return s


def _down(x):
    return math.floor(x * 100 + 1e-9) / 100.0


def compute(value, s):
    """The figures for an account worth `value` pounds."""
    if value is None or value <= 0:
        raise BudgetError("the account value from Trading 212 is %r; the budget was not changed" % value)
    pct = s["budget_pct"] / 100.0
    base, capped = float(value), False
    if s["max_budget_gbp"] is not None and value * pct > s["max_budget_gbp"]:
        base, capped = s["max_budget_gbp"] / pct, True
    return {"account_value": round(float(value), 2), "base": _down(base), "capped": capped,
            "invest_gbp": float(s["max_budget_gbp"]) if capped else _down(base * pct),
            "cash_gbp": round(float(value) - (float(s["max_budget_gbp"]) if capped else _down(base * pct)), 2),
            "budget_gbp": _down(base), "cash_buffer_pct": round(100 - s["budget_pct"], 4),
            "max_loss_per_trade_gbp": _down(base * s["max_loss_pct"] / 100.0),
            "max_daily_loss_gbp": _down(base * s["daily_loss_pct"] / 100.0),
            "settings": {k: s[k] for k in DEFAULTS if k != "enabled"}}


def plain(r, today=None):
    s = r["settings"]
    lines = ["Budget from the account value%s: %s" % (" on %s" % today if today else "", "GBP {:,.2f}".format(r["account_value"]))]
    lines.append("  Can be invested: GBP {:,.2f} ({:g}% of the account{})".format(
        r["invest_gbp"], s["budget_pct"], ", capped by max_budget_gbp GBP {:,.2f}".format(s["max_budget_gbp"])
        if r["capped"] else ""))
    lines.append("  Kept as cash: GBP {:,.2f}".format(r["cash_gbp"]))
    lines.append("  Max loss a trade: GBP {:,.2f} ({:g}% of {})".format(
        r["max_loss_per_trade_gbp"], s["max_loss_pct"], "the account" if not r["capped"] else "the capped account"))
    lines.append("  Max loss a day: GBP {:,.2f} ({:g}% of {} at the start of the day)".format(
        r["max_daily_loss_gbp"], s["daily_loss_pct"], "the account" if not r["capped"] else "the capped account"))
    if s["max_budget_gbp"] is None:
        lines.append("  No max_budget_gbp cap set.")
    return "\n".join(lines)


def _write_config(path, cfg):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def apply(account_value, cfg_path, today, force=False):
    """Write today's figures into the config, once a day. Returns (result, changed)."""
    with open(cfg_path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    s = settings(cfg)
    if s is None:
        raise BudgetError("account_budget is not enabled in %s" % cfg_path)
    last = (cfg.get("account_budget") or {}).get("last") or {}
    if last.get("date") == today and not force:
        return dict(last.get("figures") or {}, settings={k: s[k] for k in DEFAULTS if k != "enabled"}), False
    r = compute(account_value, s)
    cfg["budget_gbp"] = r["budget_gbp"]
    cfg["cash_buffer_pct"] = r["cash_buffer_pct"]
    cfg["max_loss_per_trade_gbp"] = r["max_loss_per_trade_gbp"]
    cfg["max_daily_loss_gbp"] = r["max_daily_loss_gbp"]
    figures = {k: v for k, v in r.items() if k != "settings"}
    cfg["account_budget"]["last"] = {"date": today, "at": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                                     "source": "Trading 212 account summary, totalValue", "figures": figures}
    _write_config(cfg_path, cfg)
    return r, True
