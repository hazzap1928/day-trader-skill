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
    if "total risk" in m:
        return "it would put too much at risk across everything held"
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
           "The practice test since %s: does the plan make money after fees? The S&amp;P 500 is shown to compare, "
           "not as a test." % esc(short_day(r["start"]))]
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


# ---------------------------------------------------------------- the holdings record (daily match, report only)

_MISMATCH = {
    "no_record": "is in the account, but I have no record of buying it",
    "not_held": "is in my records as held, but it isn't in the account and I can't see a sale",
    "qty": "the number of shares in the account doesn't match my records",
    "no_stop": "has no safety net at Trading 212",
    "part_stop": "has a safety net for only part of it",
    "soft_stop": "has a safety net that only this Mac watches, so it only works while the market is open",
    "stray_stop": "isn't held, but a leftover sell order for it is waiting at Trading 212",
    "unknown_label": "has a label I don't know, so I'll keep it and never sell it by a rule",
    "no_sell_rule": "has no sell date in my records, so I'll keep it until you decide",
}


def holdings_mismatch(rows, paused=False):
    """The daily check of my records against Trading 212. Nothing is sold or changed; with `paused`, new
    buys wait until the two agree again."""
    items = ["%s %s" % (company(m.get("symbol")), _MISMATCH.get(m.get("kind"), esc(m.get("what")))) for m in rows]
    return join(["\U0001f50e <b>My records and Trading 212 don't match</b>"] + bullets(items) +
                ["⏸️ <b>I've paused new buys</b> until they match again. I haven't sold or changed anything. "
                 "Please check the Trading 212 app." if paused else
                 "I haven't changed anything. Please check the Trading 212 app."])


def holdings_match_again():
    return "✅ My records and Trading 212 match again, so new buys are allowed again."


# ---------------------------------------------------------------- the daily review (report only)

_KIND = {"intraday": "day trade", "drift": "earnings trade", "news": "news idea", "no record": "no record of buying it"}
_LIMIT = {"cash": "Cash is %s, under the %s I always keep",
          "drift_share": "Earnings trades hold %s, over their %s share",
          "risk_drift": "Earnings trades could lose %s at their safety nets, over their %s limit",
          "risk_news": "News ideas could lose %s at their safety nets, over their %s limit",
          "risk_intraday": "Day trades could lose %s at their safety nets, over their %s limit",
          "risk_total": "Everything held could lose %s at the safety nets, over the %s limit"}


def daily_review(r):
    """The evening review of what is held. It reports; it never sells or asks by itself."""
    head = "\U0001f5c2️ <b>Daily review of what I'm holding</b>"
    if not r.get("ok"):
        return join([head, "⚠️ I couldn't check today: Trading 212 or my records couldn't be read. Nothing was changed."])
    out = [head]
    if r.get("no_stop"):
        out.append("\U0001f6a8 <b>No full safety net:</b> %s. The guard puts it back when the market is open." % ", ".join(
            company(x) for x in r["no_stop"]))
    items = []
    if r.get("mismatches"):
        items.append("❌ My records and Trading 212 don't match (%s)%s" % (
            ", ".join(sorted({company(m["symbol"]) for m in r["mismatches"]})),
            ". New buys are paused until they do" if r.get("paused") else ""))
    else:
        items.append("✅ My records match Trading 212")
    over = [x for x in r.get("limits") or [] if not x.get("ok")]
    if not over:
        items.append("✅ Cash and risk limits are all met")
    for x in over:
        tpl = _LIMIT.get(x.get("name"))
        items.append("⚠️ %s. Nothing is sold to fix it; new buys that add to it are refused" % (
            tpl % (gbp(x.get("now_gbp")), gbp(x.get("limit_gbp"))) if tpl and x.get("limit_gbp") is not None
            else "A money limit couldn't be checked"))
    for row in r.get("rows") or []:
        gain = "result not known" if row.get("gain_gbp") is None else "%s after fees" % b(signed_gbp(row["gain_gbp"]))
        mkt = "" if row.get("market_pct") is None else " (the S&amp;P 500 %s over the same days)" % pct(row["market_pct"], 1)
        net = "safety net at %s" % usd(row["stop"]) if row.get("stop") else "<b>no safety net</b>"
        sell = ", sell by %s" % esc(short_day(row["sell_date"])) if row.get("sell_date") else ""
        v = row.get("verdict") or {}
        read = ""
        if v.get("verdict") in _VERDICT:
            read = ". Today's read: %s%s" % (_VERDICT[v["verdict"]], "" if not v.get("why") else " (%s)" % esc(_short(v["why"], 140)))
            if row.get("strategy") == "drift":
                read += ". Noted only: this one is sold by its date rule"
        items.append("%s (%s): %s%s; %s%s%s%s%s" % (company(row["symbol"]), _KIND.get(row.get("strategy"), "a label I don't know"),
                                                   gain, mkt, net, sell, read, replay_words(row.get("replay")),
                                                   watch_words(row.get("watch")) + earnings_words(row.get("earnings"))))
    if not r.get("rows"):
        items.append("Nothing is held")
    if r.get("due"):
        items.append("\U0001f4c5 Sell date reached: %s will be sold just before the US market next closes" % ", ".join(
            company(x) for x in r["due"]))
    if r.get("approved"):
        items.append("\U0001f4c5 You approved selling %s: sold soon after the US market next opens" % ", ".join(
            company(x) for x in r["approved"]))
    if r.get("overdue"):
        items.append("⚠️ Past its sell date and still held: %s. Please check the Trading 212 app" % ", ".join(
            company(x) for x in r["overdue"]))
    for w in r.get("would_ask") or []:
        items.append("\U0001f440 %s: %s. %s" % (company(w["symbol"]), tidy(w.get("why") or "its reason may be broken"),
                                                 "I've asked you about selling it" if w.get("asked")
                                                 else "I'd ask you about selling it (not switched on yet, so no question was sent)"))
    out += bullets(items)
    out.append("<i>This review sells nothing and changes nothing. A sale needs its sell date or your Approve.</i>")
    return join(out)


