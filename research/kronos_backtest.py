"""Kronos as a stock picker for the hold agent: a monthly backtest on daily bars. Research only.

Nothing here places an order, reads a key, or changes the agent, its config or its state. Not financial advice.

DESIGN (fixed before any result was seen)
- Model: the trader-kronos setup (Kronos-small with Kronos-Tokenizer-base, CPU), on DAILY bars.
  Run it with that folder's own Python:  trader-kronos/.venv/bin/python research/kronos_backtest.py
- Universe: 50 stocks drawn at random (seed 42) from today's S&P 500 list (sp500.csv), among the members
  with a daily bar on every trading day from 256 days before the first test month to the last sell day.
  Survivorship bias: today's members are companies that survived to today, which flatters every long
  strategy here, Kronos's picks and the comparisons alike. SPY does not have this problem.
- Data: the daily bars research/prices_fetch.py writes to trader/research-data/prices.json (Yahoo,
  unofficial; prices scaled for dividends and splits). Fetched with /usr/bin/python3 if missing.
- Training cutoff: the Kronos paper (arXiv 2508.02739v1) says its pre-training data extends to June 2024.
  The maintainers have not confirmed that the released Kronos-small weights use that same cutoff (GitHub
  issue shiyu-coder/Kronos#265, open in Nov 2026), so the cutoff is treated as unclear and the test starts in
  2025-01. No month before 2025-01 is scored.
- Each test month, on its first trading day (D): for each stock, the last 256 daily bars up to and
  including D go to Kronos, which samples 5 paths 20 trading days ahead (temperature 1.0, top_p 0.9).
  Score = the median over the 5 paths of (forecast close on day 20 / close on D - 1).
- Portfolio: the top 10 by score, bought at the open of D+1, equal weight, sold at the close of the 20th
  trading day held (D+1 is day 1, so the sale is at the close of D+20). The forecast covers exactly those
  20 days. Costs: 0.15% FX fee + 0.05% slippage on each buy and each sell (research/README.md).
- A month counts only when its sale day is a final close before today: 2025-01 to 2026-10 as of Nov 2026.
- Compared over the same months and days:
  - the other 40 stocks, equal weight, same costs
  - 1,000 random portfolios of 10 from the same 50 (a fresh draw each month, seed 42, same costs); the
    report gives the percentile Kronos lands at
  - SPY, at 0.05% slippage a leg (a GBP-listed tracker pays no FX fee)
  - the rank correlation (Spearman) between score and the actual 20-day return (D+1 open to D+20 close,
    before costs) in each month, and its average
- Practicalities: the first forecast of each start is timed and an estimate of the rest is printed. Every
  forecast is cached in trader/research-data/kronos_cache.jsonl, so a stop or sleep resumes where it left
  off. The run stops by itself before 05:30 UK time (ahead of the 06:00 news run) and resumes from the
  cache when started again. Run it under caffeinate and nice so the autopilot and guard keep priority.
  When every forecast is cached it prints the results and writes kronos_results.json.

    caffeinate -i -s nice -n 19 trader-kronos/.venv/bin/python research/kronos_backtest.py
    trader-kronos/.venv/bin/python research/kronos_backtest.py --time      # one timed forecast, then stop
    trader-kronos/.venv/bin/python research/kronos_backtest.py --results   # results from the cache only
"""

import contextlib
import csv
import json
import os
import random
import statistics
import subprocess
import sys
import time
import zlib
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from _data import DATA  # noqa: E402

KRONOS = os.path.join(ROOT, "trader-kronos")
CACHE = os.path.join(DATA, "kronos_cache.jsonl")
RESULTS = os.path.join(DATA, "kronos_results.json")

SEED = 42
N_STOCKS = 50
TOP = 10
LOOKBACK = 256
HOLD = 20
PATHS = 5
T, TOP_P = 1.0, 0.9
START_MONTH = "2025-01"
K = 0.0015 + 0.0005          # FX fee + slippage, each buy and each sell
K_SPY = 0.0005               # slippage only, each leg
N_RANDOM = 1000
STOP_AT = (5, 30)            # UK time
UK = ZoneInfo("Europe/London")
MODEL_TAG = "Kronos-small/Tokenizer-base"


