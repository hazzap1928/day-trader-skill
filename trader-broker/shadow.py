#!/usr/bin/env python3
"""Three review-only records on the holdings pages: Kronos's opinion of each drift and news buy, the "stop
shadow" of each holding its stop sold, and the sell-rule replay of each holding. SHADOW ONLY: nothing here
places, cancels or changes an order, blocks a buy, or changes a stop, a sell date or a status. It only adds three fields to pages in
trader/state/holdings.json (and a line to holdings-log.jsonl).

  page["kronos"]       Kronos's forecast for the share over its planned holding, from the daily bars BEFORE
                       the buy day (what it would have said that morning; no hindsight):
                       {"score", "path_returns", "paths", "pred_len", "as_of", "model", "seconds", "at"}.
                       score = the median forecast change from the last close before the buy to the close on
                       the sell date. The same forecast research/kronos_backtest.py tested.
                       After 3 failed tries: {"error", "tries"} and no more tries.
  page["stop_shadow"]  for a drift or news holding its stop sold: what it would have been worth on its planned
                       sell date had the stop not fired: {"stop_exit_price", "stop_exit_date",
                       "planned_sell_date", "status": "waiting" | "done" | "no_price", and when done
                       "price_on_sell_date", "diff_per_share", "diff_pct", "diff_gbp"}.
                       diff = the sell-date close minus the stop's sale price (positive: the stop cost money;
                       negative: it saved money). diff_gbp is for the shares sold, at the day's exchange rate,
                       before fees (the same fee is paid either way, on a slightly different amount).

  page["replay"]       the Time Machine for holds (trader-timemachine/holds.py): the holding replayed 1,000 times on
                       its own past daily ups and downs (trend taken out; bars BEFORE the buy day), from its buy price to its sell
                       date: how often the stop is hit first, and the typical result after fees with the stop as
                       set ("fixed"), a stop raised as the price rises ("rising") and no stop ("none"). Once a page;
                       after 3 failed tries: {"error", "tries"}.

The autopilot starts `shadow.py run` once a day after the daily review, as a separate program, so a slow
Kronos run never holds up the autopilot. Kronos needs trader-kronos/.venv; without it the opinion is skipped.
Prices from Yahoo (unofficial) daily bars. Demo. Education and tooling, not financial advice.

    python3 trader-broker/shadow.py run          # fill in what is missing (what the autopilot runs)
    python3 trader-broker/shadow.py show         # the two records, as text
    python3 trader-broker/shadow.py time SYMBOL  # one timed Kronos opinion as of today, nothing written
"""

import argparse
import datetime
import importlib.util
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
KRONOS_DIR = os.path.join(ROOT, "trader-kronos")
KRONOS_PY = os.path.join(KRONOS_DIR, ".venv", "bin", "python")
KRONOS_SCRIPT = os.path.join(KRONOS_DIR, "kronos_daily.py")

HOLD = ("drift", "news")
BUDGET_S = 600            # one run of Kronos at most this long; what does not fit waits for the next day
MAX_TRIES = 3
MAX_HORIZON = 60          # trading days forecast at most
BARS_BACK_DAYS = 420      # calendar days of daily bars fetched: over 256 trading days

holdings = drift = alerts = None        # the tests set theirs
_MODS = {}


def _load(folder, name):
    key = (folder, name)
    if key not in _MODS:
        spec = importlib.util.spec_from_file_location("shadow_%s_%s" % (folder.replace("-", "_"), name),
                                                      os.path.join(ROOT, folder, name + ".py"))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        _MODS[key] = m
    return _MODS[key]


def _hold():
    global holdings
    if holdings is None:
        holdings = _load("trader-broker", "holdings")
    return holdings


def _alerts():
    global alerts
    if alerts is None:
        alerts = _load("trader-alerts", "alerts")
    return alerts


def _bars_fn():
    global drift
    if drift is None:
        drift = _load("trader-broker", "drift")
    return drift.daily_bars


def _date(x):
    try:
        return datetime.date.fromisoformat(str(x)[:10])
    except ValueError:
        return None


def held_days(buy, sell):
    """The New York trading days from the buy day to the sell day, both included (at most MAX_HORIZON)."""
    al, out, d = _alerts(), [], buy
    while d <= sell and len(out) < MAX_HORIZON:
        if al.trading_day(d)[0]:
            out.append(d.isoformat())
        d += datetime.timedelta(days=1)
    return out