# ---------------------------------------------------------------- new filings and headlines on holdings (report only)

_8K_WORDS = {"1.01": "a major agreement", "1.02": "an agreement ended", "1.03": "bankruptcy",
             "1.05": "a cyber attack", "2.01": "buying or selling a business", "2.02": "its results",
             "2.03": "new borrowing", "2.04": "a lender demanding early repayment", "2.05": "restructuring costs",
             "2.06": "a write-down of what its assets are worth", "3.01": "a delisting notice",
             "3.02": "new shares sold privately", "3.03": "shareholder rights changed", "4.01": "a change of auditor",
             "4.02": "past accounts no longer reliable", "5.01": "a change of control",
             "5.02": "a director or senior manager joining or leaving", "5.03": "its rules or year end changed",
             "5.07": "shareholder vote results", "7.01": "a public statement", "8.01": "other news"}


def watch_item(i):
    """One filing or headline in plain words: "SEC filing (Fri 20 Nov): bankruptcy" or the headline."""
    when = esc(short_day(i.get("day"))) if i.get("day") else "date not given"
    if i.get("kind") == "filing":
        what = [_8K_WORDS.get(x, "item %s" % x) for x in i.get("items") or [] if x != "9.01"]
        return "SEC filing (%s): %s" % (when, esc(", ".join(what) or "a company announcement"))
    return "Headline (%s): %s" % (when, esc(_short(i.get("title"), 140)))


def earnings_words(e):
    """A holding's results date inside its hold, when the phone should hear it (first seen, moved, or close)."""
    if not e or not e.get("in_hold") or not e.get("tell") or not e.get("date"):
        return ""
    when = {"after the close": " after the US market closes", "before the open": " before the US market opens"}.get(
        e.get("when"), "")
    sure = " (the company's date)" if e.get("sure") == "expected" else " (an estimate from past dates)"
    days = e.get("days_to")
    soon = "" if days is None else " today" if days == 0 else " in %d trading day%s" % (days, "" if days == 1 else "s")
    return ". \U0001f4c5 It reports results on %s%s%s%s, before its sell date: the share can move a lot that day" % (
        esc(short_day(e["date"])), when, soon, sure)


def replay_words(rp):
    """The holding's sell-rule replay, told once (the review after it is worked out). For information only."""
    if not rp or not rp.get("new") or rp.get("stop_first_pct") is None:
        return ""
    m = rp.get("median_pct") or {}
    if None in (m.get("fixed"), m.get("rising"), m.get("none")):
        return ""
    return (". Replaying its own past ups and downs: the safety net was hit first %s%% of the time; typical result "
            "after fees %s with the safety net as set, %s if it rose with the price, %s with none (for information)" % (
                esc("%.0f" % rp["stop_first_pct"]), pct(m["fixed"], 1), pct(m["rising"], 1), pct(m["none"], 1)))


