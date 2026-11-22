#!/usr/bin/env python3
"""New SEC filings and headlines on what is held (drift and news holdings). REPORT ONLY.

The old day-trading news monitor (trader-news/news.py) read Yahoo headlines and each company's 8-K filings
for a watchlist of the day. This reuses it for holdings: every open drift or news page of the holdings record
gets the 8-Ks (official company announcements to the SEC) and Yahoo headlines published since its buy day.
Each one is kept once, with when it was found, and marked "serious" by a fixed rule (below).

Where it shows:
  - the daily review (after the close): each holding's new filings, the serious items, and a headline count
  - the 07:30 briefing: the serious items only, each once, at most MAX_BRIEFING a holding

It never places, cancels or changes an order, never sends a question or buttons, never edits a holdings page,
and blocks nothing. Off unless the config has "hold_watch": {"enabled": true}. Broker-neutral: it reads the
holdings record (trader-broker/holdings.py) only, so an Interactive Brokers version can reuse it.

The "serious" rule (version 1, fixed in advance; changing it starts version 2):
  - an 8-K with one of SERIOUS_8K's items (bankruptcy, a cyber attack, a lender demanding early repayment,
    a write-down, a delisting notice, a new auditor, past accounts no longer reliable, a change of control)
  - a headline whose TITLE names the company (its name, or its ticker in capitals) AND matches one of
    SERIOUS_WORDS (bankruptcy, fraud, a restatement, SEC charges, a halt, a CEO or CFO leaving, a forecast
    cut, a takeover, a recall, a hack...)
  Routine 8-Ks (results, director changes, votes, agreements) and other headlines are shown in the review only.

Files (trader/state/holdwatch/): state.json (the SEC ticker list, every item found, what each place has
shown), items.jsonl (every new item once, for judging the rule later).

    python3 trader-broker/hold_watch.py show            # what has been found for each holding
    python3 trader-broker/hold_watch.py check           # look now (network: Yahoo and SEC), print what is new

Headlines are leads, not facts. Education and tooling, not financial advice.
"""

import argparse
import datetime
import importlib.util
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

RULE_VERSION = 1
MAX_BRIEFING = 3              # serious items a holding in one briefing
MAX_KEEP = 200                # items kept a holding
CIK_DAYS = 7                  # the SEC ticker list is fetched again after this many days
SHOW_PLACES = ("review", "briefing")
WATCHED = ("open", "sell_asked", "selling", "mismatch")       # bought and held (not "waiting", not "closed")

SERIOUS_8K = {
    "1.03": "bankruptcy",
    "1.05": "a cyber attack",
    "2.04": "a lender demanding early repayment",
    "2.06": "a write-down of what its assets are worth",
    "3.01": "a delisting notice",
    "4.01": "a change of auditor",
    "4.02": "past accounts no longer reliable",
    "5.01": "a change of control",
}

SERIOUS_WORDS = [
    ("bankruptcy", r"\bbankrupt\w*|\bchapter 11\b|\bgoing concern\b|\binsolven\w*"),
    ("delisting or halt", r"\bdelist\w*|\btrading (?:halt|pause|suspen)\w*|\bhalt(?:s|ed)? trading\b|\bshares? halted\b"),
    ("fraud or accounting", r"\bfraud\w*|\brestate(?:s|d|ment)?\b|\baccounting (?:irregularit\w*|probe|issues?|errors?)\b"),
    ("charges or probe", r"\bSEC charges?\b|\bsubpoena\w*|\bindict\w*|\b(?:DOJ|SEC|FTC|criminal) (?:probe|investigation)\b|\braid(?:s|ed)?\b"),
    ("short seller report", r"\bshort[- ]sell(?:er|ers|ing) report\b|\bshort report\b"),
    ("CEO or CFO leaving", r"\b(?:CEO|CFO|chief executive|chief financial officer)\b[^.]{0,40}\b(?:resign\w*|steps? down|stepping down|ousted|fired|departs?|departure|exits?|leav(?:es|ing)|to leave)\b"),
    ("forecast cut", r"\b(?:cuts?|lowers?|slashes|withdraws?|pulls?|suspends?)\b[^.]{0,30}\b(?:guidance|forecast|outlook)\b|\bprofit warning\b|\bwarns on (?:profit|sales|revenue)\b"),
    ("takeover", r"\b(?:to be acquired|agrees? to be (?:bought|acquired)|takeover (?:bid|offer|approach)|buyout (?:offer|bid)|hostile bid)\b"),
    ("recall or hack", r"\brecall\w*\b|\bdata breach\b|\bcyber ?attack\w*|\bransomware\b|\bhack(?:ed|ers?)\b"),
    ("regulator says no", r"\bFDA (?:rejects?|declines?)\b|\bcomplete response letter\b|\bclinical hold\b|\bblocks? (?:the )?(?:deal|merger)\b"),
]
_SERIOUS_RX = [(label, re.compile(rx, re.I)) for label, rx in SERIOUS_WORDS]

