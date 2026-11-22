"""The drift stop backtest: does the live drift stop (3 x the 14-day ATR under the buy) help or hurt?

The live drift rule was tested with NO stop (research/pead_tracker.py). The live agent added a stop that was
never tested. This replays every past drift signal of the tested rule with each sell rule, after costs:

  none          no stop: sold at the close of the 20th trading day held (the tested rule)
  fixed 2xATR   a stop 2 x ATR under the open it was bought at
  fixed 3xATR   3 x ATR: WHAT THE LIVE AGENT DOES
  fixed 4xATR   4 x ATR
  rising 3xATR  starts at 3 x ATR, then raised after each close to the highest close minus 3 x ATR (never lowered)

The signals are the tested rule's (ranking B: a beat, surprise of 10% or more on a forecast of at least $0.10
either side of zero, today's S&P 500 members from the day they joined), bought at the open of the 2nd trading
day after the report and held 20 trading days. Two views, each for 2019-2023 and 2024-2026 SEPARATELY:
  1. every signal on its own (average result a trade, after costs, and how each stop rule differs from none);
  2. the 10-slot book the agent runs (ranked by surprise / price, money in an S&P 500 tracker between trades),
     against holding the tracker alone.

No hindsight: the ATR uses the 14 days before the buy day; the stop is set from the buy-day open; a day that
opens under the stop sells at that open; the S&P membership is as of the report day.
Known limits (said again in the output): today's S&P 500 list only, so companies that left the index since 2019
(taken over, shrank or went bust) are missing; surprise figures as Nasdaq shows them now; prices adjusted for
dividends and splits; trades overlap; FX moves ignored.

    python3 research/pead_stop.py          # needs trader/research-data/prices.json, sp500.csv, earnings.json
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _data import DATA
import importlib.util, json, random, statistics as st

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
exec(open(os.path.join(HERE, "pead_tracker.py")).read().rsplit('\nif __name__ == "__main__":', 1)[0])

_spec = importlib.util.spec_from_file_location("holds", os.path.join(ROOT, "trader-timemachine", "holds.py"))
holds = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(holds)

RANKING, SLOTS = "B", 10
VARIANTS = [("none", "none", None), ("fixed 2xATR", "fixed", 2.0), ("fixed 3xATR (live)", "fixed", 3.0),
            ("fixed 4xATR", "fixed", 4.0), ("rising 3xATR", "rising", 3.0)]
PERIODS = [("2019-2023", DEV), ("2024-2026", TEST)]
BOOT = 2000


def rows_of(sym):
    I = S[sym]
    return list(zip(I["O"], I["H"], I["L"], I["C"]))


_ROWS = {}


def outcomes(ev):
    """Each variant's walk for one signal, or None when there are not 15 days of bars before the buy day."""
    if ev["sym"] not in _ROWS:
        _ROWS[ev["sym"]] = rows_of(ev["sym"])
    rows = _ROWS[ev["sym"]]
    e, x = ev["e"], ev["e"] + HOLD - 1
    a = holds.atr(rows, e)              # the 14 days BEFORE the buy day only
    if a is None:
        return None
    out = {}
    for name, rule, k in VARIANTS:
        out[name] = holds.walk(rows, e, x, rule, dist=None if rule == "none" else k * a)
    out["_atr_pct"] = 100 * a / rows[e][0]
    return out


def spy_net(entry_date, exit_i_date):
    T = S[TR]
    o, c = T["O"][T["idx"][entry_date]], T["C"][T["idx"][exit_i_date]]
    return c * (1 - K_TRACK) / (o * (1 + K_TRACK)) - 1


def boot_ci(diffs, seed=7):
    r = random.Random(seed)
    n = len(diffs)
    means = sorted(st.mean(r.choice(diffs) for _ in range(n)) for _ in range(BOOT))
    return means[int(0.025 * BOOT)], means[int(0.975 * BOOT)]


