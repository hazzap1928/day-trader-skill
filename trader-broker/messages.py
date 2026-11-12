"""Every word the agent sends to your phone, in one place: one function per message type.

Plain English for someone who is not a trader: no R values, no "stop" or "target" jargon, no order ids,
no "_US_EQ" suffixes. Money in pounds first, share prices in dollars. Telegram HTML (phone.py sends it
with parse_mode HTML and falls back to plain text if Telegram rejects it): every piece of text that
comes from outside (names, errors, reasons) goes through esc().

Only the phone messages live here. The logs and the markdown reports in trader/reports/ keep their
full detail. Presentation only: nothing here decides what is bought or sold.
"""
import datetime
import html
import json
import os
import re

FOOTER = "<i>Practice account</i>"
MINUS = "−"              # a real minus sign: easier to read on a phone than a hyphen

# ---------------------------------------------------------------- basics

def esc(x):
    """Text from outside, made safe for Telegram HTML."""
    return html.escape("" if x is None else str(x), quote=False)


def b(x):
    """Bold, escaped."""
    return "<b>%s</b>" % esc(x)


def plain(text):
    """The same message without HTML: for the fallback when Telegram rejects the formatting."""
    return html.unescape(re.sub(r"</?(b|i|u|s|code|pre)>", "", str(text)))


def _num(x):
    try:
        return None if x is None or x == "" else float(x)
    except (TypeError, ValueError):
        return None


def gbp(x):
    x = _num(x)
    if x is None:
        return "?"
    return ("%s£{:,.2f}" % (MINUS if x < -0.004 else "")).format(abs(x))


def signed_gbp(x):
    x = _num(x)
    if x is None:
        return "not known yet"
    return ("%s£{:,.2f}" % (MINUS if x < -0.004 else "+")).format(abs(x))


def usd(x):
    x = _num(x)
    if x is None:
        return "?"
    return "${:,.2f}".format(x)


def shares(q):
    q = _num(q)
    if q is None:
        return "? shares"
    s = ("%.2f" % q).rstrip("0").rstrip(".")
    return "%s share%s" % (s, "" if s == "1" else "s")


def pct(x, dp=1):
    x = _num(x)
    if x is None:
        return "?"
    return ("%s%." + str(dp) + "f%%") % (MINUS if x < 0 else "+", abs(x))


def day_name(d):
    """Thursday 12 Nov."""
    if isinstance(d, str):
        try:
            d = datetime.date.fromisoformat(d[:10])
        except ValueError:
            return d
    return "%s %d %s" % (d.strftime("%A"), d.day, d.strftime("%b"))


def short_day(d):
    """Thu 12 Nov."""
    if isinstance(d, str):
        try:
            d = datetime.date.fromisoformat(d[:10])
        except ValueError:
            return d
    return "%s %d %s" % (d.strftime("%a"), d.day, d.strftime("%b"))


def uk_clock(dt):
    """2:05pm"""
    h = dt.hour % 12 or 12
    return "%d:%02d%s" % (h, dt.minute, "am" if dt.hour < 12 else "pm")


def bullets(items):
    return ["• " + x for x in items if x]


def join(lines):
    return "\n".join(x for x in lines if x is not None)


# ---------------------------------------------------------------- company names

_STATE = {"dir": None, "names": None, "mtime": None}
_SUFFIX = re.compile(r"(,?\s+(Inc\.?|Incorporated|Corporation|Corp\.?|Co\.?|Company|Ltd\.?|plc|PLC|N\.V\.|"
                     r"S\.A\.|Holdings?|Group|Limited|L\.P\.|Class [A-C]|\(The\)))+$")


def use(state_dir):
    """Company names come from the state dir's universe file (trader/universe/tradable.json)."""
    if state_dir != _STATE["dir"]:
        _STATE.update(dir=state_dir, names=None, mtime=None)


def set_names(names):
    """For tests: {symbol: name}."""
    _STATE.update(dir=None, names=dict(names), mtime="fixed")


def _names():
    if _STATE["mtime"] == "fixed":
        return _STATE["names"] or {}
    d = _STATE["dir"]
    if not d:
        return {}
    path = os.path.join(d, "universe", "tradable.json")
    try:
        m = os.path.getmtime(path)
    except OSError:
        return {}
    if _STATE["names"] is None or _STATE["mtime"] != m:
        out = {}
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
            rows = doc.get("tickers") if isinstance(doc, dict) else doc
            for r in rows or []:
                if isinstance(r, dict) and r.get("symbol") and r.get("name"):
                    out[str(r["symbol"]).upper()] = _SUFFIX.sub("", str(r["name"]).strip()) or str(r["name"])
        except (OSError, ValueError, AttributeError):
            out = {}
        _STATE.update(names=out, mtime=m)
    return _STATE["names"] or {}


def ticker(sym):
    """ADBE from ADBE_US_EQ, or from a bare symbol."""
    s = str(sym or "?").strip()
    return re.sub(r"_[A-Z]{2}_EQ$", "", s).upper()


