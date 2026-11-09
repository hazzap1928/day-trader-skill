"""Replay the ORB rule (or_mid stop, 1.5R target, entries by 15:30, out at 15:50) on every
allowed ticker's last 60 days of 5-minute bars, and save every trade."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _data import DATA
import importlib.util, json, sys, time, datetime
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("tm", ROOT + "/trader-timemachine/timemachine.py")
tm = importlib.util.module_from_spec(spec); spec.loader.exec_module(tm)
cfg = json.load(open(ROOT + "/trader/config.json"))
syms = cfg["allowed_tickers"]
VARIANT = "variant" in sys.argv[1:]      # the variant: stop at the opening-range low, 2R target
rule = ({"setup": "orb", "or_minutes": 15, "stop_rule": "or_low", "target_r": 2.0} if VARIANT else
        {"setup": "orb", "or_minutes": 15, "stop_rule": "or_mid", "target_r": 1.5})
OUT = "orb_variant_trades.json" if VARIANT else "orb_trades.json"
last = (15 * 60 + 50 - tm.OPEN_MIN) // 5 - 1          # the 15:45-15:50 bar: out at the 15:50 close
latest_entry = (15 * 60 + 30 - tm.OPEN_MIN) // 5 - 1   # no entries after 15:30
now = datetime.datetime.utcnow()
today = tm.to_eastern(now).date().isoformat()
out, errors = [], {}
for i, s in enumerate(syms):
    try:
        text = tm.fetch_chart(s)
        meta, bars, _ = tm.parse_bars(tm.chart_result(text, s), now)
        days = tm.split_sessions(bars)
        sessions, _ = tm.full_sessions(days, today)
        h = tm.history_replay(sessions, rule, last)
        for t in h["trades"]:
            hh, mm = t["time"].split()[0].split(":")
            mins = (int(hh) % 12 + (12 if t["time"].endswith("PM") and int(hh) != 12 else 0)) * 60 + int(mm)
            if mins > 15 * 60 + 30:
                continue
            out.append(dict(t, symbol=s))
        errors.pop(s, None)
    except Exception as e:
        errors[s] = str(e)[:120]
    time.sleep(tm.PAUSE)
    if i % 20 == 19:
        print("%d/%d done, %d trades" % (i + 1, len(syms), len(out)), flush=True)
json.dump({"rule": rule, "trades": out, "errors": errors, "fetched": now.isoformat()},
          open(DATA + OUT, "w"))
print("trades", len(out), "errors", len(errors), errors)
