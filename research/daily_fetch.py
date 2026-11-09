"""5 years of daily bars for every allowed ticker (Yahoo, read-only), plus QQQ/VTI benchmarks."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _data import DATA
import json, sys, time, urllib.request, urllib.error
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
syms = json.load(open(ROOT + "/trader/config.json"))["allowed_tickers"]
OUT = "daily5y.json"
out, errs = {}, {}
for i, s in enumerate(syms):
    url = "https://query1.finance.yahoo.com/v8/finance/chart/%s?range=5y&interval=1d" % s
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; day-trader-skill/2.0)"})
            r = json.loads(urllib.request.urlopen(req, timeout=30).read())["chart"]["result"][0]
            q = r["indicators"]["quote"][0]
            rows = [(t, o, h, l, c) for t, o, h, l, c in zip(r["timestamp"], q["open"], q["high"], q["low"], q["close"])
                    if None not in (o, h, l, c)]
            out[s] = rows
            errs.pop(s, None)
            break
        except Exception as e:
            errs[s] = str(e)[:100]
            time.sleep(3 + 5 * attempt)
    time.sleep(0.5)
json.dump(out, open(DATA + OUT, "w"))
print(len(out), "symbols;", "errors:", errs)
print({s: len(v) for s, v in list(out.items())[:5]})
