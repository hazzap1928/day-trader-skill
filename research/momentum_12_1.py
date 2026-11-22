"""12-1 momentum, top 10, monthly, on our stock list. Research only: no orders, no live agent.

The rule (the owner's request, 22 Nov 2026):
  - at each month-end close, rank the universe by the return from about 12 months ago (252 trading
    days) to about 1 month ago (21 trading days): the most recent month is skipped
  - buy the top 10 in equal amounts at the next open, hold to the next month end, rebalance at the next
    open back to 10 equal amounts (only the difference is traded)
  - costs: 0.15% FX + 0.05% slippage on every pound bought or sold (0.2% each way)
Universe each month: S&P 500 members, price $5+, 20-day average dollar volume $20m+ (the live universe's
filters; "on Trading 212" cannot be checked for the past).

Rules set on 2019-2023 (DEV), checked once on 2024 to Oct 2026 (UNSEEN). The owner's rule is reported
on both. A small DEV-only grid (look-back 6/12 months, top 10/20, a market filter off/on) shows whether
DEV would have chosen something else; the DEV winner is also run once on UNSEEN.

Survivorship: two universes.
  today : today's S&P 500 list, each company counted from the day it joined (as in earlier research)
  pit   : the index as it was each month (github.com/fja05680/sp500), including companies that have
          since left, with whatever prices Yahoo still has for them (momentum_fetch_removed.py)
Plus an equal-weight portfolio of each universe against RSP (the real equal-weight S&P 500 fund, which
has no survivorship bias), which measures how much each universe flatters a long-only test.

Run: python3 research/momentum_12_1.py   (needs prices.json; prices_removed.json for the pit universe)
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _data import DATA
import csv, datetime, json, random, statistics as st, bisect

FX, SLIP = 0.0015, 0.0005
TRACKER_K = 0.0005                        # the GBP-listed S&P 500 tracker: no FX fee, slippage only
CAPITAL = 2250.0
DEV = ("2019-01", "2023-12")
UNSEEN = ("2024-01", "2026-10")
RUNS = 200
BENCH = ("SPY", "RSP", "QQQ")


def d_of(ts):
    return datetime.datetime.utcfromtimestamp(ts).date().isoformat()


def load(path):
    out = {}
    if not os.path.exists(path):
        return out
    for s, rows in json.load(open(path)).items():
        D = [d_of(r[0]) for r in rows]
        out[s] = {"D": D, "O": [r[1] for r in rows], "C": [r[4] for r in rows],
                  "DV": [r[4] * r[5] for r in rows], "idx": {d: i for i, d in enumerate(D)}}
    return out


ADDED = {r["Symbol"].replace(".", "-"): r["Date added"][:10] for r in csv.DictReader(open(DATA + "sp500.csv"))}
PIT_DATES, PIT_SETS = [], []
_h = DATA + "sp500-history/components.csv"
if os.path.exists(_h):
    for r in csv.DictReader(open(_h)):
        PIT_DATES.append(r["date"]); PIT_SETS.append({t.replace(".", "-") for t in r["tickers"].split(",")})

S = load(DATA + "prices.json")
REMOVED, REUSED = {}, []
for s, v in load(DATA + "prices_removed.json").items():
    # Yahoo reuses old tickers (FB, LB, STI now belong to other companies): keep a series only if it
    # covers at least 95% of the snapshots in which the company was in the index (from June 2018)
    mem = [d for d, m in zip(PIT_DATES, PIT_SETS) if d >= "2018-06-01" and s in m]
    end = (datetime.date.fromisoformat(v["D"][-1]) + datetime.timedelta(5)).isoformat()
    if mem and sum(v["D"][0] <= d <= end for d in mem) >= 0.95 * len(mem):
        REMOVED[s] = v
    else:
        REUSED.append(s)
for s, v in REMOVED.items():
    S.setdefault(s, v)
CAL = S["SPY"]["D"]
CI = {d: i for i, d in enumerate(CAL)}


def members(d, universe):
    if universe == "today":
        return [s for s, a in ADDED.items() if a <= d]
    i = bisect.bisect_right(PIT_DATES, d) - 1
    return sorted(PIT_SETS[i])


# ------------------------------------------------------------------ the monthly ranking
def month_end_idx():
    return [i for i, d in enumerate(CAL) if i + 1 == len(CAL) or CAL[i + 1][:7] != d[:7]]


ME = month_end_idx()
ME_BY_MONTH = {CAL[i][:7]: i for i in ME}


def prev_month(m):
    y, mo = int(m[:4]), int(m[5:])
    return "%04d-%02d" % (y - (mo == 1), 12 if mo == 1 else mo - 1)


def months(a, b):
    out, m = [], a
    while m <= b:
        out.append(m)
        y, mo = int(m[:4]), int(m[5:])
        m = "%04d-%02d" % (y + (mo == 12), 1 if mo == 12 else mo + 1)
    return out


_rank_cache = {}


def ranking(ei, universe, look):
    """[(score, sym)] for the eligible members at month-end index ei, plus coverage counts."""
    key = (ei, universe, look)
    if key in _rank_cache:
        return _rank_cache[key]
    d = CAL[ei]
    d0, d1 = CAL[ei - 21 * look], CAL[ei - 21]
    out, mem, no_data = [], members(d, universe), 0
    for s in mem:
        if s in BENCH:
            continue
        I = S.get(s)
        if I is None:
            no_data += 1; continue
        i = I["idx"].get(d)
        if i is None or i < 20:
            no_data += 1; continue
        c0, c1 = I["C"][I["idx"][d0]] if d0 in I["idx"] else None, I["C"][I["idx"][d1]] if d1 in I["idx"] else None
        if not c0 or not c1:
            continue
        if I["C"][i] < 5 or st.mean(I["DV"][i - 19:i + 1]) < 20e6:
            continue
        out.append((c1 / c0 - 1, s))
    out.sort(reverse=True)
    _rank_cache[key] = (out, len(mem), no_data)
    return _rank_cache[key]


def last_close(s, d):
    """The stock's close on d, or its last close before d (a company that left keeps its last price)."""
    I = S[s]; i = I["idx"].get(d)
    if i is not None:
        return I["C"][i]
    j = bisect.bisect_right(I["D"], d) - 1
    return I["C"][j] if j >= 0 else None


