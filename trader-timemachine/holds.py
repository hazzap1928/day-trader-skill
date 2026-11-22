#!/usr/bin/env python3
"""The Time Machine for HELD trades (days or weeks), on daily bars. Standard library only, broker-neutral.

The old Time Machine (timemachine.py, beside this file, unchanged) replayed one day trade on 5-minute bars.
This one walks a share held for days, the way the agent holds drift and news buys:

  - bought at the open of the first day;
  - a stop under it: each day, a day that OPENS at or under the stop sells at that open (a gap), else a day
    whose LOW reaches it sells at the stop;
  - otherwise sold at the close on the sell date;
  - costs: 0.15% FX fee + 0.05% slippage on the buy and on the sale (a GBP account at Trading 212).

Three sell rules, compared on the same trades:
  "fixed"   the stop stays where it was set: open minus STOP_MULT (3) x the 14-day ATR (what drift and news do)
  "rising"  the same start, then raised after each close to the highest close so far minus the same distance;
            never lowered (the idea of "raising the stop as the price rises"; untested, shadow only)
  "none"    no stop: out on the sell date only (what the drift research tested)

Two uses:
  walk()        one real past trade, for the drift stop backtest (research/pead_stop.py)
  noise_test()  one holding: its own past daily moves (with its past trend taken out), drawn in 5-day blocks,
                replayed RUNS times from its buy to its sell date. It shows how often ordinary ups and downs reach the stop first, and the spread
                of results under each rule. It knows nothing about the news or the earnings beat behind the buy,
                so it says nothing about whether the idea is good. Past moves are not a forecast.

No hindsight: atr() and noise_test() only ever see bars from BEFORE the buy day.
Education and tooling, not financial advice.
"""

import math
import random
import statistics as st

COST = 0.0015 + 0.0005          # each way: FX fee + slippage
STOP_MULT = 3.0
ATR_DAYS = 14
RUNS = 1000
BLOCK = 5                       # days drawn together, so calm and wild spells stay together
HISTORY = 250                   # daily moves the noise test draws from (about a year)
RULES = ("fixed", "rising", "none")


def atr(rows, i, n=ATR_DAYS):
    """Average true range over the n days before index i of rows [(open, high, low, close), ...], or None.
    Uses rows[i - n - 1 .. i - 1] only (the same as the live drift.atr)."""
    if i < n + 1:
        return None
    trs = []
    for k in range(i - n, i):
        o, h, l, c = rows[k][:4]
        pc = rows[k - 1][3]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / n


def net(entry, exit_):
    """The result after costs, as a fraction: exit x (1 - cost) / (entry x (1 + cost)) - 1."""
    return exit_ * (1 - COST) / (entry * (1 + COST)) - 1


def walk(rows, e, x, rule="fixed", dist=None, stop=None):
    """Hold from the open of rows[e] to the close of rows[x] under a sell rule.

    dist: the stop's distance under the entry (e.g. 3 x ATR); stop: an explicit starting stop instead.
    Returns {"exit_i", "exit_px", "why": "stop" | "date", "gross", "net", "entry", "stop_at_exit"}."""
    if rule not in RULES:
        raise ValueError("rule must be one of %s" % (RULES,))
    entry = rows[e][0]
    level = None
    if rule != "none":
        if stop is None:
            if dist is None:
                raise ValueError("a stop rule needs dist or stop")
            stop = entry - dist
        level = stop
        dist = entry - stop if dist is None else dist
    best = None
    for i in range(e, x + 1):
        o, h, l, c = rows[i][:4]
        if level is not None and level > 0:
            if i > e and o <= level:
                return _done(entry, i, o, "stop", level)
            if l <= level:
                return _done(entry, i, min(level, o) if i == e else level, "stop", level)
        best = c if best is None else max(best, c)
        if rule == "rising" and level is not None:
            level = max(level, best - dist)
    return _done(entry, x, rows[x][3], "date", level)


def _done(entry, i, px, why, level):
    return {"exit_i": i, "exit_px": px, "why": why, "entry": entry, "gross": px / entry - 1, "net": net(entry, px),
            "stop_at_exit": level}


