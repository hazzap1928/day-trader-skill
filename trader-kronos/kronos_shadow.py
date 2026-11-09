"""Kronos in shadow mode: a verdict on one autopilot signal. It never places, blocks or changes anything.

Runs in trader-kronos/.venv (Python 3.10+, set up by setup.sh). trader-broker/evidence.py calls it with
one signal as JSON on stdin:  {"symbol", "at" (unix time of the signal), "entry", "stop", "target"}
and reads one JSON line back:
  {"model", "paths", "pred_len", "p_target", "p_stop", "avg_R", "avg_R_net", "seconds"}

How: the last up-to-400 five-minute bars before the signal (Yahoo, unofficial) go to Kronos, which samples
PATHS possible futures up to the 15:50 New York close. Each path is walked like the real trade: the stop
first is -1R, the target first is +target R, else out at 15:50. Costs as the GBP account pays them:
0.15% FX fee plus 0.05% slippage on the buy and on the sell. Education and tooling, not financial advice.

    .venv/bin/python kronos_shadow.py --self-test      # one forecast on a fixed synthetic series
"""

import json
import math
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "vendor", "Kronos"))
PATHS = 10
LOOKBACK = 400
K = 0.0015 + 0.0005
UA = "Mozilla/5.0 (compatible; day-trader-skill/2.0)"
EOD = (15, 50)


def ny_offset(utc):
    """Hours New York is behind UTC (US daylight saving: second Sunday of March to first Sunday of November)."""
    y = utc.year

    def nth_sunday(month, n):
        d = datetime(y, month, 1)
        d += timedelta(days=(6 - d.weekday()) % 7)
        return d + timedelta(weeks=n - 1)
    start = nth_sunday(3, 2) + timedelta(hours=7)
    end = nth_sunday(11, 1) + timedelta(hours=6)
    return 4 if start <= utc.replace(tzinfo=None) < end else 5


def to_ny(ts):
    u = datetime.fromtimestamp(ts, timezone.utc).replace(tzinfo=None)
    return u - timedelta(hours=ny_offset(u))


def fetch(symbol):
    url = "https://query1.finance.yahoo.com/v8/finance/chart/%s?range=5d&interval=5m" % symbol
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=30) as r:
        res = json.loads(r.read().decode("utf-8"))["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    return [(t, o, h, l, c, v or 0) for t, o, h, l, c, v in zip(res["timestamp"], q["open"], q["high"], q["low"],
                                                                   q["close"], q["volume"]) if None not in (o, h, l, c)]


def load_predictor():
    from model import Kronos, KronosTokenizer, KronosPredictor
    which = "small"
    p = os.path.join(HERE, "models", "MODEL")
    if os.path.exists(p):
        which = open(p).read().strip() or "small"
    names = {"small": ("Kronos-small", "Kronos-Tokenizer-base", 512), "mini": ("Kronos-mini", "Kronos-Tokenizer-2k", 2048)}
    m, t, ctx = names[which]
    tokenizer = KronosTokenizer.from_pretrained(os.path.join(HERE, "models", t))
    model = Kronos.from_pretrained(os.path.join(HERE, "models", m))
    return "Kronos-" + which, KronosPredictor(model, tokenizer, device="cpu", max_context=ctx)


def walk(path, entry, stop, target):
    """One sampled future, bar by bar: (R, R after costs)."""
    risk = entry - stop
    exit_ = None
    for o, h, l, c in path:
        if l <= stop:
            exit_ = min(stop, o)
            break
        if h >= target:
            exit_ = target
            break
        exit_ = c
    if exit_ is None:
        exit_ = entry
    R = (exit_ - entry) / risk
    return R, R - K * (entry + exit_) / risk


def verdict(bars, at, entry, stop, target, predictor, name):
    import pandas as pd
    past = [b for b in bars if b[0] < at][-LOOKBACK:]
    if len(past) < 50:
        raise SystemExit(json.dumps({"error": "only %d bars before the signal" % len(past)}))
    t0 = past[-1][0] + 300
    steps = []
    t = t0
    while (to_ny(t).hour, to_ny(t).minute) < EOD and len(steps) < 80:
        steps.append(t)
        t += 300
    if not steps:
        raise SystemExit(json.dumps({"error": "no time left before 15:50"}))
    x = pd.DataFrame([{"open": b[1], "high": b[2], "low": b[3], "close": b[4], "volume": b[5]} for b in past])
    x_ts = pd.Series(pd.to_datetime([to_ny(b[0]) for b in past]))
    y_ts = pd.Series(pd.to_datetime([to_ny(s) for s in steps]))
    out = predictor.predict_batch(df_list=[x] * PATHS, x_timestamp_list=[x_ts] * PATHS, y_timestamp_list=[y_ts] * PATHS,
                                  pred_len=len(steps), T=1.0, top_p=0.9, sample_count=1, verbose=False)
    rs = [walk(list(zip(p["open"], p["high"], p["low"], p["close"])), entry, stop, target) for p in out]
    hits_t = sum(1 for p in out if (p["high"] >= target).any() and
                 (not (p["low"] <= stop).any() or (p["high"] >= target).idxmax() < (p["low"] <= stop).idxmax()))
    hits_s = sum(1 for p in out if (p["low"] <= stop).any() and
                 (not (p["high"] >= target).any() or (p["low"] <= stop).idxmax() <= (p["high"] >= target).idxmax()))
    return {"model": name, "paths": PATHS, "pred_len": len(steps), "p_target": hits_t / PATHS, "p_stop": hits_s / PATHS,
            "avg_R": round(sum(r for r, _ in rs) / len(rs), 3), "avg_R_net": round(sum(n for _, n in rs) / len(rs), 3)}


def self_test(predictor, name):
    base = 1700000000 - (1700000000 % 300)
    bars, p = [], 100.0
    for i in range(300):
        o = p
        p = p * (1 + 0.001 * math.sin(i / 7.0))
        bars.append((base + 300 * i, o, max(o, p) * 1.0005, min(o, p) * 0.9995, p, 1000 + i))
    at = bars[-1][0] + 300
    while (to_ny(at).hour, to_ny(at).minute) >= EOD or to_ny(at).hour < 9:
        at -= 3600
    bars = [b for b in bars if b[0] < at]
    return verdict(bars, at, bars[-1][4], bars[-1][4] * 0.99, bars[-1][4] * 1.02, predictor, name)


def main():
    t0 = time.time()
    name, predictor = load_predictor()
    if "--self-test" in sys.argv:
        res = self_test(predictor, name)
    else:
        sig = json.loads(sys.stdin.read())
        bars = [tuple(b) for b in sig["bars"]] if sig.get("bars") else fetch(sig["symbol"])   # evidence.py sends them
        res = verdict(bars, float(sig["at"]), float(sig["entry"]), float(sig["stop"]),
                      float(sig["target"]), predictor, name)
    res["seconds"] = round(time.time() - t0, 1)
    print(json.dumps(res))


if __name__ == "__main__":
    main()
