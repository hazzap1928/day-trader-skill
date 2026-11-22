"""Chart labels on past drift trades: did trades with a label (say "falling apart" before the buy) do worse?

Every signal of the tested drift rule (the same as research/pead_stop.py) gets the chart labels of
trader-scanner/chart.py, version 1, from the daily closes BEFORE its buy day. Then, for 2019-2023 and
2024-2026 SEPARATELY, the trades with each label are compared with those without, after costs, both with no
stop (the tested rule) and with the live 3 x ATR stop.

A label "helps" only if the gap has the same sign in both periods and holds up against random splits of the
same trades (a shuffle test: the share of 1,000 random groups of the same size with a gap at least as large).
Labels were fixed in advance (version 1); looking at many labels means some gaps will be luck.

    python3 research/pead_chart.py        # after earnings_fetch.py and prices_fetch.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _data import DATA
import importlib.util, json, random, statistics as st

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
exec(open(os.path.join(HERE, "pead_stop.py")).read().split('if __name__ == "__main__":')[0])

_spec = importlib.util.spec_from_file_location("chart", os.path.join(ROOT, "trader-scanner", "chart.py"))
chart = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(chart)

LABELS = list(chart.WORDS) + ["falling"]
RULES = (("none", "no stop"), ("fixed 3xATR (live)", "the live 3xATR stop"))
SHUFFLES = 1000


def tagged(a, b):
    out = []
    for ev in [ev for ev in events(a, b) if qualifies(ev, RANKING)]:
        o = outcomes(ev)
        I = S[ev["sym"]]
        closes = I["C"][max(0, ev["e"] - 120):ev["e"]]           # closes BEFORE the buy day only
        if o is None or len(closes) < chart.MIN_CLOSES:
            continue
        lab = chart.labels(closes)
        tags = set(lab["tags"]) | ({"falling"} if lab["falling"] else set())
        out.append((tags, {name: o[name]["net"] for name, _ in RULES}))
    return out


def shuffle_p(vals, k, gap, seed=11):
    """Share of random groups of size k whose gap (group minus rest) is at least as far from 0 as `gap`."""
    r, n, hits, tot = random.Random(seed), len(vals), 0, sum(vals)
    for _ in range(SHUFFLES):
        g = r.sample(vals, k)
        g_mean = sum(g) / k
        rest = (tot - sum(g)) / (n - k)
        if abs(g_mean - rest) >= abs(gap):
            hits += 1
    return hits / SHUFFLES


if __name__ == "__main__":
    if not E:
        sys.exit("no trader/research-data/earnings.json: run python3 research/earnings_fetch.py first")
    out = {}
    for label, (a, b) in PERIODS:
        rows = tagged(a, b)
        print("=" * 100)
        print("%s: %d drift trades with chart labels (closes before the buy day only)" % (label, len(rows)))
        out[label] = {}
        for tag in LABELS:
            with_ = [r for t, r in rows if tag in t]
            if len(with_) < 10 or len(rows) - len(with_) < 10:
                print("  %-16s %4d trades: too few to compare" % (tag, len(with_)))
                out[label][tag] = {"n": len(with_)}
                continue
            res = {"n": len(with_)}
            line = "  %-16s %4d trades (%2.0f%%)" % (tag, len(with_), 100.0 * len(with_) / len(rows))
            for name, words in RULES:
                vals = [r[name] for _, r in rows]
                w = [r[name] for t, r in rows if tag in t]
                wo = [r[name] for t, r in rows if tag not in t]
                gap = st.mean(w) - st.mean(wo)
                p = shuffle_p(vals, len(w), gap)
                res[name] = {"with_pct": 100 * st.mean(w), "without_pct": 100 * st.mean(wo), "gap_pct": 100 * gap, "p": p}
                line += " | %s: with %+.2f%%, without %+.2f%%, gap %+.2f (random %.0f%%)" % (
                    words, 100 * st.mean(w), 100 * st.mean(wo), 100 * gap, 100 * p)
            print(line)
            out[label][tag] = res
    print("\nA gap counts only if it has the same sign in both periods AND random groups match it under 5% of the time.")
    for tag in LABELS:
        a_, b_ = out["2019-2023"].get(tag, {}), out["2024-2026"].get(tag, {})
        for name, words in RULES:
            if name in a_ and name in b_:
                ga, gb = a_[name]["gap_pct"], b_[name]["gap_pct"]
                if ga * gb > 0 and a_[name]["p"] < 0.05 and b_[name]["p"] < 0.05:
                    print("  HOLDS UP: %s (%s): %+.2f then %+.2f points a trade" % (tag, words, ga, gb))
    with open(DATA + "pead_chart.json", "w") as fh:
        json.dump(out, fh, indent=1)
    print("Saved %spead_chart.json" % DATA)