# ---------------------------------------------------------------- one holding: its own past moves, replayed

def moves(rows, detrend=False):
    """Each day's (open, high, low, close) as multiples of the previous close. detrend: divided by the average
    daily change (geometric), so the past trend is taken out and only the ups and downs are left."""
    out = []
    for k in range(1, len(rows)):
        pc = rows[k - 1][3]
        if pc and pc > 0:
            o, h, l, c = rows[k][:4]
            out.append((o / pc, h / pc, l / pc, c / pc))
    if detrend and out:
        g = math.exp(sum(math.log(m[3]) for m in out) / len(out))
        out = [tuple(x / g for x in m) for m in out]
    return out


def path(mv, days, rnd, entry, block=BLOCK):
    """A made-up run of `days` daily bars starting at `entry` (the first day opens at entry)."""
    picks = []
    while len(picks) < days:
        s = rnd.randrange(0, max(1, len(mv) - block + 1))
        picks += mv[s:s + block]
    picks = picks[:days]
    rows, prev = [], None
    for j, (o, h, l, c) in enumerate(picks):
        if j == 0:
            base = entry / o                      # scale the first day so it opens at the entry price
        else:
            base = prev
        rows.append((o * base, h * base, l * base, c * base))
        prev = c * base
    return rows


def noise_test(before, entry, days, stop=None, mult=STOP_MULT, runs=RUNS, seed=19, block=BLOCK, history=HISTORY):
    """Replay a holding on its own past daily moves. `before`: [(open, high, low, close), ...] strictly before the
    buy day, oldest first. `days`: trading days held, the buy day included. `stop`: the real stop (else
    entry - mult x ATR). Returns a summary dict, or raises ValueError without enough history."""
    if days < 1:
        raise ValueError("no days to hold")
    if len(before) < max(ATR_DAYS + 2, 60):
        raise ValueError("not enough daily bars before the buy (%d)" % len(before))
    a = atr(before, len(before))
    if stop is None:
        stop = entry - mult * a
    dist = entry - stop
    if dist <= 0:
        raise ValueError("the stop is not under the entry")
    mv = moves(before[-(history + 1):], detrend=True)       # ups and downs only: its past trend is no forecast
    rnd = random.Random(seed)
    res = {r: [] for r in RULES}
    first = 0
    for _ in range(runs):
        p = path(mv, days, rnd, entry, block)
        for r in RULES:
            w = walk(p, 0, days - 1, r, dist=dist if r != "none" else None, stop=stop if r != "none" else None)
            res[r].append(w)
        if res["fixed"][-1]["why"] == "stop":
            first += 1
    out = {"runs": runs, "days": days, "entry": round(entry, 4), "stop": round(stop, 4), "atr": round(a, 4),
           "stop_pct_below": round(100 * dist / entry, 2), "history_days": len(mv), "block": block, "seed": seed,
           "stop_first_pct": round(100.0 * first / runs, 1), "rules": {}}
    for r in RULES:
        nets = sorted(w["net"] for w in res[r])
        out["rules"][r] = {"median_pct": round(100 * st.median(nets), 2), "mean_pct": round(100 * st.mean(nets), 2),
                           "p10_pct": round(100 * nets[int(0.10 * (len(nets) - 1))], 2),
                           "p90_pct": round(100 * nets[int(0.90 * (len(nets) - 1))], 2),
                           "win_pct": round(100.0 * sum(1 for x in nets if x > 0) / len(nets), 1),
                           "stopped_pct": round(100.0 * sum(1 for w in res[r] if w["why"] == "stop") / runs, 1)}
    return out


def words(nt):
    """One plain line for a log or a review."""
    if not nt or nt.get("error"):
        return "no replay (%s)" % ((nt or {}).get("error") or "none yet")
    r = nt["rules"]
    return ("in %d replays of its own past ups and downs (trend taken out), its safety net was hit first %.0f%% of the "
            "time; typical result after fees %+.1f%% as set, %+.1f%% with a rising safety net, %+.1f%% with none" % (
                nt["runs"], nt["stop_first_pct"], r["fixed"]["median_pct"], r["rising"]["median_pct"],
                r["none"]["median_pct"]))
