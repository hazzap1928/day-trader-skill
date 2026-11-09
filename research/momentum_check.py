import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _data import DATA
import random, statistics as st, sys
sys.argv = ["x", "none"]
exec(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "three_strategies.py")).read().split('if __name__ == "__main__":')[0])

def momentum_matched(a, b, keep_p, r, top=10):
    """Random picks with momentum's turnover: each month keep each held stock with probability keep_p,
    fill the rest at random from the members that month."""
    ends = month_ends(a, b)
    cash, hold, curve = CAPITAL, {}, []
    for m, ei in enumerate(ends):
        d = CAL[ei]
        if ei + 1 >= len(CAL) or CAL[ei + 1] > b: break
        nd = CAL[ei + 1]
        if ei - 21 * 12 < 0: continue
        elig = [s for s in S if member(s, d) and px(s, nd, "O") and S[s]["V"][S[s]["idx"][d]] * S[s]["C"][S[s]["idx"][d]] >= 20e6
                if S[s]["idx"].get(d) is not None]
        for s in list(hold):
            if r.random() > keep_p or px(s, nd, "O") is None:
                o = px(s, nd, "O") or hold[s]["entry"]; h = hold.pop(s); cash += h["sh"] * o * (1 - K)
        need = top - len(hold)
        joiners = r.sample([s for s in elig if s not in hold], need) if need > 0 else []
        if joiners:
            per = cash / len(joiners)
            for s in joiners:
                o = px(s, nd, "O"); hold[s] = {"sh": per / (o * (1 + K)), "entry": o}; cash -= per
        nxt = ends[m + 1] if m + 1 < len(ends) else len([x for x in CAL if x <= b]) - 1
        for j in range(ei + 1, nxt + 1):
            curve.append(cash + sum(h["sh"] * (px(s, CAL[j]) or h["entry"]) for s, h in hold.items()))
    last = [x for x in CAL if x <= b][-1]
    for s, h in hold.items(): cash += h["sh"] * (px(s, last) or h["entry"]) * (1 - K)
    return cash, curve

for a, b, tag in ((DEV[0], DEV[1], "DEV"), (TEST[0], TEST[1], "UNSEEN")):
    R = momentum(a, b)
    T = R["trades"]
    months = len(month_ends(a, b))
    keep = 1 - (len(T) / months) / 10.0
    rnd = [100 * (momentum_matched(a, b, keep, random.Random(i))[0] / CAPITAL - 1) for i in range(60)]
    strat = 100 * (R["end"] / CAPITAL - 1)
    print("%s momentum %+.1f%%; random picks with the same turnover (keep %.0f%% a month, 60 runs): median %+.1f%%, "
          "5-95%% %+.1f%% to %+.1f%%; strategy beats %d%%" % (tag, strat, 100 * keep, st.median(rnd),
          sorted(rnd)[3], sorted(rnd)[56], round(100 * sum(1 for x in rnd if strat > x) / len(rnd))))
    gains = sorted([(t["ret"] * t["sh"] * t["entry"] * (1 + K), t["sym"], t["entry_date"]) for t in T], reverse=True)
    tot = sum(g for g, _, _ in gains)
    print("   total profit %+.0f GBP; top 5 trades %+.0f GBP (%s); without them %+.0f GBP" % (
        tot, sum(g for g, _, _ in gains[:5]), ", ".join("%s %s %+.0f" % (s, d[:7], g) for g, s, d in gains[:5]),
        tot - sum(g for g, _, _ in gains[:5])))
    yrs = sorted({t["exit_date"][:4] for t in T})
    by = {y: sum(t["ret"] * t["sh"] * t["entry"] * (1 + K) for t in T if t["exit_date"][:4] == y) for y in yrs}
    print("   profit by year of exit: " + ", ".join("%s %+.0f" % (y, v) for y, v in by.items()))
