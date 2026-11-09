"""Daily bars 2017-06 to now for the S&P 500 (today's list), the allow-list and benchmarks. Yahoo, read-only.
OHLC are scaled by adjclose/close so returns include dividends and splits."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _data import DATA
import csv, json, sys, time, urllib.request
SP = DATA
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if not os.path.exists(SP + "sp500.csv"):    # today's S&P 500 list, with the date each joined
    urllib.request.urlretrieve("https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv",
                               SP + "sp500.csv")
syms = [r["Symbol"].replace(".", "-") for r in csv.DictReader(open(SP + "/sp500.csv"))]
allow = json.load(open(ROOT + "/trader/config.json"))["allowed_tickers"]
syms = list(dict.fromkeys(syms + allow + ["SPY", "RSP", "QQQ"]))
p1, p2 = 1496275200, int(time.time())          # 2017-06-01
out, errs = {}, {}
for i, s in enumerate(syms):
    url = "https://query1.finance.yahoo.com/v8/finance/chart/%s?period1=%d&period2=%d&interval=1d&events=div,split" % (s, p1, p2)
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; day-trader-skill/2.0)"})
            r = json.loads(urllib.request.urlopen(req, timeout=30).read())["chart"]["result"][0]
            q = r["indicators"]["quote"][0]; adj = r["indicators"]["adjclose"][0]["adjclose"]
            rows = []
            for t, o, h, l, c, a, v in zip(r["timestamp"], q["open"], q["high"], q["low"], q["close"], adj, q["volume"]):
                if None in (o, h, l, c, a) or c <= 0: continue
                f = a / c
                rows.append([t, o * f, h * f, l * f, a, v or 0])
            out[s] = rows; errs.pop(s, None); break
        except Exception as e:
            errs[s] = str(e)[:80]; time.sleep(3 + 6 * attempt)
    time.sleep(0.4)
    if i % 100 == 99: print(i + 1, "done", flush=True)
json.dump(out, open(SP + "/prices.json", "w"))
print("prices:", len(out), "symbols; errors:", errs, flush=True)
