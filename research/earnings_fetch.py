"""Nasdaq's earnings calendar for every weekday 2019-01-01 to 2026-11-06 (eps, forecast, % surprise). Read-only.
4 requests at a time; resumable: days already in earnings.json are skipped."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _data import DATA
import concurrent.futures as cf, datetime, json, os, sys, threading, time, urllib.request
SP = DATA
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
path = SP + "/earnings.json"
out = json.load(open(path)) if os.path.exists(path) else {}
days, d = [], datetime.date(2019, 1, 1)
while d <= datetime.date(2026, 11, 6):
    if d.weekday() < 5 and d.isoformat() not in out: days.append(d.isoformat())
    d += datetime.timedelta(days=1)
lock, errs = threading.Lock(), []

def get(day):
    url = "https://api.nasdaq.com/api/calendar/earnings?date=%s" % day
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json", "Accept-Language": "en-US"})
            rows = json.loads(urllib.request.urlopen(req, timeout=60).read())["data"]["rows"] or []
            return day, [{k: r.get(k) for k in ("symbol", "eps", "epsForecast", "surprise", "time")} for r in rows]
        except Exception as e:
            last = str(e)[:60]; time.sleep(5 + 10 * attempt)
    return day, ("ERR", last)

done = 0
with cf.ThreadPoolExecutor(4) as ex:
    for day, rows in ex.map(get, days):
        with lock:
            if isinstance(rows, tuple): errs.append((day, rows[1]))
            else: out[day] = rows
            done += 1
            if done % 100 == 0:
                json.dump(out, open(path, "w")); print(day, done, "of", len(days), flush=True)
json.dump(out, open(path, "w"))
print("earnings days:", len(out), "errors:", len(errs), errs[:5], flush=True)