def watch_words(w):
    """A holding's new items for the daily review: the serious ones, then the other filings, then a headline count."""
    if not w:
        return ""
    parts = ["\U0001f6a9 " + watch_item(i) for i in w.get("serious") or []]
    parts += [watch_item(i) for i in w.get("filings") or [] if not i.get("serious")]
    rest = sum(1 for i in w.get("all") or [] if i.get("kind") != "filing" and not i.get("serious"))
    if rest:
        parts.append("%d other headline%s" % (rest, "" if rest == 1 else "s"))
    return ". New since the last review: " + "; ".join(parts) if parts else ""


def hold_watch_briefing(by_page):
    """The briefing's lines for SERIOUS new filings or headlines on what is held. [] when there are none."""
    rows = [i for items in (by_page or {}).values() for i in items if i.get("serious")]
    if not rows:
        return []
    out = ["\U0001f6a9 <b>News that may affect what I hold</b>"]
    for i in rows:
        out.append("• %s: %s" % (company(i.get("symbol")), watch_item(i)))
    out.append("<i>Report only: nothing is sold or asked because of this. Worth a look at whether the reason for "
               "buying still holds.</i>")
    return out


# ---------------------------------------------------------------- news ideas (bought for a reason, held until a rule)

_STARS = {"high": "high confidence", "medium": "medium confidence", "low": "low confidence"}
_VERDICT = {"keep": "keep", "watch": "keep, but watch it closely", "sell": "sell"}
_VERDICT_DOT = {"keep": "✅", "watch": "\U0001f440", "sell": "\U0001f534"}


def _short(text, n):
    text = str(text or "").strip()
    return text if len(text) <= n else text[:n - 3].rstrip() + "..."


def _levels(want):
    """The confidence levels that may be bought, as a tuple: "high" or ["medium", "high"]."""
    if isinstance(want, str):
        return (want,)
    return tuple(want or ("high",))


def level_words(want):
    got = [x for x in ("low", "medium", "high") if x in _levels(want)]
    return " and ".join(got) or "high"


def _source(src):
    if not src:
        return ""
    return " Source: %s (%s)." % (esc(_short(src.get("title") or "?", 80)), esc(short_day(src.get("date") or "?")))


def verdict_line(v):
    """One holding's verdict from a news review, in plain words."""
    kind = v.get("verdict")
    if kind not in _VERDICT:
        return "%s: no clear view today." % company(v.get("symbol"))
    why = _short(v.get("why") or "no reason given", 200)
    conf = _STARS.get(v.get("confidence"))
    out = "%s %s: <b>%s</b>%s. %s%s" % (_VERDICT_DOT[kind], company(v.get("symbol")), _VERDICT[kind],
                                         "" if not conf else " (%s)" % conf,
                                         esc(why if why.endswith((".", "!", "?")) else why + "."),
                                         _source((v.get("sources") or [None])[0]))
    if v.get("strategy") == "drift":
        out += " Noted only: this one is sold by its date rule, whatever I think."
    elif kind == "sell":
        out += " I've asked you whether to sell it." if v.get("asked") else " I'll ask you whether to sell it."
    return out


def holding_verdicts(rows):
    """The "my read on what I hold" block (a list of lines), or [] when nothing is held."""
    rows = [v for v in rows or [] if v.get("symbol")]
    if not rows:
        return []
    return ["\U0001f9d0 <b>My read on what I'm holding</b>"] + bullets(verdict_line(v) for v in rows)


def hot_topics(topics):
    """The "What the market is talking about" block (a list of lines), or [] when there are none."""
    rows = [t for t in topics or [] if t.get("text")]
    if not rows:
        return []
    out = []
    for t in rows[:5]:
        text = str(t["text"]).strip()
        src = t.get("source") or {}
        out.append("%s%s" % (esc(text if text.endswith((".", "!", "?")) else text + "."),
                             "" if not src else " (%s, %s)" % (esc(_short(src.get("title") or "?", 80)),
                                                             esc(short_day(src.get("date") or "?")))))
    return ["\U0001f5e3️ <b>What the market is talking about</b>"] + bullets(out)


