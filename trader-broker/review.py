#!/usr/bin/env python3
"""The daily review of what is held (docs/hold-agent-spec.md, section 4). REPORT ONLY.

Once a trading day, after the close and the holdings match, in this order: safety, then money, then
judgement.

  1. Can the account be read? If not: "could not check today", and stop.
  2. Does the record match Trading 212? (the daily match, trader-broker/holdings_t212.py)
  3. Does every holding have a stop? A missing one is the first line.
  4. Are the money limits met? Cash at or over the buffer, drift at or under its share of the investable
     cash, risk at the stops at or under each strategy's cap.
  5. Is a rule sale due? A holding at its sell date (the guard's 15:50 close sells it: an existing rule).
  6. Does a reason look broken? The stock left the universe or went on the blocked list, or the news run
     asked about it. The review only SAYS it would ask. New SEC filings and headlines on each holding since
     the last review (trader-broker/hold_watch.py, when "hold_watch" is on) are shown beside it, and its next
     results date when it falls before the sell date (trader-broker/earnings_watch.py, when "earnings_watch" is on).
  7. How is each holding doing? Its gain in pounds after fees, the S&P 500 beside it for information.
  8. One message.

It never places, cancels or changes an order, never sends Approve/Reject buttons and never edits a page:
it says what it would sell or ask about, and shows each holding's verdict from the day's news runs (the news
runs, not this review, send the "sell it?" question for a news holding). Read-only at Trading 212. Demo. Education and tooling, not
financial advice.

    python3 trader-broker/review.py            # today's review as text (demo, read-only, nothing sent)
"""

import argparse
import datetime
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

t212 = drift = holdings = holdings_t212 = None        # the autopilot and the tests set theirs
_MODS = {}

LABELS = {"intraday": "day trade", "drift": "earnings trade", "news": "news idea"}


def _load(folder, name):
    key = (folder, name)
    if key not in _MODS:
        spec = importlib.util.spec_from_file_location("review_%s_%s" % (folder.replace("-", "_"), name),
                                                      os.path.join(ROOT, folder, name + ".py"))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        _MODS[key] = m
    return _MODS[key]


def _t212():
    global t212
    if t212 is None:
        t212 = _load("trader-broker", "t212")
    return t212


def _drift():
    global drift
    if drift is None:
        drift = _load("trader-broker", "drift")
    return drift


def _hold():
    global holdings
    if holdings is None:
        holdings = _load("trader-broker", "holdings")
    return holdings


def _ht():
    global holdings_t212
    if holdings_t212 is None:
        holdings_t212 = _load("trader-broker", "holdings_t212")
        holdings_t212.t212, holdings_t212.holdings = _t212(), _hold()
    return holdings_t212


def _num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v else None


def _date(v):
    try:
        return datetime.date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def path(sd, day):
    d = os.path.join(sd, "state", "reviews")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "%s.json" % day)


def could_not(today, why):
    return {"day": str(today), "ok": False, "error": str(why)[:200], "rows": [], "problems": []}


def limits(cfg, view, risk, cost):
    """The money limits, each {"name", "ok", "now_gbp", "limit_gbp", "words"}. Pure."""
    dr = _load("trader-broker", "day_risk")
    out = []
    account = _num(view.get("account_gbp"))
    if not account:
        return [{"name": "account", "ok": False, "words": "Trading 212 gave no account value, so the limits could not be checked"}]
    buf_pct = _num((cfg or {}).get("cash_buffer_pct")) or 0.0
    cash = _num(view.get("cash_gbp"))
    if buf_pct and cash is not None:
        need = round(account * buf_pct / 100.0, 2)
        out.append({"name": "cash", "ok": cash + 0.005 >= need, "now_gbp": round(cash, 2), "limit_gbp": need,
                    "words": "cash GBP %.2f against the GBP %.2f (%g%%) always kept" % (cash, need, buf_pct)})
    dcfg = (cfg or {}).get("drift") if isinstance((cfg or {}).get("drift"), dict) else {}
    investable = account * (1 - buf_pct / 100.0)
    share = _num(dcfg.get("max_share_pct")) or 50.0
    used = (cost or {}).get("drift", 0.0)
    if used or dcfg.get("buy_enabled") is True:
        cap = round(investable * share / 100.0, 2)
        out.append({"name": "drift_share", "ok": used <= cap + 0.005, "now_gbp": round(used, 2), "limit_gbp": cap,
                    "words": "drift holds GBP %.2f against its GBP %.2f (%g%% of the investable cash)" % (used, cap, share)})
    try:
        caps, total = dr.caps_pct(cfg), dr.total_cap_pct(cfg)
    except ValueError as e:
        return out + [{"name": "risk", "ok": False, "words": str(e)}]
    by = (risk or {}).get("by_strategy") or {}
    if caps is not None:
        for name in sorted(set(caps) | set(by)):
            now = by.get(name, 0.0)
            if name not in caps:
                if now > 0.005:
                    out.append({"name": "risk_" + name, "ok": False, "now_gbp": now, "limit_gbp": None,
                                "words": "GBP %.2f at risk under the label %r, which has no cap in risk_caps_pct" % (now, name)})
                continue
            cap = round(account * caps[name] / 100.0, 2)
            out.append({"name": "risk_" + name, "ok": now <= cap + 0.005, "now_gbp": now, "limit_gbp": cap,
                        "words": "%s: GBP %.2f at risk at the stops against its GBP %.2f cap (%g%%)" % (name, now, cap, caps[name])})
    elif total:
        now, cap = (risk or {}).get("open_risk_gbp", 0.0), round(account * total / 100.0, 2)
        out.append({"name": "risk_total", "ok": now <= cap + 0.005, "now_gbp": now, "limit_gbp": cap,
                    "words": "GBP %.2f at risk at the stops across everything against the GBP %.2f cap (%g%%)" % (now, cap, total)})
    return out


