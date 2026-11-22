"""Insider cluster buys on the S&P 500 as it really was. Research only: no orders, no live agent.

The owner's request (22 Nov 2026):
  - data: the SEC's Form 4 bulk data sets (research/form4_fetch.py, research/insider_extract.py): open-market
    purchases (code P) by directors and officers only; sales, exercises, grants and outside funds ignored
  - a "cluster buy": at least 2 different insiders buying within 30 days, together worth $100k or more.
    Dated by when the filing became public (the Form 4's filing date), never the trade date. After a signal,
    only purchases filed later count towards the next one on the same stock.
  - bought at the open of the next trading day after the filing date; held 20, 40 or 60 trading days
    (the buy day is day 1), with the drift stop: 3 x the 14-day ATR under the buy price (a day that opens
    under it sells at that open)
  - costs: 0.15% FX + 0.05% slippage each way (and 0.3% slippage as a second case)
  - universe on the filing date: an S&P 500 member that day (github.com/fja05680/sp500), price $5+ and
    $20m+ a day traded over 20 days (the live universe's filters)
  - rules set on 2019-2023 (the hold, picked by the average result against the S&P 500 a trade), checked once
    on 2024 to the last data (the SEC's files end with the newest quarter published)
  - against: the S&P 500 over the same days (a GBP tracker: 0.05% each way, no FX fee), and random buys:
    a random member that passes the same filters, bought the same day with the same hold and stop

Two views: every signal on its own, and a 10-slot book (money in the tracker between trades, like drift).

    python3 research/insider_cluster.py            # 0.05% slippage a side
    python3 research/insider_cluster.py --slip 0.3
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _data import DATA
import bisect, csv, datetime, importlib.util, json, random, statistics as st

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
_spec = importlib.util.spec_from_file_location("holds", os.path.join(ROOT, "trader-timemachine", "holds.py"))
holds = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(holds)

SLIP = float(sys.argv[sys.argv.index("--slip") + 1]) / 100 if "--slip" in sys.argv else 0.0005
holds.COST = 0.0015 + SLIP
K_TRACK = 0.0005
MIN_INSIDERS, MIN_VALUE, WINDOW = 2, 100_000, 30
HOLDS = (20, 40, 60)
STOP_K = 3.0
SLOTS = 10
CAPITAL = 2250.0
RANDOM_RUNS = int(sys.argv[sys.argv.index("--runs") + 1]) if "--runs" in sys.argv else 200
DEV = ("2019-01-01", "2023-12-31")
UNSEEN = ("2024-01-01", "2026-10-31")
BENCH = {"SPY", "RSP", "QQQ"}


# ------------------------------------------------------------------ prices and the index as it was
def d_of(ts):
    return datetime.datetime.utcfromtimestamp(ts).date().isoformat()


def load(path):
    out = {}
    if not os.path.exists(path):
        return out
    for s, rows in json.load(open(path)).items():
        D = [d_of(r[0]) for r in rows]
        out[s] = {"D": D, "rows": [tuple(r[1:5]) for r in rows], "C": [r[4] for r in rows],
                  "DV": [r[4] * r[5] for r in rows], "idx": {d: i for i, d in enumerate(D)}}
    return out


PIT_DATES, PIT_SETS = [], []
for r in csv.DictReader(open(DATA + "sp500-history/components.csv")):
    PIT_DATES.append(r["date"]); PIT_SETS.append({t.replace(".", "-") for t in r["tickers"].split(",")})
SECTOR = {r["Symbol"].replace(".", "-"): r["GICS Sector"] for r in csv.DictReader(open(DATA + "sp500.csv"))}

S = load(DATA + "prices.json")
for s, v in load(DATA + "prices_removed.json").items():
    # as in momentum_12_1.py: Yahoo reuses old tickers, so keep a series only if it covers 95% of its membership
    mem = [d for d, m in zip(PIT_DATES, PIT_SETS) if d >= "2018-06-01" and s in m]
    end = (datetime.date.fromisoformat(v["D"][-1]) + datetime.timedelta(5)).isoformat()
    if mem and sum(v["D"][0] <= d <= end for d in mem) >= 0.95 * len(mem):
        S.setdefault(s, v)
CAL = S["SPY"]["D"]
LAST = CAL[-1]


def members(d):
    return PIT_SETS[bisect.bisect_right(PIT_DATES, d) - 1]


def next_day(d):
    """The first trading day after d."""
    i = bisect.bisect_right(CAL, d)
    return CAL[i] if i < len(CAL) else None


def tradable(s, d):
    """In the S&P 500 on d, with prices, $5+ and $20m+ a day over the 20 days to d (the last close on or before d)."""
    if s in BENCH or s not in members(d) or s not in S:
        return False
    I = S[s]
    i = bisect.bisect_right(I["D"], d) - 1
    if i < 20 or I["C"][i] < 5:
        return False
    return st.mean(I["DV"][i - 19:i + 1]) >= 20e6


# ------------------------------------------------------------------ the signals
def signals():
    buys = json.load(open(DATA + "insider_buys.json"))
    latest = {}                                        # the newest ticker of each company (FB -> META)
    for b in buys:
        if b["sym"] and b["sym"] not in ("NONE", "N/A"):
            latest[b["cik"]] = b["sym"].replace(".", "-")
    by = {}
    for b in buys:
        by.setdefault(b["cik"], []).append(b)
    out, stats = [], {"clusters": 0, "not_member": 0, "no_prices_or_filters": 0}
    for cik, L in by.items():
        L.sort(key=lambda b: b["filed"])
        last = None
        for F in sorted({b["filed"] for b in L}):
            lo = (datetime.date.fromisoformat(F) - datetime.timedelta(WINDOW)).isoformat()
            w = [b for b in L if b["filed"] <= F and (last is None or b["filed"] > last) and b["traded"] >= lo]
            owners = {b["owner"] for b in w}
            value = sum(b["value"] for b in w)
            if len(owners) < MIN_INSIDERS or value < MIN_VALUE:
                continue
            last = F
            stats["clusters"] += 1
            then = w[-1]["sym"].replace(".", "-")
            now = latest.get(cik, then)
            if then not in members(F) and now not in members(F):
                stats["not_member"] += 1; continue
            sym = now if now in S else then
            if not tradable(sym, F) and not (then in S and tradable(then, F)):
                stats["no_prices_or_filters"] += 1; continue
            if not tradable(sym, F):
                sym = then
            out.append({"filed": F, "sym": sym, "insiders": len(owners), "value": value,
                        "titles": sorted({b["title"] for b in w if b["title"]})[:4]})
    out.sort(key=lambda x: (x["filed"], -x["value"]))
    return out, stats


# ------------------------------------------------------------------ one trade
def trade(sym, filed, hold, end):
    """The walk for a buy at the open after `filed`, held `hold` days with the 3 x ATR stop, or None when the
    trade cannot be made or would not finish by `end`. A company whose prices stop (taken over, delisted) is
    sold at its last close."""
    ed = next_day(filed)
    if ed is None:
        return None
    I = S[sym]
    e = I["idx"].get(ed)
    if e is None:
        return None
    rows = I["rows"]
    a = holds.atr(rows, e)
    if a is None:
        return None
    x = e + hold - 1
    if x >= len(rows):
        if I["D"][-1] >= CAL[-6]:                      # still trading: the hold has simply not finished yet
            return None
        x = len(rows) - 1                              # left the market: out at its last close
    if I["D"][x] > end:
        return None
    w = holds.walk(rows, e, x, "fixed", dist=STOP_K * a)
    xd = I["D"][w["exit_i"]]
    T = S["SPY"]
    spy = T["C"][T["idx"][xd]] * (1 - K_TRACK) / (T["rows"][T["idx"][ed]][0] * (1 + K_TRACK)) - 1 if xd in T["idx"] else 0.0
    return {"sym": sym, "entry_date": ed, "exit_date": xd, "net": w["net"], "why": w["why"], "spy": spy,
            "atr_pct": 100 * a / rows[e][0]}


# ------------------------------------------------------------------ the 10-slot book
def book(trades, period):
    """Money in the tracker; each trade takes one slot (a tenth of the value) at its buy-day open and goes back
    into the tracker at its exit. Same-day signals: the biggest cluster first."""
    by = {}
    for t in trades:
        by.setdefault(t["entry_date"], []).append(t)
    T = S["SPY"]
    days = [d for d in CAL if period[0] <= d <= period[1]]
    units = CAPITAL * (1 - K_TRACK) / T["rows"][T["idx"][days[0]]][0]
    open_, curve, done = [], [], []
    for d in days:
        ti = T["idx"][d]
        for t in by.get(d, []):
            if len(open_) >= SLOTS or any(p["sym"] == t["sym"] for p in open_):
                continue
            value = units * T["rows"][ti][0] + sum(p["stake"] * (1 + p["mark"]) for p in open_)
            u = min(units, value / SLOTS / T["rows"][ti][0])
            if u * T["rows"][ti][0] < 25:
                continue
            units -= u
            open_.append(dict(t, stake=u * T["rows"][ti][0], mark=0.0))
        for p in open_:                                 # mark to market at the close
            I = S[p["sym"]]
            j = bisect.bisect_right(I["D"], d) - 1
            p["mark"] = I["C"][j] / I["rows"][I["idx"][p["entry_date"]]][0] - 1
        for p in [p for p in open_ if p["exit_date"] == d]:
            back = p["stake"] * (1 + p["net"]) * (1 - K_TRACK)
            units += back / T["C"][ti]
            done.append(dict(p, gbp=back - p["stake"]))
            open_.remove(p)
        curve.append(units * T["C"][ti] + sum(p["stake"] * (1 + p["mark"]) for p in open_))
    end = curve[-1] * (1 - K_TRACK)
    peak, mdd = curve[0], 0.0
    for v in curve:
        peak = max(peak, v); mdd = min(mdd, v / peak - 1)
    return {"total_pct": 100 * (end / CAPITAL - 1), "max_drop_pct": 100 * mdd, "trades": done, "days": days}


def tracker(period):
    T = S["SPY"]
    days = [d for d in CAL if period[0] <= d <= period[1]]
    units = CAPITAL * (1 - K_TRACK) / T["rows"][T["idx"][days[0]]][0]
    curve = [units * T["C"][T["idx"][d]] for d in days]
    peak, mdd = curve[0], 0.0
    for v in curve:
        peak = max(peak, v); mdd = min(mdd, v / peak - 1)
    return {"total_pct": 100 * (curve[-1] * (1 - K_TRACK) / CAPITAL - 1), "max_drop_pct": 100 * mdd}


# ------------------------------------------------------------------ random buys on the same days
_pool = {}


def pool(d):
    if d not in _pool:
        _pool[d] = sorted(s for s in members(d) if tradable(s, d))
    return _pool[d]


def random_runs(sigs, hold, period, end, runs=RANDOM_RUNS, seed=11):
    r = random.Random(seed)
    avgs, books = [], []
    for _ in range(runs):
        ts = []
        for g in sigs:
            p = pool(g["filed"])
            if not p:
                continue
            t = None
            for _try in range(5):
                t = trade(r.choice(p), g["filed"], hold, end)
                if t:
                    break
            if t:
                ts.append(t)
        avgs.append(100 * st.mean(t["net"] for t in ts))
        books.append(book(ts, period)["total_pct"])
    return avgs, books


def boot(xs, n=2000, seed=7):
    r = random.Random(seed)
    m = sorted(st.mean(r.choice(xs) for _ in xs) for _ in range(n))
    return 100 * m[int(0.025 * n)], 100 * m[int(0.975 * n)]


# ------------------------------------------------------------------ drift's holdings at the same time
def drift_windows():
    """(symbol, first day, last day) of every drift trade the tested rule's 10-slot book made (no stop), from
    research/pead_tracker.py, and of every qualifying drift signal."""
    ns = {"__file__": os.path.join(HERE, "pead_tracker.py"), "__name__": "pead"}
    src = open(os.path.join(HERE, "pead_tracker.py")).read().rsplit('\nif __name__ == "__main__":', 1)[0]
    cwd = os.getcwd()
    try:
        os.chdir(HERE)
        exec(compile(src, "pead_tracker.py", "exec"), ns)
    finally:
        os.chdir(cwd)
    booked, every = [], []
    for a, b in ((DEV[0], DEV[1]), (UNSEEN[0], LAST)):
        evs = [ev for ev in ns["events"](a, b) if ns["qualifies"](ev, "B")]
        every += [(ev["sym"], ev["entry_date"], ev["exit_date"]) for ev in evs]
        evs.sort(key=lambda ev: -ns["score"](ev, "B"))
        res = ns["run"](a, b, evs, 10)
        booked += [(t["sym"], t["entry_date"], t["exit_date"]) for t in res["trades"]]
    return booked, every


def overlap(trades, windows):
    w = {}
    for s, a, b in windows:
        w.setdefault(s, []).append((a, b))
    return sum(1 for t in trades if any(a <= t["entry_date"] <= b for a, b in w.get(t["sym"], [])))


# ------------------------------------------------------------------ report
def summary(ts):
    nets = [t["net"] for t in ts]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    lo, hi = boot([t["net"] - t["spy"] for t in ts])
    return {"n": len(ts), "win_pct": 100 * len(wins) / len(nets), "avg_pct": 100 * st.mean(nets),
            "median_pct": 100 * st.median(nets), "avg_win_pct": 100 * st.mean(wins) if wins else 0.0,
            "avg_loss_pct": 100 * st.mean(losses) if losses else 0.0, "worst_pct": 100 * min(nets),
            "best_pct": 100 * max(nets), "stopped_pct": 100 * sum(t["why"] == "stop" for t in ts) / len(ts),
            "avg_spy_pct": 100 * st.mean(t["spy"] for t in ts),
            "avg_vs_spy_pct": 100 * st.mean(t["net"] - t["spy"] for t in ts), "vs_spy_ci95": (lo, hi)}


def concentration(done):
    g = sorted((t["gbp"] for t in done), reverse=True)
    total = sum(g)
    sectors = {}
    for t in done:
        k = SECTOR.get(t["sym"], "left the index since")
        x = sectors.setdefault(k, [0, 0.0]); x[0] += 1; x[1] += t["gbp"]
    names = {}
    for t in done:
        names[t["sym"]] = names.get(t["sym"], 0.0) + t["gbp"]
    return {"total_gbp": total, "top5_gbp": sum(g[:5]), "without_top5_gbp": total - sum(g[:5]),
            "sectors": sorted(([k, v[0], round(v[1], 1)] for k, v in sectors.items()), key=lambda x: -x[2]),
            "top_names": sorted(names.items(), key=lambda x: -x[1])[:5]}


def years(period, last):
    a = datetime.date.fromisoformat(period[0])
    b = datetime.date.fromisoformat(min(period[1], last))
    return (b - a).days / 365.25


def main():
    sigs, sstats = signals()
    last_filed = max(g["filed"] for g in sigs)
    print("costs each way: %.2f%% (FX 0.15%% + slippage %.2f%%)" % (100 * holds.COST, 100 * SLIP))
    print("clusters found (all companies): %d; not in the S&P 500 that day: %d; no prices or under the $5 / $20m "
          "filters: %d; usable signals: %d; newest filing in the data: %s" % (
              sstats["clusters"], sstats["not_member"], sstats["no_prices_or_filters"], len(sigs), last_filed))
    per_year = {}
    for g in sigs:
        per_year[g["filed"][:4]] = per_year.get(g["filed"][:4], 0) + 1
    print("signals by year (by filing date): %s" % ", ".join("%s %d" % kv for kv in sorted(per_year.items())))
    booked, every = drift_windows()
    out = {"slip_pct": 100 * SLIP, "signals": len(sigs), "signal_stats": sstats, "last_filed": last_filed,
           "by_year": per_year,
           "periods": {}}
    ends = {"DEV": DEV[1], "UNSEEN": LAST}
    periods = {"DEV": DEV, "UNSEEN": (UNSEEN[0], min(UNSEEN[1], LAST))}
    for name, per in periods.items():
        end = ends[name]
        ps = [g for g in sigs if per[0] <= g["filed"] <= per[1]]
        yrs = years(per, last_filed)
        trk = tracker((per[0], end))
        print("=" * 100)
        print("%s: signals filed %s to %s, trades must finish by %s. %d signals, %.1f a year. S&P 500 tracker "
              "over the whole period: %+.1f%% (worst drop %.1f%%)" % (
                  name, per[0], min(per[1], last_filed), end, len(ps), len(ps) / yrs, trk["total_pct"], trk["max_drop_pct"]))
        res = {"signals": len(ps), "per_year": len(ps) / yrs, "tracker": trk, "holds": {}}
        for h in HOLDS:
            ts = [t for g in ps if (t := trade(g["sym"], g["filed"], h, end))]
            s = summary(ts)
            b = book(ts, (per[0], end))
            c = concentration(b["trades"])
            ravg, rbook = random_runs(ps, h, (per[0], end), end)
            s.update({"book_total_pct": b["total_pct"], "book_max_drop_pct": b["max_drop_pct"],
                      "book_trades": len(b["trades"]), "book_per_year": len(b["trades"]) / yrs,
                      "random_avg_pct": st.mean(ravg),
                      "beat_random_avg_pct": 100 * sum(x < s["avg_pct"] for x in ravg) / len(ravg),
                      "random_book_median_pct": st.median(rbook),
                      "beat_random_book_pct": 100 * sum(x < b["total_pct"] for x in rbook) / len(rbook),
                      "concentration": c,
                      "drift_booked_overlap": overlap(ts, booked), "drift_any_signal_overlap": overlap(ts, every)})
            res["holds"][h] = s
            print("-- hold %d days: %d trades (%.1f a year); made money %.0f%%; average %+.2f%% (median %+.2f%%); "
                  "average win %+.1f%% against average loss %+.1f%%; stopped %.0f%%; worst %+.1f%%" % (
                      h, s["n"], s["n"] / yrs, s["win_pct"], s["avg_pct"], s["median_pct"], s["avg_win_pct"],
                      s["avg_loss_pct"], s["stopped_pct"], s["worst_pct"]))
            print("   against the S&P 500 over the same days: %+.2f%% a trade (95%% range %+.2f to %+.2f); "
                  "random buys same days: %+.2f%% a trade (beaten by the signal in %.0f%% of %d runs)" % (
                      s["avg_vs_spy_pct"], s["vs_spy_ci95"][0], s["vs_spy_ci95"][1], s["random_avg_pct"],
                      s["beat_random_avg_pct"], RANDOM_RUNS))
            print("   10-slot book: %+.1f%% (worst drop %.1f%%, %d trades) against the tracker %+.1f%%; random books' "
                  "median %+.1f%% (beaten in %.0f%%)" % (
                      b["total_pct"], b["max_drop_pct"], len(b["trades"]), trk["total_pct"],
                      s["random_book_median_pct"], s["beat_random_book_pct"]))
            print("   book profit GBP %.0f; the top 5 trades GBP %.0f; without them GBP %.0f. Top names: %s" % (
                c["total_gbp"], c["top5_gbp"], c["without_top5_gbp"],
                ", ".join("%s %.0f" % x for x in c["top_names"])))
            print("   by sector (trades, GBP): %s" % "; ".join("%s %d %.0f" % tuple(x) for x in c["sectors"]))
            print("   on a stock drift was holding (its 10-slot book): %d of %d; during any drift signal's 20 days: %d" % (
                s["drift_booked_overlap"], s["n"], s["drift_any_signal_overlap"]))
        out["periods"][name] = res
    best = max(HOLDS, key=lambda h: out["periods"]["DEV"]["holds"][h]["avg_vs_spy_pct"])
    out["chosen_hold_on_dev"] = best
    print("=" * 100)
    print("Chosen on 2019-2023 (best average against the S&P 500 a trade): %d days. Its one check on 2024 on: "
          "%+.2f%% a trade against the S&P 500, book %+.1f%% against the tracker %+.1f%%" % (
              best, out["periods"]["UNSEEN"]["holds"][best]["avg_vs_spy_pct"],
              out["periods"]["UNSEEN"]["holds"][best]["book_total_pct"], out["periods"]["UNSEEN"]["tracker"]["total_pct"]))
    tag = "" if SLIP == 0.0005 else "_slip%g" % (100 * SLIP)
    json.dump(out, open(DATA + "insider_cluster%s.json" % tag, "w"), indent=1, default=str)
    json.dump(sigs, open(DATA + "insider_signals.json", "w"), indent=0)


if __name__ == "__main__":
    main()