# ---------------------------------------------------------------- Kronos's opinion

def wants_kronos(page):
    if page.get("strategy") not in HOLD or page.get("status") == "waiting":
        return False
    k = page.get("kronos") or {}
    return k.get("score") is None and int(k.get("tries") or 0) < MAX_TRIES


def kronos_item(page, bars_fn):
    """The request for one page: daily bars strictly before the buy day, and the days it is held."""
    buy, sell = _date(page.get("buy_date")), _date((page.get("sell_rule") or {}).get("date"))
    if not buy or not sell or sell < buy:
        raise ValueError("no buy date or sell date to forecast between")
    bars = bars_fn(page["symbol"], buy - datetime.timedelta(days=BARS_BACK_DAYS), buy - datetime.timedelta(days=1))
    rows = [[d.isoformat()] + list(v) for d, v in sorted(bars.items()) if d < buy]
    return {"id": page["id"], "bars": rows, "future": held_days(buy, sell)}


def kronos_available():
    return os.path.exists(KRONOS_PY) and os.path.exists(KRONOS_SCRIPT)


def run_kronos(items, budget_s=BUDGET_S, run=None):
    """Kronos on the items, the model loaded once. Returns its answer {"model", "load_seconds", "results"}."""
    payload = json.dumps({"budget_s": budget_s, "items": items})
    if run is not None:
        out = run(payload)
    else:
        p = subprocess.run([KRONOS_PY, KRONOS_SCRIPT], input=payload, capture_output=True, text=True,
                           timeout=budget_s + 180, cwd=KRONOS_DIR)
        if p.returncode != 0:
            raise RuntimeError((p.stderr or p.stdout).strip()[-200:])
        out = p.stdout
    for line in reversed((out or "").strip().splitlines()):
        if line.strip().startswith("{"):
            return json.loads(line)
    raise RuntimeError("Kronos gave no answer")


def _failed(page, why):
    k = page.get("kronos") or {}
    return {"error": str(why)[:200], "tries": int(k.get("tries") or 0) + 1, "at": int(time.time())}


def kronos_pass(sd, bars_fn=None, run=None, budget_s=BUDGET_S, say=print):
    """Kronos's opinion on every drift and news page that has none yet. Returns the number written."""
    H = _hold()
    if run is None and not kronos_available():
        say("shadow: Kronos is not set up (trader-kronos/.venv), so no opinions today")
        return 0
    pages = [p for p in H.load(sd)["pages"] if wants_kronos(p)]
    if not pages:
        return 0
    items, failed = [], {}
    for p in pages:
        try:
            items.append(kronos_item(p, bars_fn or _bars_fn()))
        except Exception as e:
            failed[p["id"]] = "no daily bars: %s" % e
    got, model, t0 = {}, None, time.time()
    if items:
        try:
            ans = run_kronos(items, budget_s, run)
            model = ans.get("model")
            got = {r.get("id"): r for r in ans.get("results") or []}
            say("shadow: Kronos loaded in %ss; %d of %d opinions in %.0fs" % (
                ans.get("load_seconds"), len([r for r in got.values() if r.get("score") is not None]),
                len(items), time.time() - t0))
        except Exception as e:
            for it in items:
                failed[it["id"]] = "Kronos failed: %s" % e
    n = 0
    with H.locked(sd):
        doc = H.load(sd)
        for page in doc["pages"]:
            pid = page.get("id")
            r = got.get(pid)
            if r is not None and r.get("score") is not None:
                page["kronos"] = dict({k: r[k] for k in ("score", "path_returns", "paths", "pred_len", "as_of",
                                                         "seconds") if k in r}, model=model, at=int(time.time()))
            elif r is not None or pid in failed:
                page["kronos"] = _failed(page, (r or {}).get("error") or failed[pid])
            else:
                continue          # not reached inside the time budget: tried again next time
            n += 1
            H.log(sd, "kronos", id=pid, symbol=page.get("symbol"), strategy=page.get("strategy"), kronos=page["kronos"])
        if n:
            H.save(sd, doc)
    return n


# ---------------------------------------------------------------- the sell-rule replay (Time Machine for holds)

def _holds():
    return _load("trader-timemachine", "holds")