def log(*a):
    print(datetime.now(UK).strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


# ---------- data ----------

def load_prices():
    p = os.path.join(DATA, "prices.json")
    if not os.path.exists(p):
        log("no prices.json yet: fetching with /usr/bin/python3 research/prices_fetch.py (about 10 minutes)")
        subprocess.run(["/usr/bin/python3", os.path.join(HERE, "prices_fetch.py")], check=True)
    raw = json.load(open(p))
    out = {}
    for s, rows in raw.items():
        out[s] = {datetime.fromtimestamp(r[0], timezone.utc).date().isoformat(): r[1:] for r in rows}
    return out   # sym -> {date: [open, high, low, close, volume]}


def plan(prices, today=None):
    """Calendar, test months and the fixed 50-stock sample."""
    today = (today or date.today()).isoformat()
    cal = sorted(d for d in prices["SPY"] if d < today)
    months, seen = [], set()
    for i, d in enumerate(cal):
        m = d[:7]
        if m < START_MONTH or m in seen:
            continue
        seen.add(m)
        if i + HOLD < len(cal):     # the sale (close of D+20) must be a final close before today
            months.append(i)
    if not months:
        raise SystemExit("no complete test month in the data")
    first, last = months[0] - LOOKBACK + 1, months[-1] + HOLD
    need = cal[first:last + 1]
    members = [r["Symbol"].replace(".", "-") for r in csv.DictReader(open(os.path.join(DATA, "sp500.csv")))]
    eligible = sorted(s for s in set(members) if s in prices and all(d in prices[s] for d in need))
    sample = sorted(random.Random(SEED).sample(eligible, N_STOCKS))
    return cal, months, sample, len(eligible), len(set(members))


# ---------- the cache ----------

def key(sym, d):
    return "%s|%s" % (sym, d)


def read_cache():
    out = {}
    if os.path.exists(CACHE):
        for line in open(CACHE):
            try:
                r = json.loads(line)
            except ValueError:
                continue      # a line cut short by a stop
            if r.get("model") == MODEL_TAG and r.get("paths") == PATHS and r.get("lookback") == LOOKBACK:
                out[key(r["symbol"], r["date"])] = r
    return out


def append_cache(rec):
    with open(CACHE, "a") as f:
        f.write(json.dumps(rec) + "\n")
        f.flush()
        os.fsync(f.fileno())


# ---------- Kronos ----------

def load_predictor():
    sys.path.insert(0, os.path.join(KRONOS, "vendor", "Kronos"))
    with contextlib.redirect_stdout(sys.stderr):
        from model import Kronos, KronosTokenizer, KronosPredictor
        tok = KronosTokenizer.from_pretrained(os.path.join(KRONOS, "models", "Kronos-Tokenizer-base"))
        mdl = Kronos.from_pretrained(os.path.join(KRONOS, "models", "Kronos-small"))
        return KronosPredictor(mdl, tok, device="cpu", max_context=512)


def forecast(predictor, prices, cal, i, sym):
    import pandas as pd
    import torch
    days = cal[i - LOOKBACK + 1:i + 1]
    fut = cal[i + 1:i + 1 + HOLD]       # known trading dates only, no prices from them
    bars = [prices[sym][d] for d in days]
    x = pd.DataFrame([{"open": b[0], "high": b[1], "low": b[2], "close": b[3], "volume": b[4]} for b in bars])
    xt = pd.Series(pd.to_datetime(days))
    yt = pd.Series(pd.to_datetime(fut))
    torch.manual_seed(zlib.crc32(key(sym, cal[i]).encode()))
    t0 = time.time()
    with contextlib.redirect_stdout(sys.stderr):
        out = predictor.predict_batch(df_list=[x] * PATHS, x_timestamp_list=[xt] * PATHS,
                                      y_timestamp_list=[yt] * PATHS, pred_len=HOLD, T=T, top_p=TOP_P,
                                      sample_count=1, verbose=False)
    last = bars[-1][3]
    rets = [float(p["close"].iloc[-1]) / last - 1 for p in out]
    return {"symbol": sym, "date": cal[i], "model": MODEL_TAG, "paths": PATHS, "lookback": LOOKBACK,
            "pred_len": HOLD, "path_returns": [round(r, 6) for r in rets], "score": round(statistics.median(rets), 6),
            "seconds": round(time.time() - t0, 2)}


def deadline(now):
    d = now.replace(hour=STOP_AT[0], minute=STOP_AT[1], second=0, microsecond=0)
    return d if d > now else d + timedelta(days=1)


def run(prices, cal, months, sample, time_only=False):
    cache = read_cache()
    todo = [(i, s) for i in months for s in sample if key(s, cal[i]) not in cache]
    total = len(months) * len(sample)
    log("%d forecasts in all (%d months x %d stocks); %d cached, %d to do"
        % (total, len(months), len(sample), total - len(todo), len(todo)))
    if not todo:
        return True
    stop = deadline(datetime.now(UK))
    log("will stop by itself before %s UK" % stop.strftime("%Y-%m-%d %H:%M"))
    t0 = time.time()
    predictor = load_predictor()
    log("model loaded in %.0f s" % (time.time() - t0))
    per = None
    for n, (i, s) in enumerate(todo):
        if per is not None and datetime.now(UK) + timedelta(seconds=1.5 * per) >= stop:
            log("stopping for the 05:30 limit: %d of %d done, %d left. Start the same command again to resume."
                % (total - len(todo) + n, total, len(todo) - n))
            return False
        rec = forecast(predictor, prices, cal, i, s)
        append_cache(rec)
        per = rec["seconds"] if per is None else 0.8 * per + 0.2 * rec["seconds"]
        if n == 0:
            left = len(todo) - 1
            log("timing: one forecast (%s %s, %d paths x %d days) took %.1f s; estimate for the other %d: %.1f hours"
                % (s, cal[i], PATHS, HOLD, rec["seconds"], left, left * rec["seconds"] / 3600))
            if time_only:
                return False
        elif n % 25 == 0:
            left = len(todo) - n - 1
            log("%d of %d done, about %.1f s each, %.1f hours left" % (total - left, total, per, left * per / 3600))
    return True


# ---------- results ----------

def spearman(a, b):
    def ranks(v):
        order = sorted(range(len(v)), key=lambda j: v[j])
        r = [0.0] * len(v)
        j = 0
        while j < len(order):
            k = j
            while k + 1 < len(order) and v[order[k + 1]] == v[order[j]]:
                k += 1
            for m in range(j, k + 1):
                r[order[m]] = (j + k) / 2.0
            j = k + 1
        return r
    ra, rb = ranks(a), ranks(b)
    ma, mb = statistics.mean(ra), statistics.mean(rb)
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    den = (sum((x - ma) ** 2 for x in ra) * sum((y - mb) ** 2 for y in rb)) ** 0.5
    return num / den if den else 0.0


def net(buy, sell, k):
    return sell * (1 - k) / (buy * (1 + k)) - 1


def compound(rs):
    v = 1.0
    for r in rs:
        v *= 1 + r
    return v - 1


def max_dd(rs):
    v = peak = 1.0
    worst = 0.0
    for r in rs:
        v *= 1 + r
        peak = max(peak, v)
        worst = max(worst, 1 - v / peak)
    return worst


def results(prices, cal, months, sample, n_eligible, n_members):
    cache = read_cache()
    missing = sum(1 for i in months for s in sample if key(s, cal[i]) not in cache)
    if missing:
        raise SystemExit("%d forecasts are not cached yet: run the backtest again to finish them" % missing)
    rows, kron, rest, spy, ics = [], [], [], [], []
    per_month_net = []
    for i in months:
        d, buy_d, sell_d = cal[i], cal[i + 1], cal[i + HOLD]
        sc = {s: cache[key(s, d)]["score"] for s in sample}
        gross = {s: prices[s][sell_d][3] / prices[s][buy_d][0] - 1 for s in sample}
        nets = {s: net(prices[s][buy_d][0], prices[s][sell_d][3], K) for s in sample}
        ranked = sorted(sample, key=lambda s: (-sc[s], s))
        top, others = ranked[:TOP], ranked[TOP:]
        k_r = statistics.mean(nets[s] for s in top)
        o_r = statistics.mean(nets[s] for s in others)
        s_r = net(prices["SPY"][buy_d][0], prices["SPY"][sell_d][3], K_SPY)
        ic = spearman([sc[s] for s in sample], [gross[s] for s in sample])
        kron.append(k_r); rest.append(o_r); spy.append(s_r); ics.append(ic)
        per_month_net.append([nets[s] for s in sample])
        rows.append({"month": d[:7], "decided": d, "buy": buy_d, "sell": sell_d, "top10": top,
                     "kronos_net": round(k_r, 5), "other40_net": round(o_r, 5), "spy_net": round(s_r, 5),
                     "rank_corr": round(ic, 4)})
    rng = random.Random(SEED)
    rand_tot = []
    for _ in range(N_RANDOM):
        rs = [statistics.mean(rng.sample(m, TOP)) for m in per_month_net]
        rand_tot.append(compound(rs))
    k_tot = compound(kron)
    pct = 100.0 * sum(1 for r in rand_tot if r < k_tot) / N_RANDOM
    ic_mean = statistics.mean(ics)
    ic_t = ic_mean / (statistics.stdev(ics) / len(ics) ** 0.5) if len(ics) > 1 and statistics.stdev(ics) else 0.0
    summary = {
        "months": len(months), "first": rows[0]["month"], "last": rows[-1]["month"],
        "sample": sample, "eligible": n_eligible, "members": n_members,
        "kronos_total": k_tot, "other40_total": compound(rest), "spy_total": compound(spy),
        "kronos_avg_month": statistics.mean(kron), "other40_avg_month": statistics.mean(rest),
        "spy_avg_month": statistics.mean(spy),
        "kronos_max_dd": max_dd(kron), "spy_max_dd": max_dd(spy),
        "months_beat_spy": sum(1 for a, b in zip(kron, spy) if a > b),
        "months_beat_other40": sum(1 for a, b in zip(kron, rest) if a > b),
        "random_median_total": statistics.median(rand_tot), "random_percentile": pct,
        "rank_corr_mean": ic_mean, "rank_corr_t": ic_t,
        "rank_corr_positive_months": sum(1 for x in ics if x > 0),
        "rows": rows,
    }
    json.dump(summary, open(RESULTS, "w"), indent=1)
    p = lambda x: "%+.1f%%" % (100 * x)
    print()
    print("KRONOS STOCK PICKER, daily bars: %s to %s (%d months), after costs" % (summary["first"], summary["last"], len(months)))
    print("Universe: %d of %d eligible S&P 500 members (of %d today), seed %d. Survivorship bias applies."
          % (N_STOCKS, n_eligible, n_members, SEED))
    print()
    print("%-5s %-8s %-8s %-8s %-7s  %s" % ("month", "Kronos", "other40", "SPY", "rankcor", "top 10"))
    for r in rows:
        print("%-7s %-8s %-8s %-8s %+.2f   %s" % (r["month"], p(r["kronos_net"]), p(r["other40_net"]), p(r["spy_net"]),
                                              r["rank_corr"], " ".join(r["top10"])))
    print()
    print("Total, compounded month by month (held 20 of about 21 trading days a month):")
    print("  Kronos top 10   %s  (avg %s a month, worst fall %s)" % (p(k_tot), p(summary["kronos_avg_month"]), p(-summary["kronos_max_dd"])))
    print("  Other 40        %s  (avg %s a month)" % (p(summary["other40_total"]), p(summary["other40_avg_month"])))
    print("  SPY             %s  (avg %s a month, worst fall %s)" % (p(summary["spy_total"]), p(summary["spy_avg_month"]), p(-summary["spy_max_dd"])))
    print("  Random 10 of 50 median %s; Kronos beats %.1f%% of %d random portfolios" % (p(summary["random_median_total"]), pct, N_RANDOM))
    print("  Kronos beat SPY in %d of %d months, the other 40 in %d" % (summary["months_beat_spy"], len(months), summary["months_beat_other40"]))
    print("Rank correlation, score vs actual 20-day return: average %+.3f (t %.2f), positive in %d of %d months"
          % (ic_mean, ic_t, summary["rank_corr_positive_months"], len(months)))
    print("Written to", RESULTS)


def main():
    prices = load_prices()
    cal, months, sample, n_el, n_mem = plan(prices)
    log("test months %s to %s; sample: %s" % (cal[months[0]][:7], cal[months[-1]][:7], " ".join(sample)))
    if "--results" in sys.argv:
        results(prices, cal, months, sample, n_el, n_mem)
        return
    done = run(prices, cal, months, sample, time_only="--time" in sys.argv)
    if done:
        results(prices, cal, months, sample, n_el, n_mem)


if __name__ == "__main__":
    main()