_MODS = {}


def _load(folder, name):
    key = (folder, name)
    if key not in _MODS:
        spec = importlib.util.spec_from_file_location("holdwatch_%s_%s" % (folder.replace("-", "_"), name),
                                                      os.path.join(ROOT, folder, name + ".py"))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        _MODS[key] = m
    return _MODS[key]


def news():
    return _load("trader-news", "news")


def _hold():
    return _load("trader-broker", "holdings")


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _date(v):
    try:
        return datetime.date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- settings and files

def settings(cfg):
    s = (cfg or {}).get("hold_watch")
    s = s if isinstance(s, dict) else {}
    return {"enabled": s.get("enabled") is True, "filings": s.get("filings", True) is not False,
            "headlines": s.get("headlines", True) is not False}


def folder(sd):
    return os.path.join(sd, "state", "holdwatch")


def state_path(sd):
    return os.path.join(folder(sd), "state.json")


def log_path(sd):
    return os.path.join(folder(sd), "items.jsonl")


def load(sd):
    try:
        with open(state_path(sd), encoding="utf-8") as fh:
            doc = json.load(fh)
        if isinstance(doc, dict):
            doc.setdefault("pages", {})
            return doc
    except (OSError, ValueError):
        pass
    return {"pages": {}}


def save(sd, doc):
    os.makedirs(folder(sd), exist_ok=True)
    tmp = "%s.%d.tmp" % (state_path(sd), os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, sort_keys=True)
    os.replace(tmp, state_path(sd))