def open_or_last(s, d):
    I = S[s]; i = I["idx"].get(d)
    return I["O"][i] if i is not None else last_close(s, d)


def spy_above_200(ei):
    c = S["SPY"]["C"]
    return c[ei] >= st.mean(c[ei - 199:ei + 1])


# ------------------------------------------------------------------ the book
def run(period, universe="today", look=12, top=10, market_filter=False, slip=SLIP, picker=None, r=None):
    """Hold the top N each month. Returns value curve (daily closes), month-end values, stints, turnover."""
    K = FX + slip
    cash, hold = CAPITAL, {}                   # hold: sym -> shares
    curve, month_vals, stints, traded = [], [], [], 0.0
    open_stint = {}                            # sym -> (entry date, cost basis GBP)
    ms = months(*period)
    for n, m in enumerate(ms):
        ri = ME_BY_MONTH[prev_month(m)]
        bi = ri + 1                            # first trading day of the month
        ei = ME_BY_MONTH[m]
        bd = CAL[bi]
        ranks, _, _ = ranking(ri, universe, look)
        if market_filter and not spy_above_200(ri):
            pick = []
        elif picker is not None:
            pick = picker(ranks, list(hold), top, r)
        else:
            pick = [s for _, s in ranks[:top]]
        # value at the open, then trade to equal weights
        val = cash + sum(sh * open_or_last(s, bd) for s, sh in hold.items())
        target = val / len(pick) if pick else 0.0
        for s in list(hold):                  # sells first
            p = open_or_last(s, bd)
            want = target / p if s in pick else 0.0
            if hold[s] > want:
                q = hold[s] - want
                cash += q * p * (1 - K); traded += q * p
                hold[s] = want
                if want == 0.0:
                    e_d, basis = open_stint.pop(s)
                    stints.append({"sym": s, "in": e_d, "out": bd, "basis": basis})
                    del hold[s]
        # buys: spread the cash so every pick ends near the target after costs
        need = {s: max(0.0, target - hold.get(s, 0.0) * open_or_last(s, bd)) for s in pick}
        tot = sum(need.values())
        scale = min(1.0, cash / (tot * (1 + K))) if tot else 0.0
        for s, g in need.items():
            if g <= 0:
                continue
            g *= scale
            p = open_or_last(s, bd)
            hold[s] = hold.get(s, 0.0) + g / p
            cash -= g * (1 + K); traded += g
            if s not in open_stint:
                open_stint[s] = (bd, 0.0)
        for j in range(bi, ei + 1):
            curve.append(cash + sum(sh * last_close(s, CAL[j]) for s, sh in hold.items()))
        month_vals.append(curve[-1])
    final = cash + sum(sh * last_close(s, CAL[ME_BY_MONTH[ms[-1]]]) * (1 - K) for s, sh in hold.items())
    curve[-1] = final; month_vals[-1] = final
    return {"end": final, "curve": curve, "months": month_vals, "turnover": traded, "stints": stints}


