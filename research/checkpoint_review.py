#!/usr/bin/env python3
"""The review checkpoint, in plain English: is each strategy making money, is Kronos worth keeping, and do
the stops help? Read-only: it reads the holdings record (trader/state/holdings.json) and SPY's daily prices
(Yahoo, unofficial), and changes nothing. The rules are in research/README.md ("Review checkpoint").

    /usr/bin/python3 research/checkpoint_review.py                 # the real record
    /usr/bin/python3 research/checkpoint_review.py --no-spy        # without the S&P 500 comparison (offline)

It works with however many trades there are, and says when there are too few to judge.
Education and tooling, not financial advice.
"""

import argparse
import datetime
import importlib.util
import json
import os
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

HOLD = ("drift", "news")
MIN_TRADES = 20            # completed drift + news trades before any verdict
KRONOS_GAP = 0.01          # "clearly better": the higher-rated half averages at least 1 point more a trade
MIN_STOP_SALES = 5         # stop sales with a finished shadow before even an early reading
STOP_GAP = 0.01            # stops "hurt" or "helped" when the average difference is over 1 point either way
SPY_LEG = 0.0005           # an S&P 500 tracker pays slippage only (GBP-listed: no FX fee)
LABELS = {"drift": "Earnings trades (drift)", "news": "News ideas"}


def _num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v else None


def pct(x):
    return "no data" if x is None else "%+.1f%%" % (100 * x)


def gbp(x):
    return "no data" if x is None else ("-" if x < 0 else "+") + "GBP %.2f" % abs(x)


def completed(pages):
    """Closed drift and news trades with a result in pounds, each with its return after fees."""
    out = []
    for p in pages:
        res = p.get("result") or {}
        pl, cost = _num(res.get("pl_gbp")), _num(p.get("cost_gbp"))
        if p.get("strategy") in HOLD and p.get("status") == "closed" and pl is not None and cost:
            out.append(dict(p, _ret=pl / cost, _pl=pl))
    return out


def spy_return(spy, buy, sell):
    """SPY bought at the open on the buy day, sold at the close on the sale day, 0.05% slippage a leg."""
    if not spy:
        return None
    opens = [v[0] for d, v in sorted(spy.items()) if d >= buy]
    closes = [v[3] for d, v in sorted(spy.items()) if d <= sell]
    if not opens or not closes:
        return None
    return closes[-1] * (1 - SPY_LEG) / (opens[0] * (1 + SPY_LEG)) - 1


def _d(x):
    try:
        return datetime.date.fromisoformat(str(x)[:10])
    except ValueError:
        return None


def section_results(trades, spy):
    out = ["1. RESULTS AGAINST ZERO AND AGAINST THE S&P 500"]
    if not trades:
        out.append("   No drift or news trade has finished yet, so there is nothing to compare.")
    for s in HOLD:
        ts = [t for t in trades if t["strategy"] == s]
        if not ts:
            out.append("   %s: no finished trades yet." % LABELS[s])
            continue
        won = sum(1 for t in ts if t["_pl"] > 0)
        avg = statistics.mean(t["_ret"] for t in ts)
        out.append("   %s: %d finished, %d made money. Total %s, average %s a trade after fees." % (
            LABELS[s], len(ts), won, gbp(sum(t["_pl"] for t in ts)), pct(avg)))
        out.append("      Against zero: %s." % ("making money on average" if avg > 0 else "losing money on average"))
        pairs = [(t["_ret"], spy_return(spy, _d(t.get("buy_date")), _d((t.get("result") or {}).get("date"))))
                 for t in ts]
        pairs = [(a, b) for a, b in pairs if b is not None]
        if not pairs:
            out.append("      Against the S&P 500: not available (no S&P 500 prices).")
        else:
            sa = statistics.mean(b for _, b in pairs)
            beat = sum(1 for a, b in pairs if a > b)
            out.append("      Against the S&P 500 over the same days: the S&P 500 averaged %s; the trades %s it "
                       "(%d of %d trades did better than it)." % (pct(sa), "beat" if statistics.mean(a for a, _ in pairs) > sa
                                                                  else "trailed", beat, len(pairs)))
    if trades and len(trades) < MIN_TRADES:
        out.append("   Too few trades to judge: %d of the %d needed. Treat these as early numbers, mostly luck."
                   % (len(trades), MIN_TRADES))
    return out


