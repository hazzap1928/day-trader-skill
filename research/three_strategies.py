"""Three documented strategies, Trading 212 GBP-account costs, develop 2019-2023, unseen 2024-2026.
Costs: 0.15% FX fee + 0.05% slippage on every fill (buy and sell). Capital GBP 2,250. FX moves ignored."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _data import DATA
import csv, datetime, json, math, random, statistics as st, sys, collections

SP = DATA
K = 0.0015 + 0.0005
CAPITAL = 2250.0
DEV = ("2019-01-01", "2023-12-31")
TEST = ("2024-01-01", "2026-11-06")
SLOTS = 5                                    # event strategies: 5 positions of CAPITAL/5
RUNS = 200
rng = random.Random(19)

P = json.load(open(SP + "prices.json"))
added = {r["Symbol"].replace(".", "-"): r["Date added"] for r in csv.DictReader(open(SP + "sp500.csv"))}
allow = set(json.load(open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "trader/config.json")))["allowed_tickers"])
BENCH = ("SPY", "RSP", "QQQ")


def d_of(ts):
    return datetime.datetime.utcfromtimestamp(ts).date().isoformat()


S = {}
for s, rows in P.items():
    D = [d_of(r[0]) for r in rows]
    S[s] = {"D": D, "O": [r[1] for r in rows], "H": [r[2] for r in rows], "L": [r[3] for r in rows],
            "C": [r[4] for r in rows], "V": [r[5] for r in rows], "idx": {d: i for i, d in enumerate(D)}}
CAL = S["SPY"]["D"]                           # the trading calendar
CI = {d: i for i, d in enumerate(CAL)}


def member(s, d):
    """Point-in-time-ish universe: an S&P 500 stock from the day it joined (today's list), or an allow-list stock."""
    if s in BENCH:
        return False
    a = added.get(s)
    if a:
        return a[:10] <= d
    return s in allow


def px(s, d, f="C"):
    I = S[s]; i = I["idx"].get(d)
    return None if i is None else I[f][i]


def net_ret(entry, exit_):
    return exit_ * (1 - K) / (entry * (1 + K)) - 1


def months_between(a, b):
    return max(1e-9, (datetime.date.fromisoformat(b) - datetime.date.fromisoformat(a)).days / 30.44)


def mdd(curve):
    peak, worst = -1e18, 0.0
    for v in curve:
        peak = max(peak, v); worst = max(worst, (peak - v) / peak)
    return 100 * worst


def cagr(v0, v1, a, b):
    yrs = (datetime.date.fromisoformat(b) - datetime.date.fromisoformat(a)).days / 365.25
    return 100 * ((v1 / v0) ** (1 / yrs) - 1)


def bh(s, a, b):
    """Buy at the first open, sell at the last close in [a, b], costs in. Returns (total %, curve)."""
    I = S[s]; ids = [i for i, d in enumerate(I["D"]) if a <= d <= b]
    e = I["O"][ids[0]] * (1 + K)
    curve = [CAPITAL * I["C"][i] / e for i in ids]
    curve[-1] *= (1 - K)
    return 100 * (curve[-1] / CAPITAL - 1), curve


def ew_universe(a, b):
    """Equal weight in every member stock with data for the whole window, held through."""
    rets = []
    for s in S:
        if s in BENCH or not member(s, a): continue
        I = S[s]
        if I["idx"].get(a) is None and not any(a <= d <= b for d in I["D"][:5]): pass
        ids = [i for i, d in enumerate(I["D"]) if a <= d <= b]
        if len(ids) < 0.95 * len([d for d in CAL if a <= d <= b]): continue
        rets.append(net_ret(I["O"][ids[0]], I["C"][ids[-1]]))
    return 100 * st.mean(rets), len(rets)


# ---------------------------------------------------------------- 1. momentum
def month_ends(a, b):
    out = []
    for i, d in enumerate(CAL):
        if a <= d <= b and (i + 1 == len(CAL) or CAL[i + 1][:7] != d[:7]):
            out.append(i)
    return out


def momentum(a, b, look=12, skip=1, top=10, random_pick=False, r=None):
    """Monthly: rank members by the return from look months to skip months ago; hold the top N equally.
    Rebalance at the next open: sell the leavers, buy the joiners with the cash freed; held names untouched."""
    ends = month_ends(a, b)
    cash, hold, trades, curve = CAPITAL, {}, [], []
    for m, ei in enumerate(ends):
        d = CAL[ei]
        if ei + 1 >= len(CAL) or CAL[ei + 1] > b: break
        nd = CAL[ei + 1]
        lb, sk = ei - 21 * look, ei - 21 * skip
        if lb < 0: continue
        ranks = []
        for s in S:
            if not member(s, d): continue
            c0, c1, c2 = px(s, CAL[lb]), px(s, CAL[sk]), px(s, nd, "O")
            if not c0 or not c1 or not c2: continue
            vol = S[s]["V"][S[s]["idx"][d]] * S[s]["C"][S[s]["idx"][d]]
            if vol < 20e6: continue
            ranks.append((c1 / c0 - 1, s))
        if random_pick:
            r.shuffle(ranks); pick = [s for _, s in ranks[:top]]
        else:
            pick = [s for _, s in sorted(ranks, reverse=True)[:top]]
        for s in list(hold):
            if s not in pick:
                o = px(s, nd, "O")
                if o is None: continue
                h = hold.pop(s)
                cash += h["sh"] * o * (1 - K)
                trades.append(dict(h, exit=o, exit_date=nd, ret=net_ret(h["entry"], o)))
        joiners = [s for s in pick if s not in hold]
        if joiners:
            per = cash / len(joiners)
            for s in joiners:
                o = px(s, nd, "O")
                sh = per / (o * (1 + K)); cash -= per
                hold[s] = {"sym": s, "sh": sh, "entry": o, "entry_date": nd}
        # mark to market through to the next month end
        nxt = ends[m + 1] if m + 1 < len(ends) else len([x for x in CAL if x <= b]) - 1
        for j in range(ei + 1, nxt + 1):
            dj = CAL[j]
            curve.append(cash + sum(h["sh"] * (px(s, dj) or h["entry"]) for s, h in hold.items()))
    last = [d for d in CAL if d <= b][-1]
    for s, h in hold.items():
        c = px(s, last) or h["entry"]
        cash += h["sh"] * c * (1 - K)
        trades.append(dict(h, exit=c, exit_date=last, ret=net_ret(h["entry"], c)))
    return {"trades": trades, "end": cash, "curve": curve}


# ---------------------------------------------------------------- shared: indicators for the pullback
for s, I in S.items():
    C, H, L = I["C"], I["H"], I["L"]
    n = len(C)
    pre = [0.0]
    for c in C: pre.append(pre[-1] + c)
    I["sma"] = lambda k, i, pre=pre: (pre[i + 1] - pre[i + 1 - k]) / k if i >= k - 1 else None
    tr = [H[0] - L[0]] + [max(H[i] - L[i], abs(H[i] - C[i - 1]), abs(L[i] - C[i - 1])) for i in range(1, n)]
    pt = [0.0]
    for x in tr: pt.append(pt[-1] + x)
    I["atr"] = [(pt[i + 1] - pt[i - 19]) / 20 if i >= 19 else None for i in range(n)]
    rsi = [None] * n; ag = al = None
    for i in range(1, n):
        g, l_ = max(C[i] - C[i - 1], 0), max(C[i - 1] - C[i], 0)
        if ag is None: ag, al = g, l_
        else: ag, al = (ag + g) / 2, (al + l_) / 2
        if i >= 2: rsi[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    I["rsi"] = rsi


def pullback_exit(I, e, rule):
    """From the entry bar e (bought at its open). rule: ("sma", k) close above SMA k, ("days", n) n days.
    A 3xATR stop from the signal day applies to all."""
    n = len(I["C"])
    stop = I["O"][e] - 3 * I["atr"][e - 1]
    limit = {"sma": 20, "days": None}[rule[0]] or rule[1]
    for j in range(e, min(n, e + limit)):
        if I["L"][j] <= stop:
            return j, min(stop, I["O"][j]) if j > e else stop, "stop"
        if rule[0] == "sma" and j > e and I["C"][j] > I["sma"](rule[1], j):
            return j, I["C"][j], "above %d-day average" % rule[1]
    j = min(n - 1, e + limit - 1)
    return j, I["C"][j], "time"


def pullback_signals(a, b, rule, random_entries=False, r=None, counts=None):
    out = []
    for s, I in S.items():
        if s in BENCH: continue
        n = len(I["C"]); busy = -1; cands = []
        for k in range(200, n - 1):
            d = I["D"][k]
            if not (a <= d <= b) or not member(s, d) or I["atr"][k] is None: continue
            if I["C"][k] * I["V"][k] < 20e6: continue
            up = I["C"][k] > I["sma"](200, k)
            if not up: continue
            if random_entries or (I["rsi"][k] is not None and I["rsi"][k] < 10):
                cands.append(k)
        if random_entries:
            cands = sorted(r.sample(cands, min(len(cands), counts.get(s, 0))))
        for k in cands:
            e = k + 1
            if e <= busy: continue
            j, x, why = pullback_exit(I, e, rule)
            if I["D"][j] > b: continue
            busy = j
            out.append({"sym": s, "entry_date": I["D"][e], "exit_date": I["D"][j], "entry": I["O"][e], "exit": x,
                        "why": why, "ret": net_ret(I["O"][e], x), "rank": -(I["rsi"][k] or 50), "days": j - e + 1})
    return out


# ---------------------------------------------------------------- 2. post-earnings drift
try:
    E = json.load(open(SP + "earnings.json"))
except (OSError, ValueError):
    E = {}


def num(x):
    try:
        return float(str(x).replace("$", "").replace(",", "").replace("(", "-").replace(")", ""))
    except (TypeError, ValueError):
        return None


def pead_signals(a, b, min_surprise=10.0, confirm=False, hold=20, random_events=False, r=None, n_pick=None):
    events = []
    for day, rows in E.items():
        if not (a <= day <= b): continue
        for x in rows or []:
            s = str(x.get("symbol") or "").upper().replace(".", "-")
            if s not in S or not member(s, day): continue
            sur, fc = num(x.get("surprise")), num(x.get("epsForecast"))
            if sur is None or fc is None or abs(fc) < 0.10: continue
            events.append((day, s, sur))
    pool = events
    if random_events:
        pool = r.sample(events, min(len(events), n_pick))
    else:
        pool = [e for e in events if e[2] >= min_surprise]
    out = []
    for day, s, sur in pool:
        I = S[s]
        if day not in CI: continue
        ci = CI[day]
        if ci + 2 >= len(CAL): continue
        ed = CAL[ci + 2]
        e = I["idx"].get(ed)
        if e is None or e + hold - 1 >= len(I["C"]): continue
        if confirm:
            r0, r1 = I["idx"].get(CAL[ci - 1]), I["idx"].get(CAL[ci + 1])
            if r0 is None or r1 is None or I["C"][r1] <= I["C"][r0]: continue
        j = e + hold - 1
        if I["D"][j] > b: continue
        out.append({"sym": s, "entry_date": I["D"][e], "exit_date": I["D"][j], "entry": I["O"][e], "exit": I["C"][j],
                    "why": "20 days", "ret": net_ret(I["O"][e], I["C"][j]), "rank": sur, "days": hold})
    return out, len(events)


# ---------------------------------------------------------------- slot portfolio for event strategies
def slot_portfolio(T, a, b):
    by = collections.defaultdict(list)
    for t in T: by[t["entry_date"]].append(t)
    days = [d for d in CAL if a <= d <= b]
    cash, open_, taken, curve = CAPITAL, [], [], []
    for d in days:
        for p in [p for p in open_ if p["t"]["exit_date"] <= d]:
            if p["t"]["exit_date"] < d or True:
                cash += p["sh"] * p["t"]["exit"] * (1 - K); open_.remove(p); taken.append(p)
        for t in sorted(by.get(d, []), key=lambda t: -t["rank"]):
            if len(open_) >= SLOTS or any(p["t"]["sym"] == t["sym"] for p in open_): continue
            equity = cash + sum(p["sh"] * (px(p["t"]["sym"], d) or p["t"]["entry"]) for p in open_)
            stake = min(cash, equity / SLOTS)
            if stake < 50: continue
            sh = stake / (t["entry"] * (1 + K)); cash -= stake
            open_.append({"t": t, "sh": sh, "stake": stake})
        curve.append(cash + sum(p["sh"] * (px(p["t"]["sym"], d) or p["t"]["entry"]) for p in open_))
    for p in open_:
        cash += p["sh"] * p["t"]["exit"] * (1 - K); taken.append(p)
    return {"taken": taken, "end": cash, "curve": curve}


# ---------------------------------------------------------------- reporting
def tstat(xs):
    return st.mean(xs) / (st.stdev(xs) / math.sqrt(len(xs))) if len(xs) > 2 and st.stdev(xs) > 0 else 0.0


def line(name, a, b, trades_rets, n_trades, end, curve, rnd=None, stake=None):
    m = st.mean(trades_rets) if trades_rets else float("nan")
    gbp = m * (stake or CAPITAL / 10)
    s = ("  %-6s %s..%s  trades %4d (%5.1f/month)  avg %+.2f%% a trade (about %+.2f GBP)  win %4.1f%%  t %+.1f  "
         "portfolio %+6.1f%% (%+5.1f%%/yr)  max drawdown %4.1f%%" % (
             name, a, b, n_trades, n_trades / months_between(a, b), 100 * m, gbp,
             100 * sum(1 for x in trades_rets if x > 0) / max(1, len(trades_rets)), tstat(trades_rets),
             100 * (end / CAPITAL - 1), cagr(CAPITAL, end, a, b), mdd(curve)))
    if rnd is not None:
        s += "\n         random, same rules (%d runs): portfolio %+.1f%% median, 5-95%% %+.1f%% to %+.1f%%; strategy beats %d%%" % (
            len(rnd), st.median(rnd), sorted(rnd)[int(0.05 * len(rnd))], sorted(rnd)[int(0.95 * len(rnd)) - 1],
            round(100 * sum(1 for x in rnd if 100 * (end / CAPITAL - 1) > x) / len(rnd)))
    return s


def benchmarks(a, b):
    out = []
    for s in ("SPY", "RSP", "QQQ"):
        tot, curve = bh(s, a, b)
        out.append("%s %+.1f%% (dd %.1f%%)" % (s, tot, mdd(curve)))
    ew, n = ew_universe(a, b)
    out.append("equal-weight universe %+.1f%% (%d stocks, survivors only)" % (ew, n))
    return "  buy and hold, costs in: " + "; ".join(out)


if __name__ == "__main__":
    which = sys.argv[1:] or ["mom", "pead", "pull"]
    print("universe: %d symbols with prices; costs %.2f%% a fill; capital GBP %.0f" % (len(S), 100 * K, CAPITAL))
    for a, b in (DEV, TEST):
        print(benchmarks(a, b) + "  [%s..%s]" % (a, b))

    if "mom" in which:
        print("\n=== 1. MOMENTUM (pre-registered: 12-month, skip last month, top 10, monthly)")
        for a, b, tag in ((DEV[0], DEV[1], "DEV"), (TEST[0], TEST[1], "UNSEEN")):
            R = momentum(a, b)
            rets = [t["ret"] for t in R["trades"]]
            rnd = [100 * (momentum(a, b, random_pick=True, r=random.Random(i))["end"] / CAPITAL - 1) for i in range(RUNS // 4)]
            print(line(tag, a, b, rets, len(rets), R["end"], R["curve"], rnd, stake=CAPITAL / 10))
            print("         avg holding %.1f months" % st.mean(
                months_between(t["entry_date"], t["exit_date"]) for t in R["trades"]))
        print("  sensitivity on DEV only (not used to choose):")
        for look, skip in ((6, 1), (9, 1), (12, 0), (12, 1)):
            R = momentum(DEV[0], DEV[1], look, skip)
            print("    %2d-month, skip %d: portfolio %+.1f%%, max drawdown %.1f%%" % (look, skip, 100 * (R["end"] / CAPITAL - 1), mdd(R["curve"])))

    if "pead" in which:
        print("\n=== 2. POST-EARNINGS DRIFT (surprise >= 10%, forecast >= $0.10, buy 2nd day open, hold 20 days)")
        print("  development grid (DEV only), all-signal average per trade after costs:")
        best = None
        for ms in (5.0, 10.0, 20.0):
            for conf in (False, True):
                T, nev = pead_signals(DEV[0], DEV[1], ms, conf)
                m = st.mean(t["ret"] for t in T)
                print("    surprise >= %4.0f%%%s: %4d signals, avg %+.2f%%, t %+.1f" % (
                    ms, ", up on the day" if conf else "", len(T), 100 * m, tstat([t["ret"] for t in T])))
                if best is None or m > best[0]: best = (m, ms, conf)
        print("  chosen on DEV: surprise >= %.0f%%%s" % (best[1], ", up on the day" if best[2] else ""))
        for a, b, tag in ((DEV[0], DEV[1], "DEV"), (TEST[0], TEST[1], "UNSEEN")):
            T, nev = pead_signals(a, b, best[1], best[2])
            Pf = slot_portfolio(T, a, b)
            rets = [p["t"]["ret"] for p in Pf["taken"]]
            rnd_mean, rnd_port = [], []
            for i in range(RUNS):
                R, _ = pead_signals(a, b, random_events=True, r=random.Random(i), n_pick=len(T) * 2)
                R = R[:len(T)]
                rnd_mean.append(st.mean(t["ret"] for t in R))
                if i < 40: rnd_port.append(100 * (slot_portfolio(R, a, b)["end"] / CAPITAL - 1))
            print("  %-6s all %d signals: avg %+.2f%% a trade after costs (t %+.1f); random earnings events: avg %+.2f%% "
                  "(5-95%% %+.2f%% to %+.2f%%), signals beat %d%% of runs" % (
                      tag, len(T), 100 * st.mean(t["ret"] for t in T), tstat([t["ret"] for t in T]),
                      100 * st.mean(rnd_mean), 100 * sorted(rnd_mean)[10], 100 * sorted(rnd_mean)[189],
                      round(100 * sum(1 for x in rnd_mean if st.mean(t["ret"] for t in T) > x) / len(rnd_mean))))
            print(line(tag, a, b, rets, len(rets), Pf["end"], Pf["curve"], rnd_port, stake=CAPITAL / SLOTS))

    if "pull" in which:
        print("\n=== 3. PULLBACK, LONGER HOLD (RSI(2) < 10 above the 200-day average; 3xATR stop)")
        rules = [("sma", 10), ("days", 10), ("days", 20)]
        print("  development grid (DEV only), all-signal average per trade after costs:")
        best = None
        for rule in [("sma", 5)] + rules:
            T = pullback_signals(DEV[0], DEV[1], rule)
            m = st.mean(t["ret"] for t in T)
            print("    exit %-10s: %5d signals, avg %+.2f%%, t %+.1f, avg %.1f days%s" % (
                "%s %d" % rule, len(T), 100 * m, tstat([t["ret"] for t in T]), st.mean(t["days"] for t in T),
                "  (the original, for reference)" if rule == ("sma", 5) else ""))
            if rule in rules and (best is None or m > best[0]): best = (m, rule)
        rule = best[1]
        print("  chosen on DEV: exit %s %d" % rule)
        for a, b, tag in ((DEV[0], DEV[1], "DEV"), (TEST[0], TEST[1], "UNSEEN")):
            T = pullback_signals(a, b, rule)
            cnt = collections.Counter(t["sym"] for t in T)
            Pf = slot_portfolio(T, a, b)
            rets = [p["t"]["ret"] for p in Pf["taken"]]
            rnd_mean, rnd_port = [], []
            for i in range(RUNS // 4):
                R = pullback_signals(a, b, rule, random_entries=True, r=random.Random(i), counts=cnt)
                rnd_mean.append(st.mean(t["ret"] for t in R))
                if i < 25: rnd_port.append(100 * (slot_portfolio(R, a, b)["end"] / CAPITAL - 1))
            m = st.mean(t["ret"] for t in T)
            print("  %-6s all %d signals: avg %+.2f%% a trade after costs (t %+.1f); random uptrend entries, same exits: "
                  "avg %+.2f%% (5-95%% %+.2f%% to %+.2f%%), signals beat %d%% of runs" % (
                      tag, len(T), 100 * m, tstat([t["ret"] for t in T]), 100 * st.mean(rnd_mean),
                      100 * sorted(rnd_mean)[int(0.05 * len(rnd_mean))], 100 * sorted(rnd_mean)[int(0.95 * len(rnd_mean)) - 1],
                      round(100 * sum(1 for x in rnd_mean if m > x) / len(rnd_mean))))
            print(line(tag, a, b, rets, len(rets), Pf["end"], Pf["curve"], rnd_port, stake=CAPITAL / SLOTS))