def wants_replay(page):
    if page.get("strategy") not in HOLD or page.get("status") == "waiting":
        return False
    r = page.get("replay") or {}
    return r.get("rules") is None and int(r.get("tries") or 0) < MAX_TRIES


def replay_one(page, bars_fn):
    """The replay for one page, from the daily bars strictly before its buy day. Raises when it cannot be done."""
    buy, sell = _date(page.get("buy_date")), _date((page.get("sell_rule") or {}).get("date"))
    entry, stop = page.get("buy_price"), page.get("stop")
    if not buy or not sell or sell < buy:
        raise ValueError("no buy date or sell date")
    if not entry or not stop:
        raise ValueError("no buy price or stop on the page")
    bars = bars_fn(page["symbol"], buy - datetime.timedelta(days=BARS_BACK_DAYS), buy - datetime.timedelta(days=1))
    rows = [tuple(v[:4]) for d, v in sorted(bars.items()) if d < buy]
    out = _holds().noise_test(rows, float(entry), len(held_days(buy, sell)), stop=float(stop))
    out["as_of"] = max(d for d in bars if d < buy).isoformat()
    return out


def replay_pass(sd, bars_fn=None, say=print):
    """The sell-rule replay on every drift and news page that has none yet. Returns the number written."""
    H = _hold()
    pages = [p for p in H.load(sd)["pages"] if wants_replay(p)]
    if not pages:
        return 0
    got = {}
    for p in pages:
        try:
            got[p["id"]] = dict(replay_one(p, bars_fn or _bars_fn()), at=int(time.time()))
        except Exception as e:
            r = p.get("replay") or {}
            got[p["id"]] = {"error": str(e)[:200], "tries": int(r.get("tries") or 0) + 1, "at": int(time.time())}
    n = 0
    with H.locked(sd):
        doc = H.load(sd)
        for page in doc["pages"]:
            if page.get("id") in got and wants_replay(page):
                page["replay"] = got[page["id"]]
                H.log(sd, "replay", id=page["id"], symbol=page.get("symbol"), strategy=page.get("strategy"),
                      replay=page["replay"])
                n += 1
        if n:
            H.save(sd, doc)
    return n


# ---------------------------------------------------------------- the stop shadow

def stop_sold(page, guard_state=None):
    """True when the page was closed by its stop: a stop order filled at Trading 212, or the guard's market
    sale at a software stop (t212-guard.json "sold", why "stop", on the same day)."""
    res = page.get("result") or {}
    if page.get("status") != "closed":
        return False
    if res.get("why") == "stop":
        return True
    ref = (page.get("broker_ref") or {}).get("ref")
    for s in (guard_state or {}).get("sold") or []:
        if s.get("why") == "stop" and s.get("ticker") == ref and _sold_day(s.get("at")) == res.get("date"):
            return True
    return False


def _sold_day(at):
    """The guard writes "at" in New York time (ticket.ny_iso), so its first 10 characters are the NY date."""
    return str(at or "")[:10]


def shadow_pass(sd, today, bars_fn=None, usd_per_gbp=None, guard_state=None, say=print):
    """Start and finish the stop shadow of each drift and news holding its stop sold. Returns the number of
    pages changed. The sell-date price is used only once it is a final close (the sell date before today)."""
    H = _hold()
    bars_fn = bars_fn or _bars_fn()
    n = 0
    with H.locked(sd):
        doc = H.load(sd)
        for page in doc["pages"]:
            if page.get("strategy") not in HOLD or not stop_sold(page, guard_state):
                continue
            sh = page.get("stop_shadow")
            if sh and sh.get("status") != "waiting":
                continue
            res = page.get("result") or {}
            if not sh:
                sh = page["stop_shadow"] = {"stop_exit_price": res.get("price"), "stop_exit_date": res.get("date"),
                                            "planned_sell_date": (page.get("sell_rule") or {}).get("date"),
                                            "qty": res.get("qty") or page.get("qty"), "status": "waiting"}
                n += 1
                H.log(sd, "stop_shadow", id=page.get("id"), symbol=page.get("symbol"), stop_shadow=sh)
            sell = _date(sh.get("planned_sell_date"))
            if not sell or sell >= today:
                continue
            try:
                bars = bars_fn(page["symbol"], sell - datetime.timedelta(days=10), sell)
            except Exception as e:
                say("shadow: no price for %s on %s yet (%s)" % (page.get("symbol"), sell, e))
                continue
            closes = [(d, v[3]) for d, v in sorted(bars.items()) if d <= sell]
            exit_ = sh.get("stop_exit_price")
            if not closes or not exit_:
                sh["status"] = "no_price"
            else:
                day, close = closes[-1]
                qty = float(sh.get("qty") or 0)
                diff = close - float(exit_)
                sh.update(status="done", price_on_sell_date=round(close, 4), price_date=day.isoformat(),
                          diff_per_share=round(diff, 4), diff_pct=round(close / float(exit_) - 1, 6),
                          diff_gbp=round(qty * diff / usd_per_gbp, 2) if usd_per_gbp and qty else None)
            n += 1
            H.log(sd, "stop_shadow", id=page.get("id"), symbol=page.get("symbol"), stop_shadow=sh)
        if n:
            H.save(sd, doc)
    return n