def section_kronos(trades):
    out = ["2. KRONOS (THE AI PRICE FORECASTER, WATCHING ONLY)"]
    rated = [t for t in trades if _num((t.get("kronos") or {}).get("score")) is not None]
    out.append("   %d finished trade%s had Kronos's forecast written down before the buy." % (
        len(rated), "" if len(rated) == 1 else "s"))
    if len(rated) < 2:
        out.append("   Not enough to compare yet.")
        return out
    rated.sort(key=lambda t: t["kronos"]["score"], reverse=True)
    half = len(rated) // 2
    hi, lo = rated[:half], rated[len(rated) - half:]
    a, b = statistics.mean(t["_ret"] for t in hi), statistics.mean(t["_ret"] for t in lo)
    out.append("   The %d it rated highest averaged %s a trade; the %d it rated lowest averaged %s." % (
        len(hi), pct(a), len(lo), pct(b)))
    out.append("   (Rated highest = the top half of its forecasts. Kronos's forecasts lean negative, so the halves "
               "are used rather than up or down.)")
    if len(trades) < MIN_TRADES or len(rated) < MIN_TRADES:
        out.append("   Too few to decide: the rule needs %d finished trades with a forecast; there are %d." % (
            MIN_TRADES, len(rated)))
    elif a - b >= KRONOS_GAP:
        out.append("   Verdict: the highly rated trades did clearly better (by %s a trade). Keep watching Kronos; "
                   "it still decides nothing." % pct(a - b).lstrip("+"))
    else:
        out.append("   Verdict: the highly rated trades did NOT clearly do better. The rule says: remove Kronos "
                   "from the agent.")
    return out


def section_stops(pages, enough):
    out = ["3. STOP-LOSSES (THE AUTOMATIC SALE IF THE PRICE FALLS TOO FAR)"]
    sh = [p["stop_shadow"] for p in pages if p.get("strategy") in HOLD and p.get("stop_shadow")]
    done = [s for s in sh if s.get("status") == "done" and _num(s.get("diff_pct")) is not None]
    waiting = len([s for s in sh if s.get("status") == "waiting"])
    out.append("   %d holding%s sold by a stop; %d with the planned sell date passed%s." % (
        len(sh), "" if len(sh) == 1 else "s", len(done), ", %d still waiting for it" % waiting if waiting else ""))
    if not done:
        out.append("   Nothing to compare yet.")
        return out
    avg = statistics.mean(s["diff_pct"] for s in done)
    saved = sum(1 for s in done if s["diff_pct"] < 0)
    money = [_num(s.get("diff_gbp")) for s in done if _num(s.get("diff_gbp")) is not None]
    out.append("   Had they been kept to the sell date, they would have been worth %s on average compared with "
               "the stop's sale price%s." % (pct(avg), "; %s in all" % gbp(sum(money)) if money else ""))
    out.append("   The stop saved money on %d of %d (the price kept falling) and cost money on the rest." % (saved, len(done)))
    if len(done) < MIN_STOP_SALES or not enough:
        out.append("   Too few to judge: wait for the checkpoint (%d finished trades) and at least %d stop sales." % (
            MIN_TRADES, MIN_STOP_SALES))
    elif avg > STOP_GAP:
        out.append("   Recommendation: the stops cost money on average. Consider widening them (for example 4 "
                   "times the typical daily move instead of 3), and look again at the next checkpoint.")
    elif avg < -STOP_GAP:
        out.append("   Recommendation: the stops saved money on average. Keep them as they are.")
    else:
        out.append("   Recommendation: about even. Keep them as they are: they also cap the worst losses.")
    return out


def report(pages, spy=None):
    trades = completed(pages)
    n = {s: len([t for t in trades if t["strategy"] == s]) for s in HOLD}
    out = ["REVIEW CHECKPOINT: %d finished trades (%d earnings, %d news); the checkpoint is at %d." % (
        len(trades), n["drift"], n["news"], MIN_TRADES)]
    if len(trades) < MIN_TRADES:
        out.append("NOT ENOUGH TRADES YET TO DRAW CONCLUSIONS. Earnings trades will be rare until the next "
                   "earnings season (mid to late January), so most early evidence comes from news trades.")
    out += [""] + section_results(trades, spy) + [""] + section_kronos(trades) + [""]
    out += section_stops(pages, len(trades) >= MIN_TRADES)
    out += ["", "Past results, a small sample and a practice account: not financial advice."]
    return "\n".join(out)


def spy_bars(pages):
    days = [d for p in completed(pages) for d in (_d(p.get("buy_date")), _d((p.get("result") or {}).get("date"))) if d]
    if not days:
        return None
    spec = importlib.util.spec_from_file_location("cp_drift", os.path.join(ROOT, "trader-broker", "drift.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m.daily_bars("SPY", min(days) - datetime.timedelta(days=5), max(days))


def main(argv=None):
    ap = argparse.ArgumentParser(description="The review checkpoint (read-only).")
    ap.add_argument("--state-dir", default=os.path.join(ROOT, "trader"))
    ap.add_argument("--no-spy", action="store_true", help="skip the S&P 500 comparison (no internet)")
    a = ap.parse_args(argv)
    try:
        with open(os.path.join(a.state_dir, "state", "holdings.json"), encoding="utf-8") as fh:
            pages = json.load(fh).get("pages") or []
    except (OSError, ValueError):
        pages = []
    spy = None
    if not a.no_spy:
        try:
            spy = spy_bars(pages)
        except Exception as e:
            print("(S&P 500 prices not available: %s)" % str(e)[:120], file=sys.stderr)
    print(report(pages, spy))
    return 0


if __name__ == "__main__":
    sys.exit(main())
