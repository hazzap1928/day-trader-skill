"""Daily bars for companies that were in the S&P 500 at some point since June 2018 but are not in
prices.json (today's list), for the survivorship check in momentum_12_1.py. Yahoo, read-only.

Membership history: github.com/fja05680/sp500 (public, point in time), saved under
research-data/sp500-history/. Yahoo has dropped many delisted tickers, so only some come back;
the script says how many. A series that matches a ticker we already have (a renaming, e.g. FB and META)
is dropped so nothing counts twice.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _data import DATA
import csv, json, time, urllib.request

H = DATA + "sp500-history/"
os.makedirs(H, exist_ok=True)
BASE = "https://raw.githubusercontent.com/fja05680/sp500/master/"
if not os.path.exists(H + "components.csv"):
    urllib.request.urlretrieve(BASE + "S%26P%20500%20Historical%20Components%20%26%20Changes%20(Updated).csv",
                               H + "components.csv")

P = json.load(open(DATA + "prices.json"))
ever = set()
for r in csv.DictReader(open(H + "components.csv")):
    if r["date"] >= "2018-06-01":
        ever |= {t.replace(".", "-") for t in r["tickers"].split(",")}
missing = sorted(ever - set(P))

# closes by date for the duplicate check
known = {s: {r[0] // 86400: r[4] for r in rows[-2500:]} for s, rows in P.items()}


def duplicate_of(rows):
    """The existing ticker this series copies (same adjusted close on 20 shared days), else None."""
    sample = {r[0] // 86400: r[4] for r in rows[::max(1, len(rows) // 20)]}
    for s, k in known.items():
        hits = [abs(k[d] / c - 1) < 0.002 for d, c in sample.items() if d in k]
        if len(hits) >= 10 and all(hits):
            return s
    return None


p1, p2 = 1496275200, int(time.time())          # 2017-06-01
out, failed, dups = {}, {}, {}
for i, s in enumerate(missing):
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/%s?period1=%d&period2=%d&interval=1d&events=div,split"
           % (s, p1, p2))
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; research/1.0)"})
            r = json.loads(urllib.request.urlopen(req, timeout=30).read())["chart"]["result"][0]
            q = r["indicators"]["quote"][0]; adj = r["indicators"]["adjclose"][0]["adjclose"]
            rows = []
            for t, o, h, l, c, a, v in zip(r["timestamp"], q["open"], q["high"], q["low"], q["close"], adj, q["volume"]):
                if None in (o, h, l, c, a) or c <= 0: continue
                f = a / c
                rows.append([t, o * f, h * f, l * f, a, v or 0])
            if len(rows) < 20:
                failed[s] = "too few bars"; break
            d = duplicate_of(rows)
            if d:
                dups[s] = d
            else:
                out[s] = rows
            failed.pop(s, None); break
        except Exception as e:
            failed[s] = str(e)[:60]
            if "404" in str(e): break
            time.sleep(2 + 4 * attempt)
    time.sleep(0.4)
json.dump(out, open(DATA + "prices_removed.json", "w"))
json.dump({"missing": missing, "got": sorted(out), "duplicates": dups, "failed": failed},
          open(H + "fetch_report.json", "w"), indent=1)
print("missing %d; fetched %d; renamings dropped %d; not on Yahoo %d" % (len(missing), len(out), len(dups), len(failed)))
