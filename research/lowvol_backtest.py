"""Run: python3 research/lowvol_backtest.py trader/research-data
Low-volatility buy-and-hold backtest. Design fixed before running:
universe = today's S&P 500 members, each counted from its 'Date added' (survivorship bias remains);
monthly on first trading day D: rank by std of the last 252 daily returns; hold the N lowest, equal weight,
from the open of D+1 to the open of the next month's D+1. Costs 0.2% of traded value per side (0.15% FX +
0.05% slippage), charged on turnover. N = 20 is the main test; 10 and 50 are shown as sensitivity only.
Develop 2019-2023, test 2024-01 to 2026-10. Benchmarks: SPY and RSP (equal-weight S&P, no survivorship),
bought once at 0.05% a leg; equal weight of all eligible members; 500 random 20-stock portfolios."""
import json, sys, csv, math, random, statistics as st, datetime as dt
D = sys.argv[1]
P = json.load(open(D + "/prices.json"))
added = {r["Symbol"].replace(".", "-"): r["Date added"] for r in csv.DictReader(open(D + "/sp500.csv"))}
day = lambda t: dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%d")
cal = [day(b[0]) for b in P["SPY"]]
pos = {s: {day(b[0]): i for i, b in enumerate(P[s])} for s in P}
def bar(s, d):
    i = pos[s].get(d); return None if i is None else P[s][i]
firsts = []
for i, d in enumerate(cal):
    if i and d[:7] != cal[i-1][:7]: firsts.append(i)
months = [i for i in firsts if "2019-01" <= cal[i][:7] <= "2026-10"]
def vol(s, d):
    i = pos[s].get(d)
    if i is None or i < 253: return None
    c = [b[4] for b in P[s][i-252:i+1]]
    if any(x <= 0 for x in c): return None
    return st.pstdev([math.log(c[k]/c[k-1]) for k in range(1, len(c))])
def eligible(d, d1, end):
    out = []
    for s in added:
        if s not in P or s in ("SPY", "RSP", "QQQ") or added[s] > d: continue
        if bar(s, d1) is None or bar(s, end) is None: continue
        v = vol(s, d)
        if v is not None: out.append((v, s))
    return out
periods = []
for k, i in enumerate(months):
    nxt = firsts[firsts.index(i) + 1]
    d, d1, end = cal[i], cal[i+1], cal[nxt+1]
    el = eligible(d, d1, end)
    rets = {s: bar(s, end)[1] / bar(s, d1)[1] - 1 for _, s in el}
    periods.append(dict(m=d[:7], el=sorted(el), rets=rets, d1=d1, end=end))
COST = 0.002
def run(pick):
    held, out, gross = {}, [], []
    for p in periods:
        names = pick(p)
        w = {s: 1/len(names) for s in names}
        turn = sum(abs(w.get(s, 0) - held.get(s, 0)) for s in set(w) | set(held))
        g = sum(w[s] * p["rets"][s] for s in names)
        out.append((1 + g) * (1 - COST * turn) - 1); gross.append(g)
        tot = 1 + g
        held = {s: w[s] * (1 + p["rets"][s]) / tot for s in names}
    return out, gross
def etf(sym):
    out = []
    for k, p in enumerate(periods):
        r = bar(sym, p["end"])[1] / bar(sym, p["d1"])[1] - 1
        if k == 0: r = (1 + r) * (1 - 0.0005) - 1
        if k == len(periods) - 1: r = (1 + r) * (1 - 0.0005) - 1
        out.append(r)
    return out
def stats(r, sel):
    rr = [x for x, p in zip(r, periods) if sel(p["m"])]
    eq, peak, dd = 1, 1, 0
    for x in rr:
        eq *= 1 + x; peak = max(peak, eq); dd = max(dd, 1 - eq/peak)
    yrs = len(rr) / 12
    return dict(total=eq - 1, cagr=eq ** (1/yrs) - 1, dd=dd, vol=st.pstdev(rr) * math.sqrt(12), worst=min(rr))
def yr(r, y):
    eq = 1
    for x, p in zip(r, periods):
        if p["m"].startswith(y): eq *= 1 + x
    return eq - 1
S = {}
for n in (20, 10, 50):
    S["LowVol %d" % n] = run(lambda p, n=n: [s for _, s in p["el"][:n]])
S["Equal weight, all members"] = run(lambda p: [s for _, s in p["el"]])
SPY, RSP = etf("SPY"), etf("RSP")
rng = random.Random(42)
rand_tot = {"dev": [], "test": [], "all": []}
for _ in range(500):
    r, g = run(lambda p: rng.sample([s for _, s in p["el"]], 20))
    for key, sel in (("dev", lambda m: m < "2024"), ("test", lambda m: m >= "2024"), ("all", lambda m: True)):
        rand_tot[key].append(stats(r, sel)["total"])
res = {}
for label, sel in (("Develop 2019-2023", lambda m: m < "2024"), ("Test 2024-01 to 2026-10", lambda m: m >= "2024"), ("Whole 2019-01 to 2026-10", lambda m: True)):
    key = "dev" if label[0] == "D" else "test" if label[0] == "T" else "all"
    print("\n" + label)
    print("%-27s %8s %7s %8s %7s %7s" % ("", "total", "a year", "max fall", "vol", "worst m"))
    rows = [(k, v[0]) for k, v in S.items()] + [("SPY", SPY), ("RSP (equal-weight S&P)", RSP)]
    for k, r in rows:
        s = stats(r, sel); res.setdefault(label, {})[k] = s
        print("%-27s %+7.1f%% %+6.1f%% %7.1f%% %6.1f%% %+6.1f%%" % (k, 100*s["total"], 100*s["cagr"], 100*s["dd"], 100*s["vol"], 100*s["worst"]))
    lv = stats(S["LowVol 20"][0], sel)["total"]; rt = sorted(rand_tot[key])
    print("LowVol 20 beats %.0f%% of 500 random 20-stock portfolios (median random %+.1f%%)" % (100*sum(x < lv for x in rt)/len(rt), 100*st.median(rt)))
print("\nCalendar 2022 (S&P fell): LowVol 20 %+.1f%%, SPY %+.1f%%, RSP %+.1f%%, all members %+.1f%%" % (
    100*yr(S["LowVol 20"][0], "2022"), 100*yr(SPY, "2022"), 100*yr(RSP, "2022"), 100*yr(S["Equal weight, all members"][0], "2022")))
print("Calendar 2020: LowVol 20 %+.1f%%, SPY %+.1f%%" % (100*yr(S["LowVol 20"][0], "2020"), 100*yr(SPY, "2020")))
lv = S["LowVol 20"][0]
turn_proxy = st.mean([abs(a-b) for a, b in zip(lv, S["LowVol 20"][1])])
print("LowVol 20 average monthly cost drag %.2f%%; stocks in the universe per month %d-%d" % (100*turn_proxy, min(len(p["el"]) for p in periods), max(len(p["el"]) for p in periods)))
print("Latest LowVol 20 list (%s): %s" % (periods[-1]["m"], " ".join(s for _, s in periods[-1]["el"][:20])))
