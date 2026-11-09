"""Post-earnings drift on top of an S&P 500 tracker, Trading 212 GBP-account costs.

Money sits in the tracker. When a qualifying earnings surprise appears and a slot is free, the
tracker is sold for one slot (equity / slots) and the stock bought at the open of the 2nd trading
day after the report; 20 trading days later the stock is sold at the close and the tracker bought back.

Costs: the tracker is the GBP-listed UCITS version (no FX fee), so its legs pay slippage only (0.05%);
the US stock legs pay the 0.15% FX fee plus 0.05% slippage. SPY's total return stands in for the tracker.
Universe: today's S&P 500, each stock only from the day it joined. FX moves are ignored.

Pre-registered choices, picked on 2019-2023 by excess return over the tracker, then run ONCE on 2024-2026:
  ranking  A  % surprise, forecast at least $0.50 a share
           B  surprise / price, (eps - forecast) / open price
           C  B, only stocks in the top half of S&P members by 20-day dollar volume that day
  slots    5, 10, 15
All need a surprise of at least 10% on a forecast of at least $0.10, and a beat (eps > forecast).
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _data import DATA
import os, random, statistics as st, sys, json

HERE = os.path.dirname(os.path.abspath(__file__))
src = open(os.path.join(HERE, "three_strategies.py")).read().split('if __name__ == "__main__":')[0]
exec(src)


def member(s, d):                       # S&P members only, from the day they joined (no hindsight names)
    a = added.get(s)
    return bool(a) and s not in BENCH and a[:10] <= d


K_STOCK = 0.0015 + 0.0005
K_TRACK = 0.0005
HOLD = 20
TR = "SPY"
RANKINGS = ("A", "B", "C")
SLOT_CHOICES = (5, 10, 15)
RUNS = 100

# 20-day average dollar volume, and the members' median of it each day (for ranking C)
for s, I in S.items():
    dv = [c * v for c, v in zip(I["C"], I["V"])]
    pre = [0.0]
    for x in dv: pre.append(pre[-1] + x)
    I["dv20"] = [(pre[i + 1] - pre[i - 19]) / 20 if i >= 19 else None for i in range(len(dv))]
_median_dv = {}


def median_dv(d):
    if d not in _median_dv:
        vals = [S[s]["dv20"][S[s]["idx"][d]] for s in S if member(s, d) and S[s]["idx"].get(d) is not None
                and S[s]["dv20"][S[s]["idx"][d]] is not None]
        _median_dv[d] = st.median(vals) if vals else 0
    return _median_dv[d]


def events(a, b):
    """Every usable report in [a, b]: (report day, symbol, % surprise, eps, forecast, entry index info)."""
    out = []
    for day, rows in E.items():
        if not (a <= day <= b) or day not in CI: continue
        ci = CI[day]
        if ci + 2 + HOLD >= len(CAL): continue
        ed = CAL[ci + 2]
        if ed > b: continue
        for x in rows or []:
            s = str(x.get("symbol") or "").upper().replace(".", "-")
            if s not in S or not member(s, day): continue
            sur, eps, fc = num(x.get("surprise")), num(x.get("eps")), num(x.get("epsForecast"))
            if sur is None or eps is None or fc is None or abs(fc) < 0.10: continue
            I = S[s]; e = I["idx"].get(ed)
            if e is None or e + HOLD - 1 >= len(I["C"]) or I["D"][e + HOLD - 1] > b: continue
            out.append({"day": day, "sym": s, "sur": sur, "eps": eps, "fc": fc, "e": e, "entry_date": ed,
                        "exit_date": I["D"][e + HOLD - 1]})
    return out


def qualifies(ev, ranking):
    if ev["sur"] < 10 or ev["eps"] <= ev["fc"]: return False
    if ranking == "A" and ev["fc"] < 0.50: return False
    if ranking == "C":
        I = S[ev["sym"]]; i = I["idx"].get(ev["day"])
        if i is None or I["dv20"][i] is None or I["dv20"][i] < median_dv(ev["day"]): return False
    return True


def score(ev, ranking):
    if ranking == "A": return ev["sur"]
    o = S[ev["sym"]]["O"][ev["e"]]
    return (ev["eps"] - ev["fc"]) / o


def run(a, b, picks, slots):
    """picks: events in the order they should be preferred on a given entry day (best first)."""
    by = {}
    for ev in picks: by.setdefault(ev["entry_date"], []).append(ev)
    days = [d for d in CAL if a <= d <= b]
    T = S[TR]
    units = CAPITAL * (1 - K_TRACK) / T["O"][T["idx"][days[0]]]
    cash, open_, curve, trades = 0.0, [], [], []
    for d in days:
        ti = T["idx"][d]
        # entries at the open
        for ev in by.get(d, []):
            if len(open_) >= slots or any(p["sym"] == ev["sym"] for p in open_): continue
            I = S[ev["sym"]]
            equity = units * T["O"][ti] + cash + sum(p["sh"] * I2["O"][I2["idx"][d]] if (I2 := S[p["sym"]])["idx"].get(d) is not None
                                                      else p["sh"] * p["entry"] for p in open_)
            stake = equity / slots
            u = min(units, stake / T["O"][ti])
            if u * T["O"][ti] < 25: continue
            units -= u
            got = u * T["O"][ti] * (1 - K_TRACK)
            o = I["O"][ev["e"]]
            sh = got / (o * (1 + K_STOCK))
            open_.append({"sym": ev["sym"], "sh": sh, "entry": o, "exit_i": ev["e"] + HOLD - 1, "stake": u * T["O"][ti],
                          "entry_date": d})
        # exits at the close
        for p in [p for p in open_ if S[p["sym"]]["D"][p["exit_i"]] == d]:
            I = S[p["sym"]]
            back = p["sh"] * I["C"][p["exit_i"]] * (1 - K_STOCK)
            units += back * (1 - K_TRACK) / T["C"][ti]
            trades.append({"sym": p["sym"], "ret": back * (1 - K_TRACK) / p["stake"] - 1,
                           "vs_tracker": back * (1 - K_TRACK) / p["stake"] - T["C"][ti] / T["O"][T["idx"][p["entry_date"]]],
                           "entry_date": p["entry_date"], "exit_date": d})
            open_.remove(p)
        curve.append(units * T["C"][ti] + cash + sum(p["sh"] * (px(p["sym"], d) or p["entry"]) for p in open_))
    end = curve[-1] * (1 - K_TRACK)
    return {"end": end, "curve": curve, "trades": trades}


def tracker_only(a, b):
    T = S[TR]; days = [d for d in CAL if a <= d <= b]
    units = CAPITAL * (1 - K_TRACK) / T["O"][T["idx"][days[0]]]
    curve = [units * T["C"][T["idx"][d]] for d in days]
    return {"end": curve[-1] * (1 - K_TRACK), "curve": curve}


def strategy(a, b, ranking, slots):
    picks = [ev for ev in events(a, b) if qualifies(ev, ranking)]
    picks.sort(key=lambda ev: -score(ev, ranking))
    return run(a, b, picks, slots), len(picks)


def random_runs(a, b, n_signals, slots, runs=RUNS):
    pool = events(a, b)
    out = []
    for i in range(runs):
        r = random.Random(i)
        pick = r.sample(pool, min(len(pool), n_signals))
        r.shuffle(pick)
        out.append(100 * (run(a, b, pick, slots)["end"] / CAPITAL - 1))
    return out


def pct(x): return 100 * (x / CAPITAL - 1)


def report(tag, a, b, ranking, slots):
    R, n = strategy(a, b, ranking, slots)
    base = tracker_only(a, b)
    rnd = random_runs(a, b, n, slots)
    tot, btot = pct(R["end"]), pct(base["end"])
    rets = [t["ret"] for t in R["trades"]]
    vs = [t["vs_tracker"] for t in R["trades"]]
    print("%-6s %s..%s  ranking %s, %d slots" % (tag, a, b, ranking, slots))
    print("   strategy: total %+.1f%% (%+.1f%%/yr), max drawdown %.1f%%; %d earnings trades (%.1f/month), "
          "avg %+.2f%% a trade after all costs, avg %+.2f%% vs the tracker over the same days, win vs tracker %.0f%%" % (
              tot, cagr(CAPITAL, R["end"], a, b), mdd(R["curve"]), len(rets), len(rets) / months_between(a, b),
              100 * st.mean(rets) if rets else 0, 100 * st.mean(vs) if vs else 0,
              100 * sum(1 for x in vs if x > 0) / max(1, len(vs))))
    print("   tracker only: total %+.1f%% (%+.1f%%/yr), max drawdown %.1f%%  ->  strategy minus tracker %+.1f points" % (
        btot, cagr(CAPITAL, base["end"], a, b), mdd(base["curve"]), tot - btot))
    print("   random reports, same rules (%d runs): median %+.1f%%, 5-95%% %+.1f%% to %+.1f%%; strategy beats %d%%" % (
        len(rnd), st.median(rnd), sorted(rnd)[5], sorted(rnd)[94], round(100 * sum(1 for x in rnd if tot > x) / len(rnd))))
    return tot - btot


if __name__ == "__main__":
    print("DEVELOPMENT 2019-2023: every pre-registered combination (excess = strategy minus tracker, points)")
    base = pct(tracker_only(*DEV)["end"])
    grid = []
    for ranking in RANKINGS:
        for slots in SLOT_CHOICES:
            R, n = strategy(DEV[0], DEV[1], ranking, slots)
            grid.append((pct(R["end"]) - base, ranking, slots))
            vs = [t["vs_tracker"] for t in R["trades"]]
            print("   %s %2d slots: %4d signals, %3d trades, total %+.1f%% (tracker %+.1f%%), excess %+.1f, max drawdown %.1f%%, "
                  "avg %+.2f%% a trade vs the tracker" % (ranking, slots, n, len(R["trades"]), pct(R["end"]), base,
                                                         pct(R["end"]) - base, mdd(R["curve"]), 100 * st.mean(vs) if vs else 0))
    best = max(grid)
    print("CHOSEN on 2019-2023: ranking %s, %d slots (excess %+.1f points)\n" % (best[1], best[2], best[0]))
    if "--dev-only" in sys.argv:
        sys.exit(0)
    report("DEV", DEV[0], DEV[1], best[1], best[2])
    print()
    print("UNSEEN, run once:")
    report("UNSEEN", TEST[0], TEST[1], best[1], best[2])