def _idea_line(r, buying, want="high"):
    what = str(r.get("summary") or r.get("why") or "").strip()
    if len(what) > 220:
        what = what[:217].rstrip() + "..."
    what = esc(what)
    date_ok = r.get("date_confidence") in ("medium", "high")      # news_ideas.DATE_LEVELS
    mine = buying and r.get("confidence") in _levels(want) and date_ok
    size = ""
    if r.get("cost_gbp"):
        size = " %s about %s, most it could lose about %s." % (
            "I'd buy" if mine else "Would buy", gbp(r["cost_gbp"]), gbp(r.get("max_loss_gbp")))
    conf = _STARS.get(r.get("confidence"), "confidence not given")
    if buying and not mine:
        conf += (", but its date is not confirmed, so it is only watched" if r.get("confidence") in _levels(want)
                 else ", so it is only watched")
    moved = _num(r.get("moved_pct"))
    return "%s (%s): %s Event: %s. %s by %s.%s%s" % (
        "<b>%s</b>" % company(r["symbol"]), conf, what if what.endswith((".", "!", "?")) else what + ".",
        esc(short_day(r.get("event_date") or "?")), "Sell" if mine else "Would sell",
        esc(short_day(r.get("sell_date") or "?")), size,
        "" if moved is None or abs(moved) < 0.5 else " Already moved about %s on this news." % pct(moved, 0))


def _score(tally):
    t = tally or {}
    if not t.get("ideas"):
        return None
    return "Score so far: %d idea%s, %s after fees if each had been bought." % (
        t["ideas"], "" if t["ideas"] == 1 else "s", signed_gbp(t.get("pl_gbp")))


def news_ideas(doc, tally=None):
    """The main review's ideas as a message of their own: only used when the morning briefing had already
    gone. Practice only unless buying is on."""
    ideas, buying = [r for r in doc.get("ideas") or [] if r.get("from", "main") == "main"], bool(doc.get("buying"))
    want = doc.get("buy_levels") or "high"
    head = "\U0001f4f0 <b>Today's news ideas</b>"
    rejected = [r for r in doc.get("rejected") or [] if r.get("from", "main") == "main"]
    if not ideas:
        out = [head, "No idea passed the checks today%s." % (
            "" if not rejected else " (%d looked at and turned down)" % len(rejected))]
    else:
        out = [head] + bullets(_idea_line(r, buying, want) for r in ideas)
        if rejected:
            n = len(rejected)
            out.append("%d other idea%s didn't pass the checks." % (n, "" if n == 1 else "s"))
    out.append("Buying is on: I look again about an hour before the US market opens, and buy only the %s-confidence "
               "ideas with a confirmed or reported date that still hold, each with a safety net." % esc(level_words(want)) if buying
               else "Practice only: nothing is bought. I just keep score.")
    if _score(tally):
        out.append(_score(tally))
    out.append("<i>Ideas from news reading, not advice. Untested.</i>")
    for block in (hot_topics(doc.get("topics")), holding_verdicts(
            sorted((doc.get("verdicts") or {}).values(), key=lambda v: str(v.get("symbol"))))):
        if block:
            out += [""] + block
    return join(out)


def drift_unread():
    """A line for the briefing's problems: the drift holdings could not be read this morning."""
    return "⚠️ I couldn't read the earnings-drift holdings this morning. Send /status before the US open to check them."