def stint_profits(period, universe="today", look=12, top=10):
    """GBP made by each name before costs (approximate: each month's return x the money it held)."""
    res = {}
    cash_v = CAPITAL
    for m in months(*period):
        ri, ei = ME_BY_MONTH[prev_month(m)], ME_BY_MONTH[m]
        bd, ed = CAL[ri + 1], CAL[ei]
        pick = [s for _, s in ranking(ri, universe, look)[0][:top]]
        per = cash_v / len(pick)
        tot = 0.0
        for s in pick:
            r = last_close(s, ed) / open_or_last(s, bd)
            g = per * (r - 1)
            res[s] = res.get(s, 0.0) + g
            tot += per * r
        cash_v = tot
    return sorted(res.items(), key=lambda x: -x[1])


def buy_hold(sym, period, k=TRACKER_K):
    a = ME_BY_MONTH[prev_month(period[0])] + 1
    b = ME_BY_MONTH[period[1]]
    e = S[sym]["O"][S[sym]["idx"][CAL[a]]] * (1 + k)
    curve = [CAPITAL * S[sym]["C"][S[sym]["idx"][CAL[j]]] / e for j in range(a, b + 1)]
    curve[-1] *= (1 - k)
    mv = [curve[ME_BY_MONTH[m] - a] for m in months(*period)]
    mv[-1] = curve[-1]
    return {"end": curve[-1], "curve": curve, "months": mv}


def equal_weight_universe(period, universe):
    """Every eligible member, equal weight, rebalanced monthly, no costs: the universe's own return."""
    v = 1.0
    for m in months(*period):
        ri, ei = ME_BY_MONTH[prev_month(m)], ME_BY_MONTH[m]
        bd, ed = CAL[ri + 1], CAL[ei]
        ranks = ranking(ri, universe, 12)[0]
        rs = [last_close(s, ed) / open_or_last(s, bd) - 1 for _, s in ranks]
        v *= 1 + st.mean(rs)
    return 100 * (v - 1)


def rsp_no_cost(period):
    a = ME_BY_MONTH[prev_month(period[0])] + 1
    b = ME_BY_MONTH[period[1]]
    I = S["RSP"]
    return 100 * (I["C"][I["idx"][CAL[b]]] / I["O"][I["idx"][CAL[a]]] - 1)


# ------------------------------------------------------------------ measures
def pct(v):
    return 100 * (v / CAPITAL - 1)


def mdd(curve):
    peak, worst = -1e18, 0.0
    for v in curve:
        peak = max(peak, v); worst = max(worst, (peak - v) / peak)
    return 100 * worst


def monthly(mv):
    prev, out = CAPITAL, []
    for v in mv:
        out.append(v / prev - 1); prev = v
    return out