# ---------------------------------------------------------------- by hand

def _read(p):
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _today_ny():
    tk = _load("trader-broker", "ticket")
    return tk.to_eastern(datetime.datetime.utcnow()).date()


def run(sd, say=print):
    today = _today_ny()
    rate = _read(os.path.join(sd, "fx.json")).get("usd_per_gbp")
    guard = _read(os.path.join(sd, "state", "t212-guard.json"))
    try:
        say("shadow: %d stop shadow change(s)" % shadow_pass(sd, today, usd_per_gbp=rate, guard_state=guard, say=say))
    except Exception as e:
        say("shadow: stop shadow failed (%s)" % str(e)[:200])
    try:
        say("shadow: %d sell-rule replay(s) written" % replay_pass(sd, say=say))
    except Exception as e:
        say("shadow: sell-rule replays failed (%s)" % str(e)[:200])
    try:
        say("shadow: %d Kronos opinion(s) written" % kronos_pass(sd, say=say))
    except Exception as e:
        say("shadow: Kronos opinions failed (%s)" % str(e)[:200])


def show(sd):
    for p in _hold().load(sd)["pages"]:
        if p.get("strategy") not in HOLD:
            continue
        k, s = p.get("kronos") or {}, p.get("stop_shadow")
        kt = ("forecast %+.1f%% over %s days" % (100 * k["score"], k.get("pred_len")) if k.get("score") is not None
              else "error (%s), tries %s" % (k.get("error"), k.get("tries")) if k else "none yet")
        print("%-6s %-6s %-7s bought %s  Kronos: %s" % (p.get("symbol"), p.get("strategy"), p.get("status"),
                                                       p.get("buy_date"), kt))
        if s:
            print("       stop shadow: %s" % json.dumps(s, sort_keys=True))
        print("       replay: %s" % _holds().words(p.get("replay")))


def time_one(symbol):
    """One Kronos opinion as of today, 20 trading days ahead, timed. Writes nothing."""
    today = _today_ny()
    t0 = time.time()
    bars = _bars_fn()(symbol, today - datetime.timedelta(days=BARS_BACK_DAYS), today - datetime.timedelta(days=1))
    fetched = time.time() - t0
    al, fut, d = _alerts(), [], today
    while len(fut) < 20:
        if al.trading_day(d)[0]:
            fut.append(d.isoformat())
        d += datetime.timedelta(days=1)
    item = {"id": symbol, "bars": [[x.isoformat()] + list(v) for x, v in sorted(bars.items()) if x < today], "future": fut}
    t1 = time.time()
    ans = run_kronos([item], BUDGET_S)
    print(json.dumps({"bars_seconds": round(fetched, 1), "kronos_seconds_total": round(time.time() - t1, 1),
                      "answer": ans}, indent=1))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Kronos opinions and stop shadows on the holdings pages (review only).")
    ap.add_argument("cmd", choices=["run", "show", "time"])
    ap.add_argument("symbol", nargs="?")
    ap.add_argument("--state-dir", default=os.path.join(ROOT, "trader"))
    a = ap.parse_args(argv)
    if a.cmd == "run":
        run(a.state_dir, say=lambda *x: print(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), *x, flush=True))
    elif a.cmd == "show":
        show(a.state_dir)
    else:
        time_one(a.symbol or "SPY")
    return 0


if __name__ == "__main__":
    sys.exit(main())
