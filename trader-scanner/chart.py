#!/usr/bin/env python3
"""Chart labels for a share at the moment it is bought (drift and news holds). Labels only: SHADOW.

Reuses the old day-trading scanner's daily patterns (scan.py find_setups: a new 20-day high, a tight run-up,
back above the 20-day average, oversold, an oversold bounce) and adds four checks on whether the chart is
falling apart. Every label is worked out from daily closes BEFORE the buy day only (no hindsight), and the
rule is fixed in advance as version 1: changing a threshold starts version 2.

  uptrend           the last close is above the 50-day average, and that average is higher than 20 days ago
  below_ma50        the last close is under the 50-day average
  down_10pct_5d     down 10% or more over the last 5 trading days
  new_20d_low       the last close is under every close of the 20 days before it

It decides nothing: the labels are written on the holdings page and shown in the daily review, and the
research replay (research/pead_chart.py) compares past drift trades with and without each label, after
fees. Whether any label may ever affect a buy is the owner's decision. Standard library, broker-neutral.
"""

import importlib.util
import os

HERE = os.path.dirname(os.path.abspath(__file__))
VERSION = 1
MIN_CLOSES = 51

WORDS = {
    "uptrend": "in an uptrend",
    "below_ma50": "under its 50-day average",
    "down_10pct_5d": "down 10% or more in the last week",
    "new_20d_low": "at a 20-day low",
    "breakout_20d": "at a new 20-day high",
    "bull_flag": "resting after a sharp run-up",
    "reclaim_ma20": "just back above its 20-day average",
    "oversold": "oversold (14-day RSI under 30)",
    "oversold_bounce": "bouncing from oversold",
}
FALLING = ("below_ma50", "down_10pct_5d", "new_20d_low")

_scan = None


def scan():
    global _scan
    if _scan is None:
        spec = importlib.util.spec_from_file_location("chart_scan", os.path.join(HERE, "scan.py"))
        _scan = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_scan)
    return _scan


def labels(closes):
    """closes: daily closes strictly before the buy day, oldest first (at least MIN_CLOSES).
    Returns {"version", "tags": [...], "falling": bool, "nums": {...}}; raises ValueError with too few."""
    c = [float(x) for x in closes]
    if len(c) < MIN_CLOSES:
        raise ValueError("need %d daily closes before the buy, got %d" % (MIN_CLOSES, len(c)))
    tags, nums = scan().find_setups(c)
    tags = list(tags)
    last = c[-1]
    ma50 = sum(c[-50:]) / 50.0
    ma50_then = sum(c[-70:-20]) / 50.0 if len(c) >= 70 else None
    chg5 = last / c[-6] - 1
    if last > ma50 and ma50_then is not None and ma50 > ma50_then:
        tags.append("uptrend")
    if last < ma50:
        tags.append("below_ma50")
    if chg5 <= -0.10:
        tags.append("down_10pct_5d")
    if last < min(c[-21:-1]):
        tags.append("new_20d_low")
    nums = dict(nums, ma50=ma50, ma50_20d_ago=ma50_then, change_5d_pct=100 * chg5)
    nums = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in nums.items()}
    return {"version": VERSION, "tags": tags, "falling": any(t in FALLING for t in tags), "nums": nums}


def words(lab):
    if not lab or not lab.get("tags"):
        return "no chart pattern" if lab and not lab.get("error") else "no chart labels"
    return ", ".join(WORDS.get(t, t) for t in lab["tags"])