def company(sym):
    """Adobe (ADBE), or ADBE when the name is not known. Escaped."""
    t = ticker(sym)
    n = _names().get(t)
    if n and n.upper() != t:
        return "%s (%s)" % (esc(n), esc(t))
    return esc(t)


def tidy(text):
    """Outside text (an error, a guard note) with ticker suffixes taken off and the worst jargon replaced.
    Escaped, so it can go straight into a message."""
    t = str(text or "")
    t = re.sub(r"\b([A-Z][A-Z0-9.]{0,6})_[A-Z]{2}_EQ\b", lambda m: m.group(1), t)
    t = re.sub(r",? ?(order id|traceId|trace id)[ :\"]*[\w-]+\"?", "", t, flags=re.I)
    t = re.sub(r"\bat market\b", "straight away", t)
    t = re.sub(r"\bstop orders?\b", "safety-net order", t)
    t = re.sub(r"\bsoftware stops?\b", "safety net watched by this Mac", t)
    t = re.sub(r"\bthe stop\b", "the safety net", t)
    t = re.sub(r"\ba stop\b", "a safety net", t)
    t = re.sub(r"\bstop\b(?= \$?\d)", "safety net", t)
    t = re.sub(r"\s*\(demo\)", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return esc(t)


# ---------------------------------------------------------------- errors: one short human sentence

def why_not(msg):
    """Why a buy did not go through, as a few plain words."""
    m = str(msg or "").lower()
    if "insufficient" in m or "no free cash" in m or "not enough cash" in m or "free cash" in m:
        return "not enough cash in the account"
    if "at or under" in m and "stop" in m:
        return "the price had already fallen to the safety-net level"
    if "too late to chase" in m or "above the" in m and "entry" in m:
        return "the price had already run too far above the planned buy price"
    if "no fresh quote" in m or "no quote" in m:
        return "couldn't get a fresh price"
    if "trader-risk" in m or "daily loss" in m or "trades a day" in m or "blocked" in m:
        return "it no longer passed the safety checks"
    if "changed since" in m:
        return "the plan changed before it could be placed"
    if "market" in m and "closed" in m:
        return "the market was closed"
    if "position" in m and ("max" in m or "open" in m):
        return "too many trades already open"
    return "Trading 212 didn't accept the order"


def not_bought(sym, msg):
    return "⚠️ Couldn't buy %s: %s. Skipped it." % (company(sym), esc(why_not(msg)))


def buy_unclear(sym):
    return join(["⚠️ <b>Not sure whether %s was bought</b>" % company(sym),
                 "Trading 212 didn't give a clear answer. Please check the Trading 212 app."])


def bought_no_safety_net(sym, stop):
    return join(["⚠️ <b>Bought %s, but the safety net didn't go in</b>" % company(sym),
                 "The safety guard will keep trying to set it (auto-sell at %s). If you see this again, "
                 "set a stop-loss in the Trading 212 app." % b(usd(stop))])


def no_cash(syms):
    names = ", ".join(company(s) for s in syms)
    if len(syms) == 1:
        return "⚠️ Couldn't buy %s: not enough cash in the account. Skipped it." % names
    return "⚠️ Couldn't buy %d stocks: not enough cash in the account. Skipped: %s." % (len(syms), names)


# ---------------------------------------------------------------- the past-trades verdict (was the Time Machine)

def _size_words(r):
    if r is None:
        return "no figure"
    if abs(r) < 0.03:
        return "about break-even"
    if r > 0:
        return "a small profit" if r < 0.25 else "a profit"
    return "a small loss" if r > -0.25 else "a loss"


def _pounds(x):
    """About £4, or about 40p: rounded the way you would say it."""
    if abs(x) >= 1:
        return "%s£%d" % (MINUS if x < 0 else "+", round(abs(x)))
    return "%s%dp" % (MINUS if x < 0 else "+", round(abs(x) * 100))


def past_trades(tm, risk_gbp=None):
    """One honest line from the replays of similar past days, or None when there are no figures.
    tm: {"avg_R": before fees, "avg_R_net": after fees}. risk_gbp turns the after-fees figure into pounds."""
    tm = tm or {}
    before, after = _num(tm.get("avg_R")), _num(tm.get("avg_R_net"))
    if before is None:
        return None
    up = (after if after is not None else before) > 0
    head = "\U0001f4c8" if up else "\U0001f4c9"
    s = "%s Past similar trades: %s before fees" % (head, _size_words(before))
    risk = _num(risk_gbp)
    if after is not None:
        if risk:
            s += ", around %s a trade after them" % _pounds(after * risk)
        else:
            s += ", %s after them" % _size_words(after)
    return s


# ---------------------------------------------------------------- buys

def bought(sym, qty, price, cost_gbp, stop, max_loss_gbp, tm=None, trail_from=None, close_uk=None,
           safety="in", pause_hint=True):
    """One message per buy, sent once it has filled, with the real fill price.
    safety: "in" (a safety-net order at Trading 212), "software" (this Mac watches it), "pending" (being set)."""
    net = {"in": "auto-sells if it drops to %s",
           "software": "this Mac sells it if it drops to %s",
           "pending": "being set now: auto-sells if it drops to %s"}.get(safety, "auto-sells if it drops to %s")
    items = ["%s at %s each" % (b(shares(qty)), b(usd(price))),
             "Cost: %s (including the currency fee)" % b(gbp(cost_gbp)) if _num(cost_gbp) is not None else None,
             ("\U0001f6e1️ Safety net: %s" % (net % usd(stop)) + (". Most I could lose: %s" % b("about " + gbp(max_loss_gbp))
              if _num(max_loss_gbp) is not None else ""))
             if _num(stop) else None,
             ("\U0001f4c8 If it climbs past %s, the safety net starts following the price up" % usd(trail_from))
             if _num(trail_from) else None,
             ("Sold by the end of the US day at the latest (%s UK)" % esc(close_uk)) if close_uk else None,
             past_trades(tm, max_loss_gbp)]
    out = ["\U0001f7e2 <b>Bought %s</b>" % company(sym)] + bullets(items)
    if pause_hint:
        out.append("Reply /pause to stop any more buying today.")
    return join(out)


def bought_simple(sym, qty, price):
    """A fill the agent did not plan (bought in the app, say)."""
    return join(["\U0001f7e2 <b>Bought %s</b>" % company(sym),
                 "• %s at %s each" % (b(shares(qty)), b(usd(price)))])


# ---------------------------------------------------------------- sales

REASONS = {
    "stop": "hit the safety net",
    "trailing stop": "the safety net locked in the price",
    "end-of-day close": "sold at the close",
    "half sold at the target": "sold half at the aim price",
    "market sell by the guard (stop crossed)": "hit the safety net (watched by this Mac)",
    "sold in the Trading 212 app": "sold in the Trading 212 app",
}


def reason_words(reason, pl=None):
    r = str(reason or "").strip()
    if r == "trailing stop":
        p = _num(pl)
        return "the safety net locked in a profit" if p is not None and p > 0 else "the raised safety net sold it"
    if r == "stop":
        return "safety net triggered"
    return REASONS.get(r, esc(r) if r else "sold")


def sold(sym, qty, entry, exit_, reason, pl):
    p = _num(pl)
    dot = "\U0001f7e2" if p is not None and p > 0 else "\U0001f534"
    why = REASONS.get(str(reason or ""), None) if reason != "trailing stop" else reason_words(reason, pl)
    head = "%s <b>Sold %s</b>%s" % (dot, company(sym), (": " + esc(why)) if why else "")
    items = ["%s: bought at %s, sold at %s" % (shares(qty), usd(entry), usd(exit_)),
             "Result: %s" % b(signed_gbp(pl))]
    if p is not None and abs(p) < 10 and _num(entry) and _num(exit_) and \
            abs(_num(exit_) - _num(entry)) / _num(entry) < 0.001 and p < 0:
        items.append("The price barely moved, so the fees made it a small loss")
    return join([head] + bullets(items))


def sold_unknown(sym, qty, price):
    """A sale seen before Trading 212's fill history shows it: the result follows in the evening report."""
    return join(["\U0001f534 <b>Sold %s</b>" % company(sym),
                 "• %s, at about %s" % (shares(qty), usd(price)),
                 "• The result will be in this evening's report"])


# ---------------------------------------------------------------- approval requests (when auto-buy is off)

def ticket(sym, qty, price, stop, max_loss_gbp, tm, expires_uk, minutes):
    items = ["%s at about %s each" % (b(shares(qty)), b(usd(price))),
             "\U0001f6e1️ Safety net: auto-sells if it drops to %s. Most I could lose: %s" % (
                 usd(stop), b("about " + gbp(max_loss_gbp))),
             past_trades(tm, max_loss_gbp),
             "All the safety checks passed"]
    return join(["\U0001f440 <b>Buy %s?</b>" % company(sym)] + bullets(items) +
                ["Tap Approve by %s UK (%d minutes). Nothing is bought unless you do." % (esc(expires_uk), minutes)])


def ticket_end(text, outcome, detail=None):
    """The request again, with what happened at the bottom (the buttons are taken away)."""
    words = {"expired": "⏱️ <b>Expired.</b> Nothing was bought.",
             "rejected": "❌ <b>Rejected by you.</b> Nothing was bought.",
             "placed": "✅ <b>Approved and bought.</b> A message follows once it has filled.",
             "refused": "⚠️ <b>Not bought:</b> %s." % esc(why_not(detail)),
             "unclear": "⚠️ <b>Not sure whether it was bought.</b> Please check the Trading 212 app.",
             "no_stop": "⚠️ <b>Bought, but the safety net isn't in yet.</b> The guard will set it."}
    return text + "\n\n" + words.get(outcome, esc(outcome))


TAP = {"unknown": "Unknown request: nothing done.",
       "already": "Already %s: nothing more done.",
       "expired": "Too late: it expired, nothing bought.",
       "rejected": "Rejected: nothing bought.",
       "approved": "Approved: checking once more, then buying.",
       "not_running": "The autopilot isn't running: nothing done."}


# ---------------------------------------------------------------- commands

def paused():
    return ("⏸️ <b>Paused.</b> No more buying until you send /resume. "
            "Anything already bought keeps its safety net.")


def resumed():
    return "▶️ <b>Back on.</b> I'll look for trades again at the next check."


def help_text():
    return join(["Things you can send me:",
                 "• /status: how things are going right now",
                 "• /pause: stop any new buying",
                 "• /resume: start buying again"])


def status(day, market_open, market_line, guard_ok, guard_line, auto_buy, paused_, buys_today, holdings,
           cash_line=None, extra=None):
    """/status. holdings: [(sym, qty, price, safety_words)]."""
    out = ["\U0001f4cb <b>Status, %s</b>" % esc(day_name(day))]
    out.append("• Market: %s" % esc(market_line))
    out.append("• Safety guard: %s" % ("running ✅" if guard_ok else "⚠️ " + esc(guard_line)))
    out.append("• Buying: %s" % ("⏸️ paused (send /resume)" if paused_ else
                                       "automatic ✅" if auto_buy else "asks you first"))
    out.append("• Bought today: %d" % buys_today)
    if cash_line:
        out.append("• " + cash_line)
    out += holding_lines(holdings)
    out += list(extra or [])
    out.append(FOOTER)
    return join(out)


def holding_lines(holdings):
    if not holdings:
        return ["• Holding: nothing right now"]
    out = ["<b>Holding</b>"]
    for sym, qty, price, how in holdings:
        out.append("• %s: %s, now %s. %s" % (company(sym), shares(qty), usd(price), how))
    return out


def safety_words(kind, level=None, covered=None, qty=None):
    """How one holding is protected, in words (kind: order, software, partial, none)."""
    if kind == "order":
        return "\U0001f6e1️ Safety net at %s" % usd(level)
    if kind == "software":
        return "\U0001f6e1️ This Mac sells it at %s" % usd(level)
    if kind == "partial":
        return "⚠️ Safety net covers only %s of %s" % (shares(covered), shares(qty))
    return "⚠️ <b>No safety net</b>"


# ---------------------------------------------------------------- market open

def market_open(now_ny, guard_ok, guard_line, running, auto_buy, paused_, holdings, first, last, drift=None, every=15):
    out = ["\U0001f514 <b>US market open</b> (%s UK)" % esc(now_ny)]
    out.append("• Safety guard: %s" % ("running ✅" if guard_ok else "⚠️ " + esc(guard_line)))
    out.append("• Autopilot: %s" % (
        ("⏸️ paused (send /resume)" if paused_ else "buying automatically ✅" if auto_buy
         else "will ask you before buying") if running else "⚠️ <b>not running as a service</b>"))
    if first and last:
        out.append("• Checking for trades every %d minutes, %s to %s UK" % (every, esc(first), esc(last)))
    out += holding_lines(holdings)
    out += list(drift or [])
    out.append("Reply /pause to stop any buying today.")
    return join(out)


# ---------------------------------------------------------------- the evening report

def evening(r, path, compare=None, watch=None, market=None):
    """The closing report. r: report.build(). Returns one message (phone.py splits it if it is too long)."""
    trades = r.get("today") or []
    known = [t["pl"] for t in trades if t.get("pl") is not None]
    pl = sum(known) if known else (0.0 if not trades else None)
    wins = sum(1 for p in known if p > 0)
    wk = [t["pl"] for t in r.get("week") or [] if t.get("pl") is not None]
    out = ["\U0001f4ca <b>%s: day summary</b>" % esc(day_name(r["day"]))]
    if not r.get("fills_ok"):
        out.append("⚠️ Couldn't read today's trades from Trading 212. Details are in the full report.")
    elif not trades:
        out.append("No trades today.")
    else:
        word = "Up" if pl > 0.004 else "Down" if pl < -0.004 else "Flat"
        dot = "\U0001f7e2" if pl > 0.004 else "\U0001f534" if pl < -0.004 else "⚪"
        out.append("%s <b>%s %s today</b> (%d trade%s, %d winner%s)" % (
            dot, word, gbp(abs(pl)) if word != "Flat" else "", len(trades), "" if len(trades) == 1 else "s",
            wins, "" if wins == 1 else "s"))
    if r.get("fills_ok"):
        out.append("• This week: %s" % b(signed_gbp(sum(wk) if wk else 0.0)))
    out.append("• Account value: %s" % b(gbp(r.get("total"))))
    if trades:
        out += ["", "<b>Today's trades</b>"]
        for i, t in enumerate(trades[:12], 1):
            p = _num(t.get("pl"))
            dot = "\U0001f7e2" if p is not None and p > 0 else "\U0001f534"
            line = "%d. %s %s: %s → %s, %s → %s" % (
                i, dot, company(t["symbol"]), usd(t.get("entry")), usd(t.get("exit")),
                reason_words(t.get("reason"), p), b(signed_gbp(p)))
            out.append(line)
            e, x = _num(t.get("entry")), _num(t.get("exit"))
            if p is not None and p < 0 and e and x and abs(x - e) / e < 0.001:
                out.append("   (price barely moved, so fees made it a small loss)")
        if len(trades) > 12:
            out.append("   and %d more (see the full report)" % (len(trades) - 12))
    if compare:
        out += ["", compare]
    if market:
        out += ["", market]
    problems = []
    if r.get("open_tickers"):
        problems.append("⚠️ <b>Still holding after the close:</b> %s. Please check the Trading 212 app." % ", ".join(
            company(x) for x in r["open_tickers"]))
    if r.get("alerts"):
        problems.append("⚠️ The safety guard raised %d problem%s today (details in the full report)." % (
            len(r["alerts"]), "" if len(r["alerts"]) == 1 else "s"))
    if r.get("unclear"):
        problems.append("⚠️ Some safety-guard orders had an unclear result. Please check the app.")
    for e in r.get("errors") or []:
        problems.append("⚠️ Missing data: %s." % esc(str(e).split(":")[0]))
    if problems:
        out += [""] + problems
    if watch:
        out += [""] + list(watch)
    out += ["", "Full report: %s" % esc(path), FOOTER]
    return join(out)


def compare_line(trades):
    """Today's trades against what the past-trades replays suggested, in words."""
    with_tm = [t for t in trades or [] if (t.get("tm") or {}).get("avg_R") is not None]
    if not with_tm:
        return None
    exp = sum(t["tm"]["avg_R"] for t in with_tm) / len(with_tm)
    got = [t.get("r") for t in with_tm if t.get("r") is not None]
    if not got:
        return None
    g = sum(got) / len(got)
    how = "better than that" if g > exp + 0.1 else "worse than that" if g < exp - 0.1 else "about the same"
    head = "\U0001f4c8" if g > exp + 0.1 else "\U0001f4c9" if g < exp - 0.1 else "⚖️"
    return "%s Past similar trades suggested %s before fees; today's trades did %s. One day is far too few to judge." % (
        head, _size_words(exp), how)


def market_line(spy_pct):
    """How the US market (an S&P 500 tracker) moved today, or None."""
    p = _num(spy_pct)
    if p is None:
        return None
    if abs(p) < 0.1:
        return "⚖️ <b>What the market did:</b> the US market (S&amp;P 500) was flat today."
    return "%s <b>What the market did:</b> the US market (S&amp;P 500) %s %.1f%% today." % (
        "\U0001f4c8" if p > 0 else "\U0001f4c9", "rose" if p > 0 else "fell", abs(p))


# ---------------------------------------------------------------- the earnings watchlist (drift)

def watchlist(w, which):
    """w: drift.watch_data(). which: "closing" (tomorrow) or "briefing" (today). [] with the scan off."""
    if not w or w.get("off"):
        return []
    buying = w.get("buying")
    if which == "closing":
        head = "\U0001f440 <b>Tomorrow's earnings watchlist</b>"
    else:
        head = "\U0001f440 <b>Today's earnings watchlist</b>"
    head += " (%s):" % ("will buy at the open if the checks pass" if buying else "watching only, nothing bought yet")
    if w.get("missing"):
        return [head, "No list yet: the earnings check hasn't run."]
    if w.get("error"):
        return [head, "⚠️ The earnings check didn't finish (details in the log)."]
    names = w.get("names") or []
    if not names:
        n = w.get("rows") or 0
        return [head, "None. %d compan%s reported %s, none fitted the rules." % (
            n, "y" if n == 1 else "ies", "yesterday" if which == "closing" else "on %s" % esc(short_day(w["report_day"])))]
    return [head] + bullets([company(s) for s in names]) + [
        "Would be held until %s." % esc(short_day(w["exit_day"])) if w.get("exit_day") else None]


def drift_holding(r):
    """One drift holding: day X of 20, gain or loss, safety net, sell date."""
    if r.get("waiting"):
        return "%s: buy waiting to fill, to be sold %s" % (company(r["symbol"]), esc(short_day(r.get("sell_date") or "?")))
    net = ("safety net %s" % usd(r["stop"])) if r.get("stop") else "⚠️ no safety net"
    return "%s: day %s of %s, %s, %s, sells %s" % (
        company(r["symbol"]), r.get("day_n") if r.get("day_n") is not None else "?", r.get("hold_days", "?"),
        signed_gbp(r.get("gain_gbp")), net, esc(short_day(r["sell_date"])) if r.get("sell_date") else "at the next close")


def drift_holdings(rows):
    if not rows:
        return []
    out = ["<b>Held for a few weeks (earnings trades)</b>"] + bullets(drift_holding(r) for r in rows)
    bad = [r for r in rows if not r.get("waiting") and not r.get("protected")]
    if bad:
        out.append("⚠️ <b>No safety net overnight:</b> %s. Set a stop-loss in the Trading 212 app." %
                   ", ".join(company(r["symbol"]) for r in bad))
    return out


# ---------------------------------------------------------------- the morning briefing

def briefing(b_, path, watch=None, held=None, futures_pct=None, alerts_n=0, problems=None, news=None,
             earnings_held=None, reserved=None):
    a = b_.get("account") or {}
    out = ["☀️ <b>Good morning: %s</b>" % esc(day_name(b_["session"]))]
    if a.get("total") is not None:
        y = a.get("yesterday")
        out.append("• Account value: %s%s" % (b(gbp(a["total"])), "" if y is None else " (%s: %s)" % (
            esc(b_["prev"].strftime("%A")), signed_gbp(y))))
    else:
        out.append("• Account value: couldn't be read from Trading 212")
    mood = b_.get("mood")
    if mood in ("up", "down", "flat"):
        p = _num(futures_pct)
        how = {"up": "\U0001f4c8 The US market looks set to open higher",
               "down": "\U0001f4c9 The US market looks set to open lower",
               "flat": "⚖️ The US market looks set to open about flat"}[mood]
        out.append("• %s%s" % (how, "" if p is None else " (%s)" % pct(p, 1)))
    rate = (b_.get("fx") or {}).get("rate")
    if rate:
        out.append("• £1 = $%.4f" % rate)
    if news:
        out.append("• \U0001f5d3️ Big US news today: %s. Prices can jump around then." % esc(news))
    out += [""] + list(problems or []) if problems else []
    for sym, when in earnings_held or []:
        out.append("⚠️ You hold %s and it reports earnings %s." % (company(sym), esc(when)))
    if held:
        out += [""] + holding_lines(held)
    if reserved:
        out.append("\U0001f4b7 Cash set aside this morning for: %s (a plan, not a buy)" % ", ".join(company(s) for s in reserved))
    if watch:
        out += [""] + list(watch)
    out += ["", "Full briefing: %s" % esc(path), FOOTER]
    return join(out)


def briefing_missed(start_hm, minutes):
    return ("⚠️ The morning briefing didn't happen: no internet from %s for %d minutes. "
            "Run it again with python3 trader-broker/briefing.py when you can." % (esc(start_hm), minutes))


# ---------------------------------------------------------------- the safety guard

def safety_net_set(sym, qty, stop):
    return "\U0001f6e1️ Safety net is now in for %s: auto-sells %s if it drops to %s." % (
        company(sym), shares(qty), b(usd(stop)))


def safety_net_raised(sym, old, new):
    return "\U0001f6e1️ %s is doing well, so I raised its safety net from %s to %s." % (
        company(sym), usd(old), b(usd(new)))


def sold_half(sym):
    return "\U0001f3af %s reached the aim price: sold half, the rest keeps its safety net." % company(sym)


def guard_problem(sym, kind, note=None):
    """A guard row worth your phone: STOP SALE FAILED, stop failed, unclear, below stop."""
    words = {"STOP SALE FAILED": "the price fell to the safety net but the sale was refused. Please sell it in the Trading 212 app",
             "stop failed": "Trading 212 refused the safety net. This Mac will watch the price instead",
             "unclear": "a safety-guard order had an unclear result. Please check the Trading 212 app",
             "below stop": "the price is already under the safety net level. Please check the Trading 212 app"}
    return "⚠️ %s: %s." % (company(sym), words.get(kind, tidy(note or kind)))


def guard_warning(text):
    return "⚠️ Safety guard: %s" % tidy(text)


_ALARMS = [
    (r"^STOP (.+?) for (\S+) \(SELL ([\d.]+) at ([\d.]+)\)",
     lambda m: join(["⚠️ <b>%s: Trading 212 %s the safety net</b>" % (
         company(m.group(2)), "dropped" if "GONE" in m.group(1).upper() or "CANCEL" in m.group(1).upper()
         else "refused"),
                     "• This Mac is watching it instead and will sell if it drops to %s" % b(usd(m.group(4))),
                     "• Keep the Mac awake, or set a stop-loss in the Trading 212 app yourself"])),
    (r"^DRIFT (\S+): bought, but its stop at \$([\d.]+)",
     lambda m: join(["⚠️ <b>Bought %s, but its safety net didn't go in</b>" % company(m.group(1)),
                     "It has no protection overnight until it does. The safety guard keeps trying; you can also "
                     "set a stop-loss at %s in the Trading 212 app." % b(usd(m.group(2)))])),
    (r"^END-OF-DAY CLOSE FAILED[,:]? ?(.*)",
     lambda m: join(["⚠️ <b>Couldn't sell everything at the close</b>",
                     "Something may still be open overnight. Please check the Trading 212 app and sell it there.",
                     "Detail: %s" % tidy(m.group(1))])),
    (r"^END-OF-DAY CLOSE: (.*)",
     lambda m: "⚠️ At the close: %s" % tidy(m.group(1))),
    (r"^DRIFT SELL OVERDUE: (\S+)",
     lambda m: "⚠️ <b>%s should have been sold by now</b> (its few weeks are up). Please check the Trading 212 app." % company(m.group(1))),
    (r"^Trading 212 rejected the API key",
     lambda m: join(["⚠️ <b>Trading 212 rejected the API key: the safety guard has stopped</b>",
                     "Nothing is being watched. Check the key in the Trading 212 app, update ~/.zshrc, then restart "
                     "the guard. Set any missing stop-loss in the app now."])),
    (r"^GUARD OFFLINE for (\d+) minutes",
     lambda m: join(["⚠️ <b>No internet for %s minutes: the safety guard can't watch anything</b>" % m.group(1),
                     "Safety nets held at Trading 212 still work; the ones this Mac watches don't. Check the Mac's internet."])),
    (r"^(\d+) checks in a row have failed",
     lambda m: "⚠️ The safety guard couldn't reach Trading 212 %s times in a row. Nothing is being watched until it gets through." % m.group(1)),
    (r"^the guard is checking again",
     lambda m: "✅ The safety guard is reaching Trading 212 again."),
    (r"^the guard (has STOPPED|did not start)",
     lambda m: "⚠️ <b>The safety guard has stopped.</b> Nothing is being watched. Details are in trader/logs/guard.log."),
]


def guard_alert(text):
    """A loud guard alarm, in plain words, still saying what to do."""
    t = str(text or "").strip()
    for pat, fn in _ALARMS:
        m = re.match(pat, t)
        if m:
            return fn(m)
    return "⚠️ <b>Safety guard problem</b>\n%s" % tidy(t)


# ---------------------------------------------------------------- drift (buys held for a few weeks after earnings)

def drift_bought(i):
    items = ["%s at %s each" % (b(shares(i["qty"])), b(usd(i["price"]))),
             "Cost: %s (including the currency fee)" % b(gbp(i.get("cost_gbp"))),
             "\U0001f6e1️ Safety net: auto-sells if it drops to %s. Most I could lose: %s" % (
                 usd(i["stop"]), b("about " + gbp(i.get("max_loss_gbp"))))
             if i.get("stop_ok", True) else
             "⚠️ <b>The safety net isn't in yet</b> (the guard is on it)",
             "\U0001f4c5 Plan: hold for about 4 weeks, sell on %s" % esc(day_name(i["sell_date"]))]
    return join(["\U0001f7e2 <b>Bought %s</b> after its good earnings" % company(i["symbol"])] + bullets(items))


def drift_sold(row):
    p = _num(row.get("pl_gbp"))
    dot = "\U0001f7e2" if p is not None and p > 0 else "\U0001f534"
    why = {"sell date": "its few weeks were up", "stop": "hit the safety net"}.get(str(row.get("reason") or ""),
                                                                                    esc(row.get("reason") or ""))
    items = ["Held %d trading day%s (%s to %s)" % (row["days"], "" if row["days"] == 1 else "s",
                                                   esc(short_day(row["buy_day"])), esc(short_day(row["exit_day"]))),
             "Result: %s" % (b(signed_gbp(p)) if p is not None else "not known yet")]
    v = _num(row.get("vs_spy_pct"))
    if v is not None:
        items.append("%s the US market over the same days by %.1f%%" % ("Beat" if v >= 0 else "Behind", abs(v)))
    return join(["%s <b>Sold %s</b>: %s" % (dot, company(row["symbol"]), why)] + bullets(items))


def drift_missed(start_hm):
    return "⚠️ Missed today's earnings buys: the autopilot wasn't running at %s New York. Nothing was bought late." % esc(start_hm)


def drift_none(err):
    return "⚠️ No earnings buys today: %s. Nothing was bought." % tidy(err)


def drift_failed():
    return "⚠️ The earnings buys hit a problem, so nothing more is bought today. Details are in the log."


# ---------------------------------------------------------------- the Friday report

def _frac(x):
    """A fraction (0.012) as a percent (+1.20%), about 0% when it rounds to nothing."""
    x = _num(x)
    if x is None:
        return "no data"
    if abs(x) < 0.00005:
        return "about 0%"
    return pct(100 * x, 2)


def _track(p):
    if p is None:
        return "no data"
    if p["status"] == "TOO FEW TRADES TO JUDGE":
        s = "too few trades to judge yet (%d of %d)" % (p["n"], p["min_trades"])
        if p.get("lean"):
            s += ", %s so far" % ("on track" if p["lean"] == "ON TRACK" else "not on track")
        return s
    return "✅ on track" if p["status"] == "ON TRACK" else "❌ not on track"


def friday(r, drift=None):
    week, since = r.get("week"), r.get("since_start")
    out = ["\U0001f4ca <b>Week summary, %s</b>" % esc(day_name(r["today"])),
           "The practice test since %s: does the plan make money after fees, and beat simply holding the S&amp;P 500?"
           % esc(short_day(r["start"]))]
    for label, p in (("This week", week), ("Since the start", since)):
        if not p:
            continue
        out.append("• <b>%s</b>: %s. %s over %d trade%s; the plan %s vs the S&amp;P 500 %s" % (
            label, _track(p), signed_gbp(p.get("total_gbp")), p["n"], "" if p["n"] == 1 else "s",
            _frac(p.get("strategy_ret")), _frac(p.get("tracker_ret"))))
    out.append("• Real trades so far: %d, average %s a trade after fees, total %s" % (
        len(r.get("real") or []), signed_gbp(r.get("real_avg_gbp")), signed_gbp(r.get("real_total_gbp"))))
    vr = _num(r.get("virtual_avg_ret"))
    out.append("• Trades it spotted but only pretended to make: %d, average %s each after fees%s" % (
        r.get("virtual") or 0, _frac(vr) if vr is not None else "no data",
        "" if r.get("virtual_win") is None else ", %.0f%% made money" % r["virtual_win"]))
    se, sx = _num(r.get("slip_entry")), _num(r.get("slip_exit"))
    if se is not None or sx is not None:
        out.append("• Prices I actually got: %s worse than expected when buying, %s when selling" % (
            "?" if se is None else "%.2f%%" % (100 * se), "?" if sx is None else "%.2f%%" % (100 * sx)))
    k = r.get("kronos") or {}
    if k and (k.get("agree", [0])[0] or k.get("disagree", [0])[0]):
        a, d = k["agree"], k["disagree"]
        better = None
        if a[1] is not None and d[1] is not None:
            better = "agreed" if a[1] > d[1] else "disagreed"
        out.append("• The AI forecaster (watching only) agreed with %d signals and disagreed with %d%s" % (
            a[0], d[0], "" if not better else "; the ones it %s with did better" % better))
    if drift:
        out += [""] + list(drift)
    out += ["", FOOTER]
    return join(out)


def friday_drift(real, shadow):
    """real/shadow: {"n", "avg", "vs", "status"} or None."""
    out = ["<b>Earnings trades (held a few weeks)</b>"]
    for label, g in (("Real", real), ("Practice only (not bought)", shadow)):
        if not g or not g["n"]:
            out.append("• %s: none finished yet" % label)
            continue
        out.append("• %s: %d finished, average %s after fees%s; %s" % (
            label, g["n"], pct(g["avg"], 2), "" if g.get("vs") is None else ", %s the US market by %.1f%%" % (
                "beat" if g["vs"] >= 0 else "behind", abs(g["vs"])), esc(g["status"])))
    return out


# ---------------------------------------------------------------- the system

def back_online(what, start_hm, end_hm, minutes, missed, extra=None):
    out = ["✅ <b>Back online.</b> %s had no internet from %s to %s (%d min)." % (
        esc(what), esc(start_hm), esc(end_hm), minutes)]
    out.append("Missed: %s." % tidy("; ".join(missed)) if missed else "Nothing was missed.")
    out += [tidy(x) for x in extra or []]
    return join(out)


def offline_paused(minutes):
    return ("⚠️ <b>No internet for %d minutes,</b> so I've paused buying for the rest of today. "
            "The safety guard keeps watching anything already bought when it can reach Trading 212." % minutes)


def stopped():
    return "⚠️ <b>The autopilot has stopped.</b> Details are in trader/logs/autopilot.log."


def not_running(problems):
    return join(["⚠️ <b>The autopilot isn't running</b>, because:"] + bullets(tidy(p) for p in problems) +
                ["It tries again every 5 minutes once that's fixed."])


def slow_cycle(minutes, seconds, gap, n):
    return ("⚠️ One check for trades took %dm %02ds, close to the %d-minute gap between checks "
            "(%d stocks on the list). The list may need trimming." % (minutes, seconds, gap, n))


def budget_not_updated():
    return "⚠️ Couldn't update today's spending limits from the account value. Yesterday's limits stay."


def report_failed(which):
    return "⚠️ Couldn't build %s. Details are in trader/logs/autopilot.log." % esc(which)


def universe_built(n):
    return "\U0001f4cb The list of stocks I can trade is ready: %d companies." % n


def universe_changed(n, was, added, removed, limit=8):
    out = ["\U0001f4cb <b>This week's list of stocks I can trade:</b> %d companies (was %d)" % (n, was)]
    if added:
        out.append("• Added: %s%s" % (", ".join(company(x) for x in added[:limit]),
                                           "" if len(added) <= limit else " and %d more" % (len(added) - limit)))
    if removed:
        out.append("• Dropped: %s%s" % (", ".join(company(x) for x in removed[:limit]),
                                             "" if len(removed) <= limit else " and %d more" % (len(removed) - limit)))
    return join(out)


def universe_failed():
    return "⚠️ Couldn't refresh the list of stocks I can trade, so last week's list stays. Details are in trader/logs/universe.log."


def delivered_late(items, dropped=0):
    """items: [(when, text)]: messages that waited while the phone couldn't be reached."""
    out = ["\U0001f4ec <b>Sent late</b>: %d message%s that couldn't be sent at the time%s" % (
        len(items), "" if len(items) == 1 else "s", "" if not dropped else " (%d older ones dropped)" % dropped)]
    for when, text in items:
        out += ["", "<i>%s</i>" % esc(when), text]
    return join(out)


def paired(name):
    return "✅ Paired with this Mac's trading agent. Only taps from you, %s, are accepted." % esc(name)


def test_tap():
    return "\U0001f9ea Test from the trading agent: tap a button. Nothing is bought, this is only a test."
