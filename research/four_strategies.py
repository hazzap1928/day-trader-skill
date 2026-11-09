import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _data import DATA
import json, math, random, statistics as st, sys, collections, datetime
SP = DATA
FX = 1.3372
CAPITAL, RISK, CAP = 2250.0, 30.0, 1875.0          # GBP: investable, max loss a trade, max position
SLIP, FEE = 0.0005, 0.0015
COSTS = {"USD balance": SLIP, "GBP account (API today)": FEE + SLIP}
daily = json.load(open(SP + "daily5y.json"))

def d_of(ts): return datetime.datetime.utcfromtimestamp(ts).date().isoformat()

# ------------------------------------------------------------ indicators per symbol
IND = {}
for s, rows in daily.items():
    D = [d_of(r[0]) for r in rows]; O = [r[1] for r in rows]; H = [r[2] for r in rows]; L = [r[3] for r in rows]; C = [r[4] for r in rows]
    n = len(C)
    def sma(x, k, i): return sum(x[i - k + 1:i + 1]) / k if i >= k - 1 else None
    tr = [H[0] - L[0]] + [max(H[i] - L[i], abs(H[i] - C[i - 1]), abs(L[i] - C[i - 1])) for i in range(1, n)]
    atr = [sma(tr, 20, i) for i in range(n)]
    s200 = [sma(C, 200, i) for i in range(n)]; s50 = [sma(C, 50, i) for i in range(n)]; s5 = [sma(C, 5, i) for i in range(n)]
    # RSI(2), Wilder
    rsi = [None] * n; ag = al = None
    for i in range(1, n):
        g, l_ = max(C[i] - C[i - 1], 0), max(C[i - 1] - C[i], 0)
        if i == 2: ag, al = (max(C[1]-C[0],0) + g) / 2, (max(C[0]-C[1],0) + l_) / 2
        elif i > 2: ag, al = (ag * 1 + g) / 2, (al * 1 + l_) / 2
        if i >= 2: rsi[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    hi55 = [max(C[i - 55:i]) if i >= 55 else None for i in range(n)]
    lo20 = [min(L[i - 20:i]) if i >= 20 else None for i in range(n)]
    IND[s] = dict(D=D, O=O, H=H, L=L, C=C, atr=atr, s200=s200, s50=s50, s5=s5, rsi=rsi, hi55=hi55, lo20=lo20,
                  idx={d: i for i, d in enumerate(D)})

# ------------------------------------------------------------ exits for the daily strategies
def exit_donchian(I, i, entry, stop):
    """Entered at the open of bar i. Out: stop touched (at the stop, or the open if it gaps under), a close under the
    20-day low (next open), or the close of the 20th day."""
    n = len(I["C"])
    for j in range(i, min(n, i + 20)):
        if I["L"][j] <= stop:
            return j, min(stop, I["O"][j]) if j > i else stop, "stop"
        if j > i and I["lo20"][j] is not None and I["C"][j] < I["lo20"][j]:
            if j + 1 < n: return j + 1, I["O"][j + 1], "20-day low"
            return j, I["C"][j], "20-day low"
    j = min(n - 1, i + 19)
    return j, I["C"][j], "20 days"

def exit_rsi2(I, i, entry, stop):
    n = len(I["C"])
    for j in range(i, min(n, i + 10)):
        if I["L"][j] <= stop:
            return j, min(stop, I["O"][j]) if j > i else stop, "stop"
        if I["s5"][j] is not None and I["C"][j] > I["s5"][j]:
            return j, I["C"][j], "back above 5-day average"
    j = min(n - 1, i + 9)
    return j, I["C"][j], "10 days"

def daily_trades(kind, rand=None, rng=None, count=None):
    """All signals, one open trade per symbol at a time. rand: random entries in an uptrend instead."""
    out = []
    for s, I in IND.items():
        n = len(I["C"]); i = 210; busy_until = -1
        cands = []
        for k in range(210, n - 1):
            if I["s200"][k] is None or I["atr"][k] is None: continue
            up = I["C"][k] > I["s200"][k]
            if rand:
                if up: cands.append(k)
                continue
            if kind == "donchian" and up and I["hi55"][k] is not None and I["C"][k] > I["hi55"][k]:
                cands.append(k)
            if kind == "rsi2" and up and I["rsi"][k] is not None and I["rsi"][k] < 10:
                cands.append(k)
        if rand:
            cands = sorted(rng.sample(cands, min(len(cands), count[s])))
        for k in cands:
            e_i = k + 1
            if e_i <= busy_until: continue
            entry = I["O"][e_i]
            stop = entry - (2 if kind == "donchian" else 3) * I["atr"][k]
            if stop <= 0: continue
            j, x, why = (exit_donchian if kind == "donchian" else exit_rsi2)(I, e_i, entry, stop)
            busy_until = j
            rank = (I["C"][k] / I["C"][k - 55] - 1) if kind == "donchian" else -(I["rsi"][k] or 0)
            out.append({"sym": s, "entry_date": I["D"][e_i], "exit_date": I["D"][j], "entry": entry, "stop": stop,
                        "exit": x, "why": why, "R": (x - entry) / (entry - stop), "days": j - e_i + 1, "rank": rank})
    return out

def intraday(path, trend=False):
    T = json.load(open(path))["trades"]
    out = []
    for t in T:
        if trend:
            I = IND.get(t["symbol"]); i = I["idx"].get(t["date"]) if I else None
            if i is None or i < 1 or I["s50"][i - 1] is None or not I["C"][i - 1] > I["s50"][i - 1]:
                continue
        risk = t["entry"] - t["stop"]
        out.append({"sym": t["symbol"], "entry_date": t["date"], "exit_date": t["date"], "entry": t["entry"],
                    "stop": t["stop"], "exit": t["entry"] + t["R"] * risk, "why": t["outcome"], "R": t["R"],
                    "days": 1, "time": t["time"]})
    return out

# ------------------------------------------------------------ costs and stats
def net_R(t, k): return t["R"] - k * (t["entry"] + t["exit"]) / (t["entry"] - t["stop"])
def sized(t):
    risk = t["entry"] - t["stop"]
    return min(RISK * FX / risk, CAP * FX / t["entry"])
def gbp(t, k):
    sh = sized(t); return sh * ((t["exit"] - t["entry"]) - k * (t["entry"] + t["exit"])) / FX

def stats(T, k):
    r = [net_R(t, k) for t in T]
    m = st.mean(r); se = st.stdev(r) / math.sqrt(len(r))
    return m, se, m / se if se else 0

def portfolio(T, k, per_day_cap=None, intraday_mode=False):
    """Capital-limited: entries in date order (best rank first), sized min(risk, cap, cash). Returns metrics."""
    by_entry = collections.defaultdict(list)
    for t in T: by_entry[t["entry_date"]].append(t)
    days = sorted({d for I in IND.values() for d in I["D"]} | set(by_entry))
    first = min(by_entry); last = max(t["exit_date"] for t in T)
    days = [d for d in days if first <= d <= last]
    cash, open_, eq, taken = CAPITAL, [], [], []
    for d in days:
        # exits on d (intraday trades exit the same day)
        if not intraday_mode:
            for p in [p for p in open_ if p["t"]["exit_date"] <= d]:
                cash += p["sh"] * p["t"]["exit"] * (1 - k) / FX; open_.remove(p); taken.append(p)
        cands = by_entry.get(d, [])
        cands = sorted(cands, key=(lambda t: t.get("time", "")) if intraday_mode else (lambda t: -t["rank"]))
        if per_day_cap: cands = cands[:per_day_cap]
        for t in cands:
            sh = sized(t)
            cost = sh * t["entry"] * (1 + k) / FX
            if intraday_mode:
                cost = min(cost, cash)  # one at a time is not modelled; each trade gets the capital it needs
                sh = cost / (t["entry"] * (1 + k) / FX)
                cash += sh * t["exit"] * (1 - k) / FX - cost
                taken.append({"t": t, "sh": sh, "cost": cost})
                continue
            if any(p["t"]["sym"] == t["sym"] for p in open_) or cost > cash: continue
            cash -= cost; open_.append({"t": t, "sh": sh, "cost": cost})
        # mark to market
        mv = 0.0
        for p in open_:
            I = IND[p["t"]["sym"]]; i = I["idx"].get(d)
            px = I["C"][i] if i is not None else p["t"]["entry"]
            mv += p["sh"] * px / FX
        eq.append((d, cash + mv))
    for p in open_:
        cash += p["sh"] * p["t"]["exit"] * (1 - k) / FX; taken.append(p)
    pl = [p["sh"] * p["t"]["exit"] * (1 - k) / FX - p["cost"] for p in taken]
    order = sorted(range(len(taken)), key=lambda i: taken[i]["t"]["exit_date"])
    streak = worst = 0
    for i in order:
        streak = streak + 1 if pl[i] < 0 else 0; worst = max(worst, streak)
    peak, mdd = -1e18, 0.0
    for _, v in eq:
        peak = max(peak, v); mdd = max(mdd, peak - v)
    months = max(1.0, (datetime.date.fromisoformat(last) - datetime.date.fromisoformat(first)).days / 30.44)
    rs = [(p["t"]["R"] - k * (p["t"]["entry"] + p["t"]["exit"]) / (p["t"]["entry"] - p["t"]["stop"])) for p in taken]
    return {"trades": len(taken), "per_month": len(taken) / months, "avg_gbp": st.mean(pl), "avg_R": st.mean(rs),
            "win": 100.0 * sum(1 for x in pl if x > 0) / len(pl), "streak": worst, "mdd": mdd,
            "mdd_pct": 100 * mdd / CAPITAL, "total": sum(pl), "months": months, "first": first, "last": last}

def bh(syms, first, last):
    rets = []
    for s in syms:
        I = IND[s]; ids = [i for i, d in enumerate(I["D"]) if first <= d <= last]
        if ids: rets.append(I["C"][ids[-1]] / I["O"][ids[0]] - 1)
    return 100 * st.mean(rets)

# ------------------------------------------------------------ run
rng = random.Random(19)
S = {
    "1 ORB now (mid stop, 1.5R)": intraday(SP + "orb_trades.json"),
    "2 ORB variant (low stop, 2R, above 50-day avg)": intraday(SP + "orb_variant_trades.json", trend=True),
    "3 Trend swing (55-day high, 2xATR stop, 20-day low exit)": daily_trades("donchian"),
    "4 Pullback (RSI2<10 in uptrend, exit above 5-day avg)": daily_trades("rsi2"),
}
for name, T in S.items():
    print("=" * 100); print(name, "| %d signals, %s to %s" % (len(T), min(t["entry_date"] for t in T), max(t["exit_date"] for t in T)))
    print("  avg holding %.1f days; exits %s" % (st.mean(t["days"] for t in T), dict(collections.Counter(t["why"] for t in T).most_common(4))))
    print("  R before costs %+.3f" % st.mean(t["R"] for t in T))
    for cname, k in COSTS.items():
        m, se, tt = stats(T, k)
        print("  %-24s R after %+.3f (se %.3f, t %+.1f)  GBP a trade %+6.2f  green %4.1f%%" % (
            cname, m, se, tt, st.mean(gbp(t, k) for t in T), 100 * sum(1 for t in T if net_R(t, k) > 0) / len(T)))
    k = COSTS["USD balance"]
    if name[0] in "34":
        cnt = collections.Counter(t["sym"] for t in T)
        rmeans = []
        for _ in range(200):
            R = daily_trades(name[0] == "3" and "donchian" or "rsi2", rand=True, rng=rng, count=cnt)
            rmeans.append(st.mean(net_R(t, k) for t in R))
        m = stats(T, k)[0]
        print("  random entries, same exits, same tickers (200 runs): mean R after %+.3f (5-95%%: %+.3f to %+.3f); "
              "strategy beats %d%% of them" % (st.mean(rmeans), sorted(rmeans)[10], sorted(rmeans)[189],
                                              sum(1 for x in rmeans if m > x) / 2))
        half = sorted(t["entry_date"] for t in T)[len(T) // 2]
        a = [net_R(t, k) for t in T if t["entry_date"] < half]; b = [net_R(t, k) for t in T if t["entry_date"] >= half]
        print("  halves (USD costs): before %s %+.3f (n %d), after %+.3f (n %d)" % (half, st.mean(a), len(a), st.mean(b), len(b)))
    for cname, kk in COSTS.items():
        P = portfolio(T, kk, per_day_cap=(5 if name[0] in "12" else None), intraday_mode=name[0] in "12")
        print("  PORTFOLIO %-23s %4d trades, %5.1f/month, avg %+.3fR %+6.2f GBP, win %4.1f%%, worst losing streak %2d, "
              "max drawdown GBP %.0f (%.0f%% of 2,250), total %+.0f GBP over %.0f months" % (
                  cname, P["trades"], P["per_month"], P["avg_R"], P["avg_gbp"], P["win"], P["streak"], P["mdd"],
                  P["mdd_pct"], P["total"], P["months"]))
    if name[0] in "34":
        P = portfolio(T, k)
        syms = [s for s in IND if s not in ("QQQ", "VTI", "XLK")]
        print("  buy and hold %s..%s: equal-weight list %+.0f%%, QQQ %+.0f%% (strategy on 2,250: %+.0f%%)" % (
            P["first"], P["last"], bh(syms, P["first"], P["last"]), bh(["QQQ"], P["first"], P["last"]), 100 * P["total"] / CAPITAL))