def signal_level(a, b):
    sigs = [ev for ev in events(a, b) if qualifies(ev, RANKING)]
    rows, short = [], 0
    for ev in sigs:
        o = outcomes(ev)
        if o is None:
            short += 1
            continue
        rows.append((ev, o))
    res = {"signals": len(sigs), "used": len(rows), "too_little_history": short, "variants": {}}
    base = [o["none"]["net"] for _, o in rows]
    for name, rule, k in VARIANTS:
        nets = [o[name]["net"] for _, o in rows]
        vs = [o[name]["net"] - spy_net(ev["entry_date"], S[ev["sym"]]["D"][o[name]["exit_i"]]) for ev, o in rows]
        diffs = [x - y for x, y in zip(nets, base)]
        lo, hi = boot_ci(diffs) if name != "none" else (0.0, 0.0)
        res["variants"][name] = {
            "avg_pct": 100 * st.mean(nets), "median_pct": 100 * st.median(nets),
            "win_pct": 100.0 * sum(1 for x in nets if x > 0) / len(nets),
            "stopped_pct": 100.0 * sum(1 for _, o in rows if o[name]["why"] == "stop") / len(rows),
            "worst_pct": 100 * min(nets), "best_pct": 100 * max(nets),
            "avg_vs_spy_pct": 100 * st.mean(vs),
            "diff_vs_none_pct": 100 * st.mean(diffs), "diff_ci95": (100 * lo, 100 * hi)}
    res["atr_pct_median"] = st.median(o["_atr_pct"] for _, o in rows)
    return res


def book(a, b, name):
    """The 10-slot book (pead_tracker.run with the stop): a stopped trade's money goes back into the tracker at
    that day's close, which frees its slot for the next signal."""
    picks = [ev for ev in events(a, b) if qualifies(ev, RANKING)]
    picks.sort(key=lambda ev: -score(ev, RANKING))
    by = {}
    for ev in picks:
        by.setdefault(ev["entry_date"], []).append(ev)
    days = [d for d in CAL if a <= d <= b]
    T = S[TR]
    units = CAPITAL * (1 - K_TRACK) / T["O"][T["idx"][days[0]]]
    open_, curve, trades = [], [], []
    for d in days:
        ti = T["idx"][d]
        for ev in by.get(d, []):
            if len(open_) >= SLOTS or any(p["sym"] == ev["sym"] for p in open_):
                continue
            o = outcomes(ev)
            if o is None:
                continue
            w = o[name]
            I = S[ev["sym"]]
            equity = units * T["O"][ti] + sum(p["sh"] * (px(p["sym"], d, "O") or p["entry"]) for p in open_)
            stake = equity / SLOTS
            u = min(units, stake / T["O"][ti])
            if u * T["O"][ti] < 25:
                continue
            units -= u
            got = u * T["O"][ti] * (1 - K_TRACK)
            entry = I["O"][ev["e"]]
            open_.append({"sym": ev["sym"], "sh": got / (entry * (1 + K_STOCK)), "entry": entry, "stake": u * T["O"][ti],
                          "exit_date": I["D"][w["exit_i"]], "exit_px": w["exit_px"], "why": w["why"], "entry_date": d})
        for p in [p for p in open_ if p["exit_date"] == d]:
            back = p["sh"] * p["exit_px"] * (1 - K_STOCK)
            units += back * (1 - K_TRACK) / T["C"][ti]
            trades.append({"sym": p["sym"], "ret": back * (1 - K_TRACK) / p["stake"] - 1, "why": p["why"]})
            open_.remove(p)
        curve.append(units * T["C"][ti] + sum(p["sh"] * (px(p["sym"], d) or p["entry"]) for p in open_))
    end = curve[-1] * (1 - K_TRACK)
    return {"total_pct": pct(end), "max_drawdown_pct": mdd(curve), "trades": len(trades),
            "stopped": sum(1 for t in trades if t["why"] == "stop"),
            "avg_trade_pct": 100 * st.mean([t["ret"] for t in trades]) if trades else 0.0}