def _log(sd, rows):
    if not rows:
        return
    os.makedirs(folder(sd), exist_ok=True)
    with open(log_path(sd), "a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, sort_keys=True) + "\n")


def sec_agent(cfg, environ=None):
    """The SEC's required User-Agent (name and email), from SEC_USER_AGENT or the config; None without one."""
    env = os.environ if environ is None else environ
    ua = str(env.get("SEC_USER_AGENT") or (cfg or {}).get("sec_user_agent") or "").strip()
    if not ua or "@" not in ua or news().PLACEHOLDER in ua:
        return None
    return ua


def names(sd):
    """{symbol: short company name} from the universe file, the same names the phone uses."""
    M = _load("trader-broker", "messages")
    M.use(sd)
    try:
        return dict(M._names())
    except Exception:
        return {}


# ---------------------------------------------------------------- the rule

def held_pages(doc):
    """The holdings pages to watch: drift and news (any label but day trades), bought and still held."""
    H = _hold()
    return [p for p in doc.get("pages") or [] if p.get("status") in WATCHED and H.is_hold(p) and p.get("symbol")]


GENERIC_FIRST = {"american", "general", "united", "first", "bank", "national", "international", "global",
                 "public", "royal", "southern", "northern", "western", "eastern", "the", "new", "digital", "applied",
                 "advanced", "consolidated", "federal", "texas", "pacific", "atlantic", "central", "realty", "energy"}


def _name_forms(name):
    """The company name, and its first word when that is distinctive ("Ford" for "Ford Motor")."""
    name = str(name or "").strip()
    out = [name] if len(name) >= 3 else []
    words = name.split()
    if len(words) > 1 and len(words[0]) >= 4 and words[0].lower().strip(".,") not in GENERIC_FIRST:
        out.append(words[0].strip(".,"))
    return out


def names_company(title, sym, name):
    """True when a headline title names the company: its short name, or its ticker in capitals (3+ letters,
    or any length as "(P)" or "$P")."""
    title = str(title or "")
    sym = str(sym or "").upper()
    for n in _name_forms(name):
        if re.search(r"\b%s\b" % re.escape(n).replace(r"\ ", r"[\s-]+"), title, re.I):
            return True
    if not sym:
        return False
    if re.search(r"\(%s\)|\$%s\b|\b(?:NYSE|NASDAQ|Nasdaq)\s*:\s*%s\b" % ((re.escape(sym),) * 3), title):
        return True
    return len(sym) >= 3 and re.search(r"(?<![A-Za-z])%s(?![A-Za-z])" % re.escape(sym), title) is not None


def serious(item, sym, name=None):
    """(True, why) when the item meets the serious rule (version RULE_VERSION), else (False, None)."""
    if item.get("kind") == "filing":
        hit = [SERIOUS_8K[i] for i in item.get("items") or [] if i in SERIOUS_8K]
        return (True, "SEC filing: " + ", ".join(hit)) if hit else (False, None)
    title = item.get("title") or ""
    if not names_company(title, sym, name):
        return False, None
    for label, rx in _SERIOUS_RX:
        if rx.search(title):
            return True, "headline: " + label
    return False, None


def _ny_day(t):
    if t is None:
        return None
    return news().to_eastern(t).date()


# ---------------------------------------------------------------- the check

def _ciks(fetcher, doc, now):
    at = doc.get("ciks_at")
    fresh = False
    try:
        fresh = bool(doc.get("ciks")) and (now - datetime.datetime.fromisoformat(at)).days < CIK_DAYS
    except (TypeError, ValueError):
        fresh = False
    if fresh:
        return doc["ciks"]
    st = {}
    got = news().cik_map(fetcher, st)
    doc["ciks"], doc["ciks_at"] = got, now.isoformat()
    return got


def check(sd, cfg, now=None, fetcher=None, environ=None):
    """Look for new filings and headlines on every held drift and news holding. Saves what is new; returns
    {"ok", "new": [item...], "errors": [...], "watched": [symbols]}. Never raises for a feed failure; never
    changes an order or a page."""
    N, now = news(), now or _now()
    s = settings(cfg)
    pages = held_pages(_hold().load(sd))
    doc = load(sd)
    out = {"ok": True, "new": [], "errors": [], "watched": [p["symbol"] for p in pages], "checked": now.isoformat()}
    if not pages:
        doc["checked"] = now.isoformat()
        save(sd, doc)
        return out
    ua = sec_agent(cfg, environ)
    if fetcher is None:
        fetcher = N.Fetcher(ua)
    sec = s["filings"]
    if sec and not ua and not getattr(fetcher, "offline", None) and not getattr(fetcher, "fake", False):
        out["errors"].append("SEC filings skipped: no sec_user_agent in the config")
        sec = False
    nm = names(sd)
    ciks = {}
    if sec:
        try:
            ciks = _ciks(fetcher, doc, now)
        except Exception as e:
            out["errors"].append("SEC ticker list: %s" % str(e)[:160])
            sec = False
    logged = []
    for p in pages:
        sym = str(p["symbol"]).upper()
        buy = _date(p.get("buy_date"))
        items = []
        if s["headlines"]:
            try:
                items += N.parse_rss(fetcher.get(N.YAHOO % N.yahoo_symbol(sym), "yahoo-%s.xml" % sym), sym)
            except Exception as e:
                out["errors"].append("headlines for %s: %s" % (sym, str(e)[:160]))
        if sec:
            cik = ciks.get(sym)
            if not cik:
                out["errors"].append("no SEC company number for %s" % sym)
            else:
                try:
                    data = fetcher.get(N.SEC_FEED % (cik, "8-K", 10), "sec-8-K-%s.xml" % cik, sec=True)
                    items += [i for i in N.parse_sec_atom(data, sym) if str(i.get("form") or "").upper().startswith("8-K")]
                except Exception as e:
                    out["errors"].append("SEC filings for %s: %s" % (sym, str(e)[:160]))
        items = N.dedupe(N.tag(items, {sym: nm.get(sym)}))
        rec = doc["pages"].setdefault(p["id"], {"symbol": sym, "strategy": _hold().strategy_of(p), "items": []})
        have = {i["id"] for i in rec["items"]}
        for it in sorted(items, key=lambda i: i["time"] or now):
            day = _ny_day(it["time"])
            if it["id"] in have or day is None or (buy and day < buy) or it["time"] > now + datetime.timedelta(minutes=10):
                continue
            hit, why = serious(it, sym, nm.get(sym))
            row = {"id": it["id"], "page": p["id"], "symbol": sym, "strategy": rec["strategy"], "kind": it["kind"],
                   "form": it.get("form"), "items": it.get("items") or [], "title": it.get("title") or "",
                   "link": it.get("link") or "", "time": it["time"].isoformat(), "day": day.isoformat(),
                   "tags": it.get("tags") or [], "serious": hit, "why": why, "rule": RULE_VERSION,
                   "found_at": now.isoformat(), "shown": []}
            rec["items"].append(row)
            have.add(it["id"])
            out["new"].append(row)
            logged.append({k: v for k, v in row.items() if k != "shown"})
        rec["items"] = rec["items"][-MAX_KEEP:]
    doc["checked"] = now.isoformat()
    doc["errors"] = out["errors"]
    save(sd, doc)
    _log(sd, logged)
    return out


def take(sd, where, serious_only=False, page_ids=None):
    """{page id: [items not yet shown in `where`]} (oldest first), and marks them shown there. Serious only for
    the briefing, at most MAX_BRIEFING a holding (the rest are left for the review)."""
    if where not in SHOW_PLACES:
        raise ValueError("where must be one of %s" % (SHOW_PLACES,))
    doc = load(sd)
    out = {}
    for pid, rec in (doc.get("pages") or {}).items():
        if page_ids is not None and pid not in page_ids:
            continue
        got = [i for i in rec.get("items") or [] if where not in (i.get("shown") or [])
               and (i.get("serious") or not serious_only)]
        if serious_only:
            got = got[:MAX_BRIEFING]
        for i in got:
            i.setdefault("shown", []).append(where)
        if got:
            out[pid] = [{k: v for k, v in i.items() if k != "shown"} for i in got]
    if out:
        save(sd, doc)
    return out


def run(sd, cfg, where, now=None, fetcher=None, environ=None, say=None):
    """check() then take(): what a place should show now, as {"by_page": {...}, "errors": [...], "on": bool}.
    Off (the default): {"on": False}, nothing fetched. Never raises."""
    if not settings(cfg)["enabled"]:
        return {"on": False, "by_page": {}, "errors": []}
    try:
        res = check(sd, cfg, now=now, fetcher=fetcher, environ=environ)
        got = take(sd, where, serious_only=(where == "briefing"))
        if say:
            say("hold watch: %d new item(s) on %s; %d to show in the %s%s" % (
                len(res["new"]), ", ".join(res["watched"]) or "nothing held", sum(len(v) for v in got.values()), where,
                "" if not res["errors"] else "; notes: " + "; ".join(res["errors"][:3])))
        return {"on": True, "by_page": got, "errors": res["errors"]}
    except Exception as e:
        if say:
            say("hold watch: could not check (%s: %s)" % (type(e).__name__, str(e)[:200]))
        return {"on": True, "by_page": {}, "errors": ["could not check: %s" % str(e)[:160]], "failed": True}


def summary(items):
    """One holding's items for the review: {"filings": [...], "serious": [...], "headlines": n}."""
    filings = [i for i in items if i["kind"] == "filing"]
    return {"filings": filings, "serious": [i for i in items if i.get("serious")],
            "headlines": sum(1 for i in items if i["kind"] != "filing"), "all": items}


def item_line(i):
    return "%s %s %s%s  %s" % ("!" if i.get("serious") else " ", i.get("day"), "8-K " if i["kind"] == "filing" else "",
                               i.get("title"), i.get("link") or "")


def show(sd):
    doc = load(sd)
    L = ["Hold watch (report only), last checked %s" % (doc.get("checked") or "never")]
    for pid, rec in sorted((doc.get("pages") or {}).items()):
        L.append("%s (%s, page %s): %d item(s)" % (rec.get("symbol"), rec.get("strategy"), pid, len(rec.get("items") or [])))
        for i in rec.get("items") or []:
            L.append("  " + item_line(i) + ("  [%s]" % i["why"] if i.get("why") else ""))
    for e in doc.get("errors") or []:
        L.append("note: %s" % e)
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description="New SEC filings and headlines on what is held (report only).")
    ap.add_argument("cmd", choices=("show", "check"))
    ap.add_argument("--state-dir", default=os.path.join(ROOT, "trader"))
    a = ap.parse_args(argv)
    if a.cmd == "show":
        print(show(a.state_dir))
        return 0
    try:
        with open(os.path.join(a.state_dir, "config.json"), encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (OSError, ValueError):
        cfg = {}
    res = check(a.state_dir, cfg)
    print("watched: %s" % (", ".join(res["watched"]) or "nothing held"))
    for i in res["new"]:
        print(item_line(i) + ("  [%s]" % i["why"] if i.get("why") else ""))
    if not res["new"]:
        print("nothing new")
    for e in res["errors"]:
        print("note: %s" % e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