def build(sd, cfg, res, today, bars_fn=None, asks=None, verdicts=None, watch=None, earnings=None):
    """The review from a daily-match result (holdings_t212.daily_match). Pure but for the SPY bars
    (`bars_fn`, information only; a failure just leaves the market figure out). Changes nothing."""
    H, D = _hold(), _drift()
    today = _date(today)
    if not res:
        return could_not(today, "the account or the holdings record could not be read")
    v = res["view"]
    rate = _num(v.get("usd_per_gbp"))
    if not rate:
        return could_not(today, "no GBP/USD rate")
    doc = H.load(sd)
    pages = {}
    for p in H.live(doc, v.get("broker"), v.get("account")):
        pages.setdefault((p.get("broker_ref") or {}).get("ref"), []).append(p)
    risk = res.get("risk") or H.open_risk(v)
    stops = {r["ref"]: r for r in risk["rows"]}
    cost = H.cost_by_strategy(v)
    try:
        allowed, blocked, _src = D.universe(cfg, sd)
    except Exception:
        allowed, blocked = None, set()
    try:
        nxt = D.add_trading_days(today, 1)
    except Exception:
        nxt = None
    pending_asks = {a.get("page"): a for a in (asks or []) if a.get("status") == "pending"}
    spy = None
    firsts = [_date(p.get("buy_date")) for ps in pages.values() for p in ps if _date(p.get("buy_date"))]
    if firsts and bars_fn is not None:
        try:
            tracker = ((cfg or {}).get("news") or {}).get("tracker") or "SPY"
            spy = bars_fn(tracker, min(firsts) - datetime.timedelta(days=4), today + datetime.timedelta(days=1))
        except Exception:
            spy = None
    rows, due, overdue, would_ask, approved = [], [], [], [], []
    for pos in v.get("positions") or []:
        ref, qty = pos["ref"], _num(pos.get("qty")) or 0.0
        if qty <= 0:
            continue
        page = (pages.get(ref) or [None])[-1]
        strategy = H.strategy_of(page) if page else "no record"
        avg, price = _num(pos.get("avg")), _num(pos.get("price"))
        gain = None
        if avg and price:
            gain = round(qty * (price * (1 - H.FEE) - avg * (1 + H.FEE)) / rate, 2)
        st = stops.get(ref) or {}
        row = {"symbol": pos.get("symbol"), "ref": ref, "strategy": strategy, "qty": qty, "avg": avg, "price": price,
               "gain_gbp": gain, "stop": st.get("stop"), "stop_how": st.get("how"), "risk_gbp": st.get("risk_gbp"),
               "buy_date": (page or {}).get("buy_date"), "reason": (page or {}).get("reason"),
               "sell_date": None, "status": (page or {}).get("status"), "market_pct": None,
               "verdict": (verdicts or {}).get(pos.get("symbol")) if page is not None and H.is_hold(page) else None,
               "watch": _watch(watch, page), "replay": _replay(page),
               "earnings": (earnings or {}).get((page or {}).get("id")), "chart": _chart(page)}
        sell = H.sell_date(page) if page else None
        if sell:
            row["sell_date"] = sell.isoformat()
        buy = _date(row["buy_date"])
        if spy and buy:
            try:
                days = [d for d in sorted(spy) if buy <= d <= today]
                if days:
                    row["market_pct"] = round(100 * (spy[days[-1]][3] / spy[days[0]][0] - 1), 2)
            except Exception:
                row["market_pct"] = None
        rows.append(row)
        if page is None or not H.is_hold(page):
            continue
        if page.get("status") == "selling":
            approved.append(row["symbol"])
        elif sell and sell <= today:
            overdue.append(row["symbol"])              # still held after the close on or past its sell date
        elif sell and nxt and sell == nxt:
            due.append(row["symbol"])
        why = None
        if D.key(row["symbol"]) in blocked:
            why = "it is now on your blocked list"
        elif allowed is not None and D.key(row["symbol"]) not in allowed:
            why = "it is no longer in the list of companies the agent picks from"
        asked = pending_asks.get(page.get("id"))
        if asked:
            would_ask.append({"symbol": row["symbol"], "why": asked.get("why"), "asked": True})
        elif why:
            would_ask.append({"symbol": row["symbol"], "why": why, "asked": False})
    kinds = {}
    for m in res.get("mismatches") or []:
        kinds.setdefault(m["kind"], []).append(m.get("symbol"))
    no_stop = sorted(set((kinds.get("no_stop") or []) + (kinds.get("part_stop") or [])
                         + [r["symbol"] for r in rows if str(r.get("stop_how") or "").startswith("no stop")]))
    lim = limits(cfg, v, risk, cost)
    problems = []
    if no_stop:
        problems.append("no_stop")
    if res.get("pausing"):
        problems.append("mismatch")
    if any(not x["ok"] for x in lim):
        problems.append("limits")
    if overdue:
        problems.append("overdue")
    return {"day": today.isoformat(), "ok": True, "rows": rows, "no_stop": no_stop,
            "soft_stop": sorted(set(kinds.get("soft_stop") or [])),
            "mismatches": [{"kind": m["kind"], "symbol": m.get("symbol"), "what": m.get("what")} for m in res.get("mismatches") or []],
            "paused": bool(res.get("pausing")), "limits": lim, "due": due, "overdue": overdue, "approved": approved,
            "would_ask": would_ask, "risk_gbp": risk.get("open_risk_gbp"), "risk_by_strategy": risk.get("by_strategy") or {},
            "account_gbp": _num(v.get("account_gbp")), "problems": problems, "report_only": True}