def news_briefing(d):
    """The news ideas part of the morning briefing (a list of lines), from news_ideas.briefing_data."""
    if not d:
        return []
    state = d.get("state")
    if state == "error":
        return ["\U0001f4f0 <b>News ideas</b>", "⚠️ I couldn't read this morning's news ideas: something went wrong "
                                                "on this Mac (the details are in the briefing log)."]
    head = "\U0001f4f0 <b>News ideas</b> (%s)" % ("buying is on" if d.get("buying") else "practice only, nothing is bought")
    if state == "failed":
        return [head, "⚠️ This morning's news review failed%s, so there are no ideas from it. I'll look again about an "
                      "hour before the US market opens." % (
                          ": your Claude plan had reached its usage limit" if d.get("limit") else "")]
    if state == "skipped":
        return [head, "This morning's news review was skipped (this Mac wasn't awake in time). I'll look about an "
                      "hour before the US market opens."]
    if state != "ok":
        return [head, "This morning's news review hasn't finished yet. If it finds ideas they'll follow in a "
                      "message of their own."]
    ideas = d.get("ideas") or []
    if not ideas:
        out = [head, "No idea passed the checks this morning%s." % (
            "" if not d.get("rejected") else " (%d looked at and turned down)" % d["rejected"])]
    else:
        out = [head] + bullets(_idea_line(r, d.get("buying"), d.get("buy_confidence") or "high") for r in ideas)
    if _score(d.get("tally")):
        out.append(_score(d["tally"]))
    if ideas:
        out.append("<i>Ideas from news reading, not advice. Untested. I check them again before the US market opens.</i>")
    topics, read = hot_topics(d.get("topics")), holding_verdicts(d.get("verdicts"))
    if topics:
        out = topics + [""] + out
    if read:
        out += [""] + read
    return out


def news_update(doc, changes):
    """The short message before the US open, sent only when something changed since the morning."""
    buying = bool(doc.get("buying"))
    out = ["\U0001f4f0 <b>News ideas: update before the US market opens</b>"]
    items = []
    for r in changes.get("dropped") or []:
        why = str(r.get("update_why") or "").strip() or "no reason given"
        items.append("Dropped %s: %s" % (company(r["symbol"]), esc(why if why.endswith((".", "!", "?")) else why + ".")))
    for r in changes.get("new") or []:
        items.append("New: " + _idea_line(r, buying, doc.get("buy_levels") or "high"))
    out += bullets(items)
    kept = [r for r in doc.get("ideas") or [] if r.get("state") == "kept"]
    if kept:
        out.append("Still standing from this morning: %s." % ", ".join(company(r["symbol"]) for r in kept))
    out.append("Buying is on: only the %s-confidence ideas with a confirmed or reported date still standing are bought, soon after the open."
               % esc(level_words(doc.get("buy_levels") or "high")) if buying else "Practice only: nothing is bought.")
    changed = [v for v in changes.get("verdicts") or [] if v.get("verdict") in ("watch", "sell")]
    if changed:
        out += [""] + holding_verdicts(changed)
    return join(out)


def news_update_failed(limit, had, buying):
    return join(["⚠️ <b>The news check before the US market opens failed</b>%s." % (
        ": your Claude plan had reached its usage limit" if limit else ""),
        ("This morning's %d idea%s couldn't be re-checked, so %s." % (
            had, "" if had == 1 else "s", "nothing is bought on news today" if buying else "they stay unconfirmed"))
        if had else ("Nothing is bought on news today." if buying else None),
        "Details are in trader/logs/news.log."])


def news_update_missed(had, buying):
    return ("⚠️ The news check before the US market opens didn't run in time (this Mac wasn't awake). This morning's "
            "%d idea%s couldn't be re-checked, so %s." % (
                had, "" if had == 1 else "s", "nothing is bought on news today" if buying else "they stay unconfirmed"))


def news_failed(limit=False):
    return ("⚠️ This morning's news ideas run failed%s, so there are no ideas from it and nothing is bought "
            "because of it. Details are in trader/logs/news.log." % (
                ": your Claude plan had reached its usage limit" if limit else ""))


def friday_news(by, want="high", buying=False):
    """The news ideas part of the Friday message: pretend results by how confident the idea was."""
    out = ["<b>News ideas</b> (%s)" % ("buying is on" if buying else "practice only")]
    rows = [(k, by[k]) for k in ("high", "medium", "low", "unknown") if by.get(k)]
    if not rows:
        return out + ["• No idea has a result yet"]
    for k, g in rows:
        out.append("• %s: %d idea%s, %d made money, %s in all after fees (%s each on average)" % (
            {"unknown": "Confidence not given"}.get(k, k.capitalize() + " confidence"), g["n"], "" if g["n"] == 1 else "s",
            g["wins"], signed_gbp(g["pl_gbp"]), signed_gbp(g["avg_gbp"])))
    out.append("Only %s-confidence ideas may be bought; the others are kept to compare. Too few to prove anything yet."
               % esc(want))
    return out


