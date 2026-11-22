"""Kronos's opinion on a holding, on DAILY bars: shadow only. It never places, blocks or changes anything.

The same forecast research/kronos_backtest.py tested (Kronos-small, 256 daily bars, 5 sampled paths): how
far the share is expected to move between the last bar given and the end of its planned holding. Runs in
trader-kronos/.venv (Python 3.10+). trader-broker/shadow.py sends, as JSON on stdin:

  {"budget_s": 240, "items": [{"id", "bars": [[date, open, high, low, close, volume], ...],
                               "future": [date, ...]}]}

and reads one JSON line back (the only thing on stdout; library chatter goes to stderr):

  {"model", "load_seconds", "results": [{"id", "score", "path_returns", "paths", "pred_len", "as_of",
                                          "seconds"} or {"id", "error"}]}

score = the median over the paths of (forecast close on the last future day / last close given - 1).
The model is loaded once for all items. An item that would not finish inside budget_s is left out (no
entry in results), so the caller can try it another day. Education and tooling, not financial advice.

    .venv/bin/python kronos_daily.py --self-test      # one forecast on a fixed synthetic series
"""

import contextlib
import json
import math
import os
import statistics
import sys
import time
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "vendor", "Kronos"))
PATHS = 5
LOOKBACK = 256
T, TOP_P = 1.0, 0.9


def load_predictor():
    from model import Kronos, KronosTokenizer, KronosPredictor
    tok = KronosTokenizer.from_pretrained(os.path.join(HERE, "models", "Kronos-Tokenizer-base"))
    mdl = Kronos.from_pretrained(os.path.join(HERE, "models", "Kronos-small"))
    return "Kronos-small/Tokenizer-base", KronosPredictor(mdl, tok, device="cpu", max_context=512)


def forecast(predictor, item):
    import pandas as pd
    import torch
    bars = [b for b in item["bars"] if None not in b[1:5]][-LOOKBACK:]
    fut = list(item["future"])
    if len(bars) < 60:
        return {"id": item["id"], "error": "only %d daily bars" % len(bars)}
    if not fut:
        return {"id": item["id"], "error": "no future days to forecast"}
    x = pd.DataFrame([{"open": b[1], "high": b[2], "low": b[3], "close": b[4], "volume": b[5] or 0} for b in bars])
    xt = pd.Series(pd.to_datetime([b[0] for b in bars]))
    yt = pd.Series(pd.to_datetime(fut))
    torch.manual_seed(zlib.crc32(("%s|%s" % (item["id"], bars[-1][0])).encode()))
    t0 = time.time()
    out = predictor.predict_batch(df_list=[x] * PATHS, x_timestamp_list=[xt] * PATHS, y_timestamp_list=[yt] * PATHS,
                                  pred_len=len(fut), T=T, top_p=TOP_P, sample_count=1, verbose=False)
    last = float(bars[-1][4])
    rets = [float(p["close"].iloc[-1]) / last - 1 for p in out]
    return {"id": item["id"], "score": round(statistics.median(rets), 6), "path_returns": [round(r, 6) for r in rets],
            "paths": PATHS, "pred_len": len(fut), "as_of": bars[-1][0], "seconds": round(time.time() - t0, 1)}


def synthetic():
    import datetime
    d, bars, p = datetime.date(2026, 1, 2), [], 100.0
    while len(bars) < 300:
        if d.weekday() < 5:
            o = p
            p = p * (1 + 0.01 * math.sin(len(bars) / 9.0))
            bars.append([d.isoformat(), o, max(o, p) * 1.003, min(o, p) * 0.997, p, 1e6])
        d += datetime.timedelta(days=1)
    fut = []
    while len(fut) < 20:
        if d.weekday() < 5:
            fut.append(d.isoformat())
        d += datetime.timedelta(days=1)
    return {"budget_s": 600, "items": [{"id": "SELFTEST", "bars": bars, "future": fut}]}


def main():
    t0 = time.time()
    with contextlib.redirect_stdout(sys.stderr):
        req = synthetic() if "--self-test" in sys.argv else json.loads(sys.stdin.read())
        budget = float(req.get("budget_s") or 600)
        name, predictor = load_predictor()
        load_s = round(time.time() - t0, 1)
        results, per = [], None
        for item in req.get("items") or []:
            if per is not None and time.time() - t0 + 1.5 * per > budget:
                break
            try:
                r = forecast(predictor, item)
            except Exception as e:
                r = {"id": item.get("id"), "error": str(e)[:200]}
            results.append(r)
            if r.get("seconds"):
                per = r["seconds"] if per is None else max(per, r["seconds"])
    sys.stdout.write(json.dumps({"model": name, "load_seconds": load_s, "results": results}) + "\n")


if __name__ == "__main__":
    main()
