"""Kronos vs ATR as risk forecasts, from the forecasts cached by research/kronos_backtest.py. Research only.
Run: python3 research/kronos_risk_check.py trader/research-data"""
import json, sys, statistics as st, math, datetime as dt
D = sys.argv[1]
prices = json.load(open(D + "/prices.json"))
cache = [json.loads(l) for l in open(D + "/kronos_cache.jsonl") if l.strip()]
def day(ts): return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%d")
idx = {}
for s in set(r["symbol"] for r in cache):
    bars = prices[s]; idx[s] = ({day(b[0]): i for i, b in enumerate(bars)}, bars)
def ranks(v):
    o = sorted(range(len(v)), key=lambda i: v[i]); r = [0]*len(v); i = 0
    while i < len(v):
        j = i
        while j + 1 < len(v) and v[o[j+1]] == v[o[i]]: j += 1
        for k in range(i, j+1): r[o[k]] = (i + j) / 2
        i = j + 1
    return r
def corr(a, b):
    ma, mb = st.mean(a), st.mean(b)
    num = sum((x-ma)*(y-mb) for x, y in zip(a, b)); den = math.sqrt(sum((x-ma)**2 for x in a)*sum((y-mb)**2 for y in b))
    return num/den if den else 0
def sp(a, b): return corr(ranks(a), ranks(b))
rows = []
for r in cache:
    m, bars = idx[r["symbol"]]; i = m.get(r["date"])
    if i is None or i + 20 >= len(bars) or i < 30: continue
    c = [b[4] for b in bars]; h = [b[2] for b in bars]; l = [b[3] for b in bars]; o = [b[1] for b in bars]
    tr = [max(h[k]-l[k], abs(h[k]-c[k-1]), abs(l[k]-c[k-1])) for k in range(i-13, i+1)]
    atr = st.mean(tr) / c[i]
    lr = [math.log(c[k]/c[k-1]) for k in range(i-19, i+1)]
    hv = st.pstdev(lr)
    pr = r["path_returns"]
    ks = st.pstdev(pr); kr = max(pr) - min(pr)
    fut = [math.log(c[k]/c[k-1]) for k in range(i+1, i+21)]
    rv = st.pstdev(fut)
    entry = o[i+1]
    r20 = abs(c[i+20]/entry - 1)
    mae = (entry - min(l[i+1:i+21])) / entry
    rows.append(dict(m=r["date"][:7], s=r["symbol"], atr=atr, hv=hv, ks=ks, kr=kr, rv=rv, r20=r20, mae=mae,
                     stop=(min(l[i+1:i+21]) <= entry*(1-3*atr))))
months = sorted(set(x["m"] for x in rows))
print("rows", len(rows), "months", len(months))
preds = ["atr", "hv", "ks", "kr"]; targs = ["rv", "mae", "r20"]
out = {}
for t in targs:
    for p in preds:
        cs = [sp([x[p] for x in rows if x["m"] == mo], [x[t] for x in rows if x["m"] == mo]) for mo in months]
        mean = st.mean(cs); tt = mean / (st.stdev(cs)/math.sqrt(len(cs)))
        out[(t, p)] = (mean, tt, sum(c > 0 for c in cs))
        print("%-4s by %-3s  avg monthly rank corr %+.3f  t %5.1f  positive %d/%d" % (t, p, mean, tt, out[(t,p)][2], len(cs)))
# does Kronos add anything beyond ATR? per month: rank-regress target on atr rank and ks rank
print()
for t in targs:
    bk, ba = [], []
    for mo in months:
        xs = [x for x in rows if x["m"] == mo]
        a = ranks([x["atr"] for x in xs]); k = ranks([x["ks"] for x in xs]); y = ranks([x[t] for x in xs])
        n = len(xs); ma, mk, my = st.mean(a), st.mean(k), st.mean(y)
        saa = sum((u-ma)**2 for u in a); skk = sum((u-mk)**2 for u in k); sak = sum((u-ma)*(v-mk) for u, v in zip(a, k))
        say = sum((u-ma)*(v-my) for u, v in zip(a, y)); sky = sum((u-mk)*(v-my) for u, v in zip(k, y))
        det = saa*skk - sak*sak
        ba.append((skk*say - sak*sky)/det); bk.append((saa*sky - sak*say)/det)
    for nm, b in (("ATR", ba), ("Kronos", bk)):
        print("%-4s together: %-6s weight %+.3f  t %5.1f" % (t, nm, st.mean(b), st.mean(b)/(st.stdev(b)/math.sqrt(len(b)))))
print()
print("3xATR stop touched within 20 days: %.1f%% of %d holdings" % (100*st.mean([x['stop'] for x in rows]), len(rows)))
# same-budget Kronos stop: scale ks so average distance equals average 3xATR distance
scale = st.mean([3*x["atr"] for x in rows]) / st.mean([x["ks"] for x in rows])
kh = st.mean([x["mae"] >= scale*x["ks"] for x in rows])
print("Kronos-sized stop, same average distance (%.1f%%): touched %.1f%%" % (100*st.mean([3*x['atr'] for x in rows]), 100*kh))
hscale = st.mean([3*x["atr"] for x in rows]) / st.mean([x["hv"] for x in rows])
print("20-day-volatility stop, same average distance: touched %.1f%%" % (100*st.mean([x["mae"] >= hscale*x["hv"] for x in rows])))
print("flat stop, same average distance: touched %.1f%%" % (100*st.mean([x["mae"] >= st.mean([3*y['atr'] for y in rows]) for x in rows])))
print()
cost = 0.002
for mult in (2, 3, 4, 5, None):
    res = []
    for r in cache:
        m, bars = idx[r["symbol"]]; i = m.get(r["date"])
        c = [b[4] for b in bars]; h = [b[2] for b in bars]; l = [b[3] for b in bars]; o = [b[1] for b in bars]
        tr = [max(h[k]-l[k], abs(h[k]-c[k-1]), abs(l[k]-c[k-1])) for k in range(i-13, i+1)]
        atr = st.mean(tr)
        entry = o[i+1]; out = c[i+20]
        if mult:
            stop = c[i] - mult*atr
            if entry <= stop: out = entry
            else:
                for k in range(i+1, i+21):
                    if o[k] <= stop: out = o[k]; break
                    if l[k] <= stop: out = stop; break
        res.append(out/entry*(1-cost)/(1+cost) - 1)
    print("stop %-4s avg holding %+.2f%%  median %+.2f%%  worst %+.1f%%" % (("%dxATR" % mult) if mult else "none", 100*st.mean(res), 100*st.median(res), 100*min(res)))
