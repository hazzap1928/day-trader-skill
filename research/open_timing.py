"""Opening minutes against later in the morning: is the price gap big enough to matter at 0.3% slippage a side?

Yahoo keeps 1-minute prices for about 30 days, so this can only look at the last four weeks or so (in Nov 2026:
24 Oct to 20 Nov, the main Q3 earnings season). Two samples:

  1. drift buy days: every signal of the tested drift rule (research/pead_stop.py's) whose buy day is inside
     the window, plus the live agent's own shortlisted signals (trader/state/drift/signals.jsonl)
  2. a broad sample: the allow-list's US shares on every day of the window (for context)

For each share and day, the price at 09:33 (the minute drift's order goes in: that minute's first trade) is
compared with the first trade at 09:45, 10:00 and 10:30, and the opening minutes' own price swings are measured
(each minute's high against its low, 09:31 to 09:35, against the same at 10:00).

Signed averages say whether waiting was cheaper ON AVERAGE (a lasting edge); the average size of the gap says how
much the price you get depends on the minute (noise). Gaps are compared with 0.3%, the slippage at which the
drift backtest's lead over the S&P 500 disappears. Four weeks is a very small sample: one earnings season.

    python3 research/open_timing.py      # network: Yahoo 1-minute bars (cached in trader/research-data/minutes/)
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _data import DATA
import datetime, importlib.util, json, statistics as st, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CACHE = os.path.join(DATA, "minutes")
os.makedirs(CACHE, exist_ok=True)
TIMES = ("09:45", "10:00", "10:30")
BAR = 0.30


def _mod(folder, name):
    spec = importlib.util.spec_from_file_location("ot_" + name, os.path.join(ROOT, folder, name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


tk = _mod("trader-broker", "ticket")
alerts = _mod("trader-alerts", "alerts")


def window(today):
    days, d = [], today - datetime.timedelta(days=29)
    while d < today:
        if alerts.trading_day(d)[0]:
            days.append(d)
        d += datetime.timedelta(days=1)
    return days


def fetch_week(sym, a, b):
    """1-minute bars from a to b (dates, at most 7 days apart) -> {"YYYY-MM-DD HH:MM" NY: [o, h, l, c]}."""
    path = os.path.join(CACHE, "%s_%s_%s.json" % (sym, a, b))
    if os.path.exists(path):
        return json.load(open(path))
    p1 = int(datetime.datetime.combine(a, datetime.time(0, 0)).replace(tzinfo=datetime.timezone.utc).timestamp())
    p2 = int(datetime.datetime.combine(b + datetime.timedelta(days=1), datetime.time(0, 0)).replace(tzinfo=datetime.timezone.utc).timestamp())
    url = "https://query2.finance.yahoo.com/v8/finance/chart/%s?period1=%d&period2=%d&interval=1m" % (sym.replace(".", "-"), p1, p2)
    out = {}
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; day-trader-skill/2.0)"})
            r = json.loads(urllib.request.urlopen(req, timeout=30).read())["chart"]["result"][0]
            q = r["indicators"]["quote"][0]
            for t, o, h, l, c in zip(r.get("timestamp") or [], q["open"], q["high"], q["low"], q["close"]):
                if None not in (o, h, l, c):
                    out[tk.to_eastern(datetime.datetime.utcfromtimestamp(t)).strftime("%Y-%m-%d %H:%M")] = [o, h, l, c]
            break
        except Exception:
            time.sleep(3 + 5 * attempt)
    json.dump(out, open(path, "w"))
    time.sleep(0.4)
    return out


def bars_for(sym, days):
    out = {}
    for i in range(0, len(days), 5):
        chunk = days[i:i + 5]
        out.update(fetch_week(sym, chunk[0], chunk[-1]))
    return out


def measure(bars, day):
    k = lambda t: "%s %s" % (day, t)
    if k("09:33") not in bars:
        return None
    p0 = bars[k("09:33")][0]
    row = {"p0933": p0}
    for t in TIMES:
        if k(t) in bars:
            row[t] = 100 * (bars[k(t)][0] / p0 - 1)
    opening = [bars[k("09:3%d" % m)] for m in range(1, 6) if k("09:3%d" % m) in bars]
    if opening:
        row["open_swing"] = st.mean(100 * (b[1] - b[2]) / ((b[1] + b[2]) / 2) for b in opening)
    if k("10:00") in bars:
        b = bars[k("10:00")]
        row["ten_swing"] = 100 * (b[1] - b[2]) / ((b[1] + b[2]) / 2)
    return row


def report(label, rows):
    print("%s: %d share-days" % (label, len(rows)))
    if not rows:
        return {}
    out = {"n": len(rows)}
    for t in TIMES:
        xs = [r[t] for r in rows if t in r]
        if len(xs) < 2:
            continue
        mean, sd = st.mean(xs), st.stdev(xs)
        se = sd / len(xs) ** 0.5
        out[t] = {"n": len(xs), "mean": mean, "median": st.median(xs), "mean_abs": st.mean(abs(x) for x in xs),
                  "se": se, "cheaper_by_bar": sum(1 for x in xs if x < -BAR) / len(xs),
                  "dearer_by_bar": sum(1 for x in xs if x > BAR) / len(xs)}
        print("  %s against 09:33: average %+.2f%% (give or take %.2f), median %+.2f%%; typical size of the gap %.2f%%; "
              "cheaper by over %.1f%% on %.0f%% of days, dearer by over %.1f%% on %.0f%%" % (
                  t, mean, 2 * se, st.median(xs), out[t]["mean_abs"], BAR, 100 * out[t]["cheaper_by_bar"], BAR,
                  100 * out[t]["dearer_by_bar"]))
    sw = [r["open_swing"] for r in rows if "open_swing" in r]
    tw = [r["ten_swing"] for r in rows if "ten_swing" in r]
    if sw and tw:
        out["open_swing"], out["ten_swing"] = st.mean(sw), st.mean(tw)
        print("  one minute's high against its low: %.2f%% on average in the opening minutes (09:31-09:35), %.2f%% at 10:00"
              % (st.mean(sw), st.mean(tw)))
    return out


if __name__ == "__main__":
    today = datetime.date.today()
    days = window(today)
    print("window: %s to %s (%d trading days; Yahoo keeps 1-minute prices about 30 days)" % (days[0], days[-1], len(days)))
    # 1. drift buy days
    g = {"__file__": os.path.join(HERE, "pead_stop.py"), "__name__": "ot"}
    sys.argv = ["x"]
    exec(open(os.path.join(HERE, "pead_stop.py")).read().rsplit('\nif __name__ == "__main__":', 1)[0], g)
    first = days[0].isoformat()
    # the drift rule's numbers (a beat, surprise of 10% or more, forecast at least $0.10 either side of zero, an S&P
    # member that day), bought on the 2nd trading day after the report; the 20-day hold need not have ended yet
    sig, cal = set(), [d.isoformat() for d in days]
    allcal = g["CAL"] + [d for d in cal if d > g["CAL"][-1]]
    ci = {d: i for i, d in enumerate(allcal)}
    for day, rows in g["E"].items():
        if day not in ci or ci[day] + 2 >= len(allcal):
            continue
        entry = allcal[ci[day] + 2]
        if not (first <= entry < today.isoformat()):
            continue
        for x in rows or []:
            sym = str(x.get("symbol") or "").upper().replace(".", "-")
            sur, eps, fc = g["num"](x.get("surprise")), g["num"](x.get("eps")), g["num"](x.get("epsForecast"))
            if None in (sur, eps, fc) or abs(fc) < 0.10 or sur < 10 or eps <= fc or not g["member"](sym, day):
                continue
            sig.add((sym, entry))
    try:
        for line in open(os.path.join(ROOT, "trader", "state", "drift", "signals.jsonl")):
            r = json.loads(line)
            if r.get("entry_day", "") >= first and r.get("entry_day", "") < today.isoformat():
                sig.add((r["id"].split("-", 3)[-1], r["entry_day"]))
    except OSError:
        pass
    drift_rows = []
    for sym in sorted({s for s, _ in sig}):
        bars = bars_for(sym, days)
        for s, d in sig:
            if s == sym:
                m = measure(bars, d)
                if m:
                    drift_rows.append(m)
    print()
    res = {"window": [str(days[0]), str(days[-1])], "drift": report("DRIFT BUY DAYS (the morning after a beat)", drift_rows)}
    # 2. broad sample
    allow = [s for s in json.load(open(os.path.join(ROOT, "trader", "config.json")))["allowed_tickers"]
             if s not in ("SPY", "QQQ", "IWM", "DIA", "VTI", "VOO", "RSP", "XLK", "XLF", "XLE", "XLV", "SMH", "TLT", "GLD")]
    broad = []
    for sym in allow:
        bars = bars_for(sym, days)
        for d in days:
            m = measure(bars, d.isoformat())
            if m:
                broad.append(m)
    print()
    res["broad"] = report("BROAD SAMPLE (%d large US shares, every day)" % len(allow), broad)
    json.dump(res, open(os.path.join(DATA, "open_timing.json"), "w"), indent=1)
    print("\nSaved %sopen_timing.json. One earnings season only; Yahoo's 1-minute data is unofficial." % DATA)