def yearly(R, period):
    ms = months(*period)
    out, prev = {}, CAPITAL
    for m, v in zip(ms, R["months"]):
        if m.endswith("-12") or m == ms[-1]:
            out[m[:4]] = 100 * (v / prev - 1); prev = v
    return out


def turnover_matched(keep_rate):
    def pick(ranks, held, top, r):
        elig = [s for _, s in ranks]
        keep = [s for s in held if s in elig and r.random() < keep_rate]
        rest = [s for s in elig if s not in keep]
        return keep + r.sample(rest, max(0, top - len(keep)))
    return pick


def keep_rate(period, universe, look, top):
    prev, kept, n = None, 0, 0
    for m in months(*period):
        pick = {s for _, s in ranking(ME_BY_MONTH[prev_month(m)], universe, look)[0][:top]}
        if prev is not None:
            kept += len(pick & prev); n += len(prev)
        prev = pick
    return kept / n


def summary(tag, R, spy, period):
    mr, sr = monthly(R["months"]), monthly(spy["months"])
    yrs = len(R["months"]) / 12
    return {
        "tag": tag, "total_pct": round(pct(R["end"]), 1),
        "a_year_pct": round(100 * ((R["end"] / CAPITAL) ** (1 / yrs) - 1), 1),
        "worst_drop_pct": round(mdd(R["curve"]), 1),
        "months": len(mr), "months_up_pct": round(100 * sum(x > 0 for x in mr) / len(mr)),
        "months_beat_spy_pct": round(100 * sum(a > b for a, b in zip(mr, sr)) / len(mr)),
        "worst_month_pct": round(100 * min(mr), 1), "best_month_pct": round(100 * max(mr), 1),
        "turnover_x": round(R.get("turnover", 0) / CAPITAL, 1) if "turnover" in R else None,
        "by_year": {y: round(v, 1) for y, v in yearly(R, period).items()},
    }


def show(s):
    by = ", ".join("%s %+.1f" % kv for kv in s["by_year"].items())
    print("  %-34s %+7.1f%%  (%+5.1f%% a year)  worst drop %4.1f%%  months up %2d%%  beat SPY %2d%% of months"
          % (s["tag"], s["total_pct"], s["a_year_pct"], s["worst_drop_pct"], s["months_up_pct"], s["months_beat_spy_pct"]))
    print("  %-34s by year: %s" % ("", by))