def coverage(a, b):
    """How many reports passed the numbers but could not be used: no price history in our data (today's S&P 500
    list), or not a member on the report day."""
    by_numbers, no_prices, not_member = 0, 0, 0
    for day, rows in E.items():
        if not (a <= day <= b):
            continue
        for x in rows or []:
            sur, eps, fc = num(x.get("surprise")), num(x.get("eps")), num(x.get("epsForecast"))
            if sur is None or eps is None or fc is None or abs(fc) < 0.10 or sur < 10 or eps <= fc:
                continue
            by_numbers += 1
            s = str(x.get("symbol") or "").upper().replace(".", "-")
            if s not in S:
                no_prices += 1
            elif not member(s, day):
                not_member += 1
    return {"pass_the_numbers": by_numbers, "no_price_history": no_prices, "not_yet_a_member": not_member}


if __name__ == "__main__":
    if not E:
        sys.exit("no trader/research-data/earnings.json: run python3 research/earnings_fetch.py first")
    out = {"variants": [v[0] for v in VARIANTS], "periods": {}}
    for label, (a, b) in PERIODS:
        print("=" * 100)
        print("%s (%s to %s)" % (label, a, b))
        sl = signal_level(a, b)
        cv = coverage(a, b)
        print("  signals of the tested rule: %d (%d used; %d with under 15 days of bars before the buy). Median ATR: "
              "%.1f%% of the price, so a 3xATR stop sits about %.0f%% under the buy" % (
                  sl["signals"], sl["used"], sl["too_little_history"], sl["atr_pct_median"], 3 * sl["atr_pct_median"]))
        print("  reports passing the numbers on the whole Nasdaq calendar: %d; no price history in our data (not in "
              "TODAY's S&P 500): %d; in today's list but not yet a member: %d" % (
                  cv["pass_the_numbers"], cv["no_price_history"], cv["not_yet_a_member"]))
        print("  EVERY SIGNAL ON ITS OWN, after costs (0.2% each way):")
        for name in out["variants"]:
            v = sl["variants"][name]
            extra = "" if name == "none" else "   vs none %+.2f points a trade (95%% range %+.2f to %+.2f)" % (
                v["diff_vs_none_pct"], v["diff_ci95"][0], v["diff_ci95"][1])
            print("    %-20s avg %+.2f%%  median %+.2f%%  winners %.0f%%  stopped %4.1f%%  worst %+.1f%%  vs S&P 500 same "
                  "days %+.2f%%%s" % (name, v["avg_pct"], v["median_pct"], v["win_pct"], v["stopped_pct"], v["worst_pct"],
                                      v["avg_vs_spy_pct"], extra))
        base = pct(tracker_only(a, b)["end"])
        print("  THE 10-SLOT BOOK (money in the S&P 500 tracker between trades); tracker alone %+.1f%%:" % base)
        bk = {}
        for name in out["variants"]:
            r = book(a, b, name)
            bk[name] = r
            print("    %-20s total %+.1f%%  (minus the tracker %+.1f points)  max drawdown %.1f%%  trades %d, stopped %d, "
                  "avg %+.2f%% a trade" % (name, r["total_pct"], r["total_pct"] - base, r["max_drawdown_pct"], r["trades"],
                                           r["stopped"], r["avg_trade_pct"]))
        out["periods"][label] = {"range": [a, b], "signals": sl, "coverage": cv, "book": bk, "tracker_pct": base}
    with open(DATA + "pead_stop.json", "w") as fh:
        json.dump(out, fh, indent=1, default=float)
    print("\nSaved %spead_stop.json. Limits: today's S&P 500 list only (survivorship), surprise figures as Nasdaq shows "
          "them now, overlapping trades (the 95%% ranges are too narrow), FX moves ignored." % DATA)