def friday_verdicts(t):
    """The Friday lines about the daily verdicts on what is held: did acting on a "sell" beat the rule?"""
    out = []
    names = {"drift": "earnings trades", "news": "news ideas"}
    for name in sorted(t or {}):
        g = t[name]
        if not (g["keep"] + g["watch"] + g["sell"]):
            continue
        out.append("• My daily read on %s so far: %d keep, %d watch, %d sell" % (
            names.get(name, esc(name)), g["keep"], g["watch"], g["sell"]))
        if g["compared"]:
            out.append("• Where I said sell and it was held anyway (%d finished): selling the next morning would have "
                       "made %s; holding made %s. Too few to prove anything" % (
                           g["compared"], signed_gbp(g["would_gbp"]), signed_gbp(g["rule_gbp"])))
        elif g["sell"]:
            out.append("• None of those sells has finished yet, so there is nothing to compare yet")
    return out


def news_bought(i):
    items = ["%s at about %s each" % (b(shares(i["qty"])), b(usd(i["price"]))),
             "Cost: %s (including the currency fee)" % b(gbp(i.get("cost_gbp"))),
             "\U0001f6e1️ Safety net: auto-sells if it drops to %s. Most I could lose: %s" % (
                 usd(i["stop"]), b("about " + gbp(i.get("max_loss_gbp"))))
             if i.get("stop_ok", True) else
             "⚠️ <b>The safety net isn't in yet</b> (the guard is on it)",
             "Why: %s" % esc(i.get("why") or "?"),
             "\U0001f4c5 Plan: sell by %s, or sooner if the reason breaks (I'll ask you first)" % esc(day_name(i["sell_date"]))]
    return join(["\U0001f7e2 <b>Bought %s</b> on a news idea" % company(i["symbol"])] + bullets(items))


def approved_sale_sent(sym):
    return ("✅ <b>Selling %s</b>, as you approved. The sell order has gone in; the result will be in this "
            "evening's report." % company(sym))


def news_buy_failed():
    return "⚠️ The news buys hit a problem, so nothing more is bought on them today. Details are in the log."


def news_sell_ask(ask):
    src = (ask.get("sources") or [{}])[0]
    return join(["\U0001f440 <b>Sell %s?</b>" % company(ask["symbol"]),
                 "The reason I bought it looks broken: %s" % esc(ask.get("why") or "?"),
                 "Source: %s (%s)" % (esc(src.get("title") or "?"), esc(short_day(src.get("date") or "?")))
                 if src else None,
                 "Tap Approve to sell it soon after the US market next opens (within minutes if it is open now). "
                 "Tap Reject, or do nothing, to keep it: I'll ask again tomorrow if it still looks broken. "
                 "Its safety net stays in place until it is sold."])


def news_sell_end(text, outcome):
    words = {"approved": "✅ <b>Approved by you.</b> It will be sold soon after the US market next opens. "
                         "Its safety net stays until then.",
             "rejected": "❌ <b>Rejected by you.</b> Keeping it, with its safety net. I'll ask again tomorrow "
                         "if it still looks broken."}
    return text + "\n\n" + words.get(outcome, esc(outcome))


SELL_TAP = {"approved": "Approved: it will be sold soon after the market next opens.",
            "rejected": "Rejected: keeping it.",
            "expired": "That question is from an earlier day: nothing done. I ask again if it still looks broken.",
            "already": "Already answered: nothing more done.",
            "unknown": "Unknown request: nothing done."}


def news_sold(page):
    """A news holding that has been sold (from the holdings record, with Trading 212's own result)."""
    res = page.get("result") or {}
    p = _num(res.get("pl_gbp"))
    dot = "\U0001f7e2" if p is not None and p > 0 else "\U0001f534"
    why = {"stop": "hit the safety net", "sold at the close": "its sell date came, or you approved the sale",
           "sold because you approved it": "you approved the sale",
           "sold in the Trading 212 app": "sold in the Trading 212 app"}.get(str(res.get("why") or ""),
                                                                             esc(res.get("why") or "sold"))
    return join(["%s <b>Sold %s</b>: %s" % (dot, company(page.get("symbol")), why)] + bullets([
        "Bought %s, sold %s" % (esc(short_day(page.get("buy_date") or "?")), esc(short_day(res.get("date") or "?"))),
        "Result: %s" % (b(signed_gbp(p)) if p is not None else "not known yet"),
        "Bought because: %s" % esc(page.get("reason") or "?")]))