def _replay(page):
    """The holding's sell-rule replay (shadow.py, page["replay"]): the figures, and whether it is new (worked out
    in the last 36 hours, so the phone tells it once). None when there is none."""
    r = (page or {}).get("replay") or {}
    if not r.get("rules"):
        return None
    try:
        new = (datetime.datetime.now().timestamp() - float(r.get("at") or 0)) < 36 * 3600
    except (TypeError, ValueError):
        new = False
    return {"stop_first_pct": r.get("stop_first_pct"), "runs": r.get("runs"), "new": new,
            "median_pct": {k: (r["rules"].get(k) or {}).get("median_pct") for k in ("fixed", "rising", "none")}}


def _chart(page):
    """The chart labels at the buy (shadow.py, page["chart"]) and whether they are new (last 36 hours). Or None."""
    c = (page or {}).get("chart") or {}
    if c.get("tags") is None:
        return None
    try:
        new = (datetime.datetime.now().timestamp() - float(c.get("at") or 0)) < 36 * 3600
    except (TypeError, ValueError):
        new = False
    return {"tags": c["tags"], "falling": bool(c.get("falling")), "new": new}


def _watch(watch, page):
    """A holding's new filings and headlines (trader-broker/hold_watch.py), or None. Shown only."""
    items = (watch or {}).get((page or {}).get("id"))
    if not items:
        return None
    return _load("trader-broker", "hold_watch").summary(items)


def worth_sending(r, sd=None):
    """A message only when something is held or something is wrong. A failed check is told only when the
    record says something should be held."""
    if not r.get("ok"):
        return bool(sd and _hold().live(_hold().load(sd)))
    return bool(r.get("rows") or r.get("problems") or r.get("mismatches"))