if __name__ == "__main__":
    out = {"run": datetime.date.today().isoformat(), "costs_each_way_pct": 100 * (FX + SLIP), "periods": {}}
    print("12-1 momentum, top 10, monthly. Costs 0.15%% FX + 0.05%% slippage each way. Capital GBP %.0f." % CAPITAL)
    print("Prices to %s. Companies that left the index, with usable prices: %d (dropped as reused tickers: %s)." % (CAL[-1], len(REMOVED), " ".join(REUSED)))

    # --- 1. DEV only: the grid that "sets the rules"
    print("\n1. Setting the rules, 2019-2023 only (today's list):")
    grid = []
    for look in (6, 12):
        for top in (10, 20):
            for mf in (False, True):
                R = run(DEV, "today", look, top, mf)
                grid.append((pct(R["end"]), mdd(R["curve"]), look, top, mf))
                print("  look-back %2d months, top %2d, market filter %-3s  %+7.1f%%  worst drop %4.1f%%"
                      % (look, top, "on" if mf else "off", pct(R["end"]), mdd(R["curve"])))
    spy_dev = buy_hold("SPY", DEV)
    print("  S&P 500 tracker (SPY)                          %+7.1f%%  worst drop %4.1f%%" % (pct(spy_dev["end"]), mdd(spy_dev["curve"])))
    best = max(grid)
    chosen = dict(look=best[2], top=best[3], market_filter=best[4])
    print("  DEV's best of the 8 (by total return): look-back %d, top %d, filter %s"
          % (best[2], best[3], "on" if best[4] else "off"))
    out["dev_grid"] = [dict(total_pct=round(g[0], 1), worst_drop_pct=round(g[1], 1), look=g[2], top=g[3],
                            market_filter=g[4]) for g in grid]
    out["dev_choice"] = chosen

    # --- 2. the owner's rule (and DEV's choice) on both periods, today's list and the real index
    for name, period in (("DEV 2019-2023", DEV), ("UNSEEN 2024 to Oct 2026", UNSEEN)):
        print("\n2. %s" % name)
        spy, rsp = buy_hold("SPY", period), buy_hold("RSP", period)
        P = {"spy": summary("S&P 500 tracker (SPY)", spy, spy, period),
             "rsp": summary("Equal-weight S&P 500 (RSP)", rsp, spy, period)}
        R = run(period, "today")
        P["rule_today"] = summary("Momentum, today's list", R, spy, period)
        P["rule_today_slip03"] = summary("  same, 0.3% slippage a side", run(period, "today", slip=0.003), spy, period)
        if PIT_SETS:
            P["rule_pit"] = summary("Momentum, the index as it was", run(period, "pit"), spy, period)
        if chosen != dict(look=12, top=10, market_filter=False):
            P["dev_choice_today"] = summary("DEV's choice, today's list", run(period, "today", **chosen), spy, period)
        for k in ("rule_today", "rule_today_slip03", "rule_pit", "dev_choice_today", "spy", "rsp"):
            if k in P:
                show(P[k])
        # random portfolios with momentum's turnover
        kr = keep_rate(period, "today", 12, 10)
        rnd = sorted(pct(run(period, "today", picker=turnover_matched(kr), r=random.Random(i))["end"]) for i in range(RUNS))
        beat = sum(P["rule_today"]["total_pct"] > x for x in rnd) / len(rnd)
        P["random"] = {"keep_rate": round(kr, 2), "median_pct": round(st.median(rnd), 1),
                       "p5": round(rnd[int(0.05 * RUNS)], 1), "p95": round(rnd[int(0.95 * RUNS)], 1),
                       "momentum_beats_pct": round(100 * beat)}
        print("  random 10 with the same turnover (%d runs, keep %.0f%% a month): median %+.1f%%, 5-95%% %+.1f to %+.1f; "
              "momentum beats %d%%" % (RUNS, 100 * kr, st.median(rnd), P["random"]["p5"], P["random"]["p95"], round(100 * beat)))
        # concentration
        prof = stint_profits(period)
        tot = sum(g for _, g in prof)
        P["top5"] = [(s, round(g)) for s, g in prof[:5]]
        P["top5_share_pct"] = round(100 * sum(g for _, g in prof[:5]) / tot) if tot > 0 else None
        print("  profit before costs ~GBP %+.0f; top 5 names %s = %s%% of it" % (
            tot, ", ".join("%s %+.0f" % x for x in P["top5"]), P["top5_share_pct"]))
        # survivorship: each universe, equal weight, against RSP
        ew_t = equal_weight_universe(period, "today")
        P["survivorship"] = {"rsp_pct": round(rsp_no_cost(period), 1), "ew_today_pct": round(ew_t, 1)}
        if PIT_SETS:
            P["survivorship"]["ew_pit_pct"] = round(equal_weight_universe(period, "pit"), 1)
        cov = [ranking(ME_BY_MONTH[prev_month(m)], "pit", 12) for m in months(*period)] if PIT_SETS else []
        if cov:
            P["survivorship"]["pit_members_no_prices_avg"] = round(st.mean(c[2] for c in cov), 1)
        print("  survivorship: equal weight of today's list %+.1f%%, of the real index %s, RSP %+.1f%%; "
              "real-index members without prices, average a month: %s" % (
                  ew_t, ("%+.1f%%" % P["survivorship"]["ew_pit_pct"]) if PIT_SETS else "n/a",
                  P["survivorship"]["rsp_pct"], P["survivorship"].get("pit_members_no_prices_avg")))
        out["periods"][name] = P
    json.dump(out, open(DATA + "momentum_12_1.json", "w"), indent=1)
    print("\nSaved", DATA + "momentum_12_1.json")
