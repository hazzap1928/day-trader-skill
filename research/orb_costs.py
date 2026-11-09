import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _data import DATA
import json, statistics as st, sys, collections, math
d = json.load(open(DATA + (sys.argv[1] if len(sys.argv) > 1 else "orb_trades.json")))
FX = json.load(open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "trader/fx.json")))["usd_per_gbp"]
FEE = 0.0015
MAX_LOSS, MAX_POS = 30.0, 1875.0          # GBP: config max loss per trade, 75% of the 2,500 budget

def costed(t, slip):
    e, s, R = t["entry"], t["stop"], t["R"]
    risk = e - s
    x = e + R * risk
    shares = min(MAX_LOSS * FX / risk, MAX_POS * FX / e)
    r_gbp = shares * risk / FX
    c = (FEE + slip) * (e + x)
    return {"R": R, "R_net": R - c / risk, "gbp": R * r_gbp, "gbp_net": R * r_gbp - shares * c / FX,
            "r_gbp": r_gbp, "risk_pct": 100 * risk / e, "cost_R": c / risk, "date": t["date"], "time": t["time"],
            "sym": t["symbol"], "outcome": t["outcome"]}

def mean(xs): return st.mean(xs) if xs else float("nan")
def se(xs): return st.stdev(xs) / math.sqrt(len(xs)) if len(xs) > 1 else float("nan")

T = d["trades"]
days = sorted({t["date"] for t in T})
print("FX %.4f; %d trades, %d tickers, %d sessions (%s to %s)" % (FX, len(T), len({t['symbol'] for t in T}), len(days), days[0], days[-1]))
print("outcomes:", collections.Counter(t["outcome"] for t in T))
print()
print("ALL TRADES            avg R    +-se   avg GBP   cost R  cost GBP  green%")
base = [costed(t, 0) for t in T]
print("before costs        %+.3f  %.3f  %+7.2f" % (mean([c["R"] for c in base]), se([c["R"] for c in base]), mean([c["gbp"] for c in base])))
for slip in (0.0, 0.0002, 0.0005, 0.0010):
    C = [costed(t, slip) for t in T]
    print("fee + slip %.2f%%   %+.3f  %.3f  %+7.2f   %.3f   %6.2f   %4.1f" % (
        100 * slip, mean([c["R_net"] for c in C]), se([c["R_net"] for c in C]), mean([c["gbp_net"] for c in C]),
        mean([c["cost_R"] for c in C]), mean([c["gbp"] - c["gbp_net"] for c in C]),
        100 * sum(1 for c in C if c["R_net"] > 0) / len(C)))
print("avg 1R in GBP: %.2f (position cap binds when the stop is tight)" % mean([c["r_gbp"] for c in base]))

print()
print("BY STOP DISTANCE (risk as %% of price), fee + 0.05%% slip")
C = [costed(t, 0.0005) for t in T]
edges = [0, 0.25, 0.5, 0.75, 1.0, 1.5, 100]
for lo, hi in zip(edges, edges[1:]):
    g = [c for c in C if lo <= c["risk_pct"] < hi]
    if not g: continue
    print("  %4.2f-%5.2f%%  n=%4d (%4.1f/day)  R before %+.3f  cost %.3fR  R after %+.3f (se %.3f)  GBP after %+6.2f" % (
        lo, hi, len(g), len(g) / len(days), mean([c["R"] for c in g]), mean([c["cost_R"] for c in g]),
        mean([c["R_net"] for c in g]), se([c["R_net"] for c in g]), mean([c["gbp_net"] for c in g])))

print()
print("TRADES PER DAY")
per_day = collections.Counter(c["date"] for c in C)
print("  raw triggers across the list: avg %.1f a day (min %d, max %d)" % (mean(list(per_day.values())), min(per_day.values()), max(per_day.values())))
def by_time(c):
    h, m = c["time"].split()[0].split(":"); pm = c["time"].endswith("PM")
    return (int(h) % 12 + (12 if pm else 0)) * 60 + int(m)
for min_risk in (0.0, 0.5, 0.75, 1.0):
    for cap in (5,):
        daily = []
        for day in days:
            g = sorted([c for c in C if c["date"] == day and c["risk_pct"] >= min_risk], key=by_time)[:cap]
            daily.append(g)
        taken = [c for g in daily for c in g]
        n_eligible = sum(1 for c in C if c["risk_pct"] >= min_risk) / len(days)
        print("  stop >= %.2f%% of price: %4.1f eligible a day; first %d a day -> %.1f taken/day, avg R after %+.3f (se %.3f), "
              "GBP/day after %+6.2f, before %+6.2f" % (
                  min_risk, n_eligible, cap, len(taken) / len(days), mean([c["R_net"] for c in taken]),
                  se([c["R_net"] for c in taken]), sum(c["gbp_net"] for c in taken) / len(days),
                  sum(c["gbp"] for c in taken) / len(days)))