def text(r):
    """The review for the log and the command line (full wording)."""
    if not r.get("ok"):
        return "DAILY REVIEW (report only) %s: could not check today (%s)" % (r.get("day"), r.get("error"))
    L = ["DAILY REVIEW (report only: nothing is sold, asked or changed) %s" % r["day"]]
    if r["no_stop"]:
        L.append("  !!! NO STOP (or a stop for only part): %s" % ", ".join(r["no_stop"]))
    L.append("  record against Trading 212: %s" % (
        "matches" if not r["mismatches"] else "; ".join("%s: %s" % (m["symbol"], m["what"]) for m in r["mismatches"])))
    if r["paused"]:
        L.append("  new buys are PAUSED until they match")
    for x in r["limits"]:
        L.append("  %s %s" % ("ok  " if x["ok"] else "OVER", x["words"]))
    for row in r["rows"]:
        L.append("  %-6s %-10s %g shares, %s after fees%s, stop %s (%s), sell date %s" % (
            row["symbol"], row["strategy"], row["qty"],
            "no price" if row["gain_gbp"] is None else "GBP %+.2f" % row["gain_gbp"],
            "" if row["market_pct"] is None else " (S&P 500 %+.2f%% over the same days, for information)" % row["market_pct"],
            "none" if not row["stop"] else "$%.2f" % row["stop"], row.get("stop_how") or "?", row["sell_date"] or "none"))
        if row.get("verdict"):
            L.append("         today's verdict from the news runs: %s (confidence %s): %s%s" % (
                row["verdict"].get("verdict"), row["verdict"].get("confidence"), row["verdict"].get("why"),
                "; written down only, the date rule decides" if row["strategy"] == "drift" else ""))
        if row.get("chart"):
            L.append("         chart when bought (labels only): %s" % (", ".join(row["chart"]["tags"]) or "no pattern"))
        if row.get("earnings"):
            L.append("         " + _load("trader-broker", "earnings_watch").words(row["earnings"]))
        if row.get("replay"):
            rp = row["replay"]
            L.append("         replays (shadow): stop hit first in %s%% of %s; typical result after fees %+.1f%% as set, "
                     "%+.1f%% rising, %+.1f%% no stop" % (rp["stop_first_pct"], rp["runs"], rp["median_pct"]["fixed"],
                                                         rp["median_pct"]["rising"], rp["median_pct"]["none"]))
        for i in (row.get("watch") or {}).get("all") or []:
            L.append("         new: %s%s" % (_load("trader-broker", "hold_watch").item_line(i).strip(),
                                              "  [%s]" % i["why"] if i.get("why") else ""))
    if not r["rows"]:
        L.append("  nothing is held")
    if r["due"]:
        L.append("  rule sale due at the next close (its sell date): %s" % ", ".join(r["due"]))
    if r["overdue"]:
        L.append("  !!! PAST ITS SELL DATE and still held: %s" % ", ".join(r["overdue"]))
    if r["approved"]:
        L.append("  a sale you approved, sold at the next close: %s" % ", ".join(r["approved"]))
    for w in r["would_ask"]:
        L.append("  %s %s: %s" % ("already asked you about" if w["asked"] else "WOULD ASK you about selling", w["symbol"], w["why"]))
    return "\n".join(L)


def run(sd, cfg, res, today, bars_fn=None, say=None, push=None, asks=None, verdicts=None, watch=None, earnings=None):
    """Build, save, log and (when worth it) send the review. Never raises; never changes an order or a page."""
    T = _t212()
    try:
        r = build(sd, cfg, res, today, bars_fn=bars_fn, asks=asks, verdicts=verdicts, watch=watch, earnings=earnings)
    except Exception as e:
        r = could_not(today, e)
    try:
        T._write_json(path(sd, r["day"]), r)
    except Exception:
        pass
    if say:
        say(text(r))
    if push and worth_sending(r, sd):
        M = T.messages()
        M.use(sd)
        push(M.daily_review(r))
    return r


def main(argv=None, environ=None):
    ap = argparse.ArgumentParser(description="The daily review of holdings (demo, read-only, report only).")
    ap.add_argument("--state-dir", default="trader")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    T, D = _t212(), _drift()
    env = os.environ if environ is None else environ
    cfg = T._read_json(os.path.join(a.state_dir, "config.json"), {}) or {}
    today = D._today_ny()
    try:
        key, secret = T.credentials(env)
        client = T.Client("demo", key, secret, state_dir=a.state_dir)
        rate = (T._read_json(os.path.join(a.state_dir, "fx.json"), {}) or {}).get("usd_per_gbp") or T.fetch_gbpusd()[0]
        pause = (cfg.get("holdings") or {}).get("pause_on_mismatch", True) is not False if isinstance(cfg.get("holdings"), dict) else True
        res = _ht().daily_match(client, a.state_dir, float(rate), today.isoformat(), _load("trader-broker", "report"), pause=pause)
        asks = (T._read_json(os.path.join(a.state_dir, "state", "news", "asks.json"), {}) or {}).get("asks")
        doc = T._read_json(os.path.join(a.state_dir, "state", "news", "today.json"), {}) or {}
        verdicts = doc.get("verdicts") if doc.get("day") == today.isoformat() and isinstance(doc.get("verdicts"), dict) else {}
        r = build(a.state_dir, cfg, res, today, bars_fn=D.daily_bars, asks=asks, verdicts=verdicts)
    except Exception as e:
        r = could_not(today, T.scrub(str(e), ()))
    print(json.dumps(r, indent=2) if a.json else text(r))
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
