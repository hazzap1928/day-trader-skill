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

The "serious" rule (version 2, the owner's of 22 Nov 2026; changing it starts version 3):
  - SEC filings: a late-filing notice (NT 10-K, NT 10-Q); an 8-K with one of SERIOUS_8K's items (a major
    agreement ended, bankruptcy, a cyber attack, a lender demanding early repayment, a write-down, a delisting
    notice, new shares sold privately, a new auditor, past accounts no longer reliable, a change of control);
    an 8-K whose own words (items 5.02, 1.01, 8.01, read from the SEC) show the company's CEO or CFO leaving
    or a new share offering. Other director or officer changes are shown in the evening review only.
  - Headlines: the TITLE names the company (its name, its first word when distinctive, or its ticker in
    capitals) AND it matches SERIOUS_WORDS (bankruptcy, a halt, fraud or a restatement, a short-seller report,
    the CEO or CFO leaving, a forecast cut, a takeover, a new share offering, a recall or hack, a regulator
    saying no), or it names the company as a takeover target, or the SEC, the Justice Department, the FTC or
    prosecutors are named as acting. Shareholder law-firm notices ("investigates claims", class actions)
    never count unless one of those authorities is named.
  Routine 8-Ks and other headlines are shown in the evening review only.
  "Notable" (version 1, the owner's of 22 Nov 2026): a headline naming the company with a weak forecast or results
  below estimates is listed by name in the evening review, never in the briefing.

Files (trader/state/holdwatch/): state.json (the SEC ticker list, every item found, what each place has
shown), items.jsonl (every new item once, for judging the rule later).

    python3 trader-broker/hold_watch.py show            # what has been found for each holding
    python3 trader-broker/hold_watch.py check           # look now (network: Yahoo and SEC), print what is new
    python3 trader-broker/hold_watch.py briefed --since 2026-11-23   # everything that reached the briefing

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

RULE_VERSION = 2              # 2 (22 Nov 2026): the owner's changes to the first draft
MAX_BRIEFING = 3              # serious items a holding in one briefing
MAX_KEEP = 200                # items kept a holding
CIK_DAYS = 7                  # the SEC ticker list is fetched again after this many days
SHOW_PLACES = ("review", "briefing")
WATCHED = ("open", "sell_asked", "selling", "mismatch")       # bought and held (not "waiting", not "closed")

SERIOUS_8K = {
    "1.02": "a major agreement ended",
    "1.03": "bankruptcy",
    "1.05": "a cyber attack",
    "2.04": "a lender demanding early repayment",
    "2.06": "a write-down of what its assets are worth",
    "3.01": "a delisting notice",
    "3.02": "new shares sold privately",
    "4.01": "a change of auditor",
    "4.02": "past accounts no longer reliable",
    "5.01": "a change of control",
}
LATE_FILING = "a late-filing notice (it cannot file its accounts on time)"
READ_ITEMS = ("5.02", "1.01", "8.01")      # 8-Ks whose text is read: a CEO or CFO leaving, a new share offering

# ---- the filing's own words (items 5.02, 1.01, 8.01), one sentence at a time
_ROLE = re.compile(r"\b(?:chief executive officer|chief financial officer|principal executive officer|"
                   r"principal financial officer|CEO|CFO)\b", re.I)
_GOING = re.compile(r"\b(?:resign(?:ed|s|ation)?|(?:will|to|decided to|intends? to|plans? to|decision to) retire|"
                    r"retire[sd]? from|retirement from|step(?:s|ped)? down|stepping down|terminat(?:ed|ion of)|"
                    r"depart(?:ed|ure|s)? from|will depart|will leave|leave the company|cease[sd]? to serve|"
                    r"no longer serv\w+|separation from)\b", re.I)
_OFFER = re.compile(r"\b(?:underwriting agreement|underwritten (?:public )?offering|public offering|registered direct "
                    r"offering|at[- ]the[- ]market|equity distribution agreement|sales agreement)\b", re.I)
_SHARES = re.compile(r"\b(?:shares of (?:its |the company.s )?common stock|common shares|ordinary shares|"
                     r"depositary shares)\b", re.I)

# ---- headlines
SERIOUS_WORDS = [
    ("bankruptcy", r"\bbankrupt\w*|\bchapter 11\b|\bgoing concern\b|\binsolven\w*"),
    ("delisting or halt", r"\bdelist\w*|\btrading (?:halt|pause|suspen)\w*|\bhalt(?:s|ed)? trading\b|\bshares? halted\b"),
    ("fraud or accounting", r"\bfraud\w*|\brestate(?:s|d|ment)?\b|\baccounting (?:irregularit\w*|probe|issues?|errors?)\b"),
    ("short seller report", r"\bshort[- ]sell(?:er|ers|ing) report\b|\bshort report\b"),
    ("CEO or CFO leaving", r"\b(?:CEO|CFO|chief executive|chief financial officer)\b[^.]{0,40}\b(?:resign\w*|retir\w*|steps? down|stepping down|ousted|fired|departs?|departure|exits?|out|leav(?:es|ing)|to leave)\b"
                           r"|\b(?:replaces?|replaced|ousts?|ousted|fires?|fired|names? (?:a )?new|appoints? (?:a )?new|search(?:es|ing)? for (?:a )?new)\s+(?:its\s+)?(?:interim\s+)?(?:CEO|CFO|chief executive|chief financial officer)\b"),
    ("forecast cut", r"\b(?:cuts?|lowers?|slashes|withdraws?|pulls?|suspends?)\b[^.]{0,30}\b(?:guidance|forecast|outlook)\b|\bprofit warning\b|\bwarns on (?:profit|sales|revenue)\b"),
    ("takeover", r"\b(?:to be acquired|agrees? to be (?:bought|acquired)|takeover (?:bid|offer|approach)|buyout (?:offer|bid)|hostile bid)\b"),
    ("new share offering", r"\b(?:public|secondary|follow-on|equity|common stock|share) offering\b|\bshare sales?\b"),
    ("recall or hack", r"\brecall\w*\b|\bdata breach\b|\bcyber ?attack\w*|\bransomware\b|\bhack(?:ed|ers?)\b"),
    ("regulator says no", r"\bFDA (?:rejects?|declines?)\b|\bcomplete response letter\b|\bclinical hold\b|\bblocks? (?:the )?(?:deal|merger)\b"),
]
_SERIOUS_RX = [(label, re.compile(rx, re.I)) for label, rx in SERIOUS_WORDS]

# "notable" (the owner's, 22 Nov 2026; version 1): a weak forecast or results below estimates. Listed by name in
# the evening review only, never in the morning briefing. The title must name the company, as for serious.
NOTABLE_VERSION = 1
NOTABLE_WORDS = [
    ("weak forecast or results",
     r"\b(?:forecasts?|guidance|outlook|revenue|sales|profit|earnings|results|quarter)\b[^.]{0,40}?\b(?:below|miss(?:es|ed)?|"
     r"short of|falls? short|disappoint\w*|weak(?:er)?|soft(?:er)?|light|tepid|underwhelm\w*|lower than expected)\b"
     r"|\b(?:weak|soft|disappointing|tepid|downbeat|gloomy|cautious)\s+(?:[\w-]+\s+)?(?:forecast|guidance|outlook|results|quarter)\b"
     r"|\bmiss(?:es|ed)? (?:estimates|expectations|forecasts)\b"),
]
_NOTABLE_RX = [(label, re.compile(rx, re.I)) for label, rx in NOTABLE_WORDS]


def notable(item, sym, name=None):
    """(True, why) for a headline the owner wants in the evening review only (a weak forecast, results below
    estimates), when it is not already serious; else (False, None)."""
    if item.get("kind") == "filing":
        return False, None
    title = item.get("title") or ""
    if not names_company(title, sym, name) or (_LAW_FIRM.search(title) and not _GOV.search(title)):
        return False, None
    for label, rx in _NOTABLE_RX:
        if rx.search(title):
            return True, "headline: " + label
    return False, None
# the SEC, the Justice Department, the FTC or prosecutors acting (named as doing it). Case matters: "sec" is not the SEC
_AGENCY = r"(?:SEC|Securities and Exchange Commission|DOJ|Justice Department|Department of Justice|FTC|Federal Trade Commission|federal prosecutors|prosecutors)"
_GOV = re.compile(r"\b%s\b[^.]{0,40}?\b(?:charges?|charged|sues|sued|opens?|launch(?:es|ed)?|probe\w*|investigat\w*|subpoena\w*|raid\w*|indict\w*|fines?|fined)\b"
                  r"|\b(?:probe|investigation|subpoena|charges?|lawsuit) (?:by|from) (?:the )?%s\b|\bindict(?:s|ed|ment)\b" % (_AGENCY, _AGENCY))
# shareholder law-firm notices ("INVESTOR ALERT: ... Law Firm Investigates Claims ...", class-action deadlines)
_LAW_FIRM = re.compile(
    r"\blaw firm\b|\bclass action\b|\bsecurities (?:fraud )?(?:lawsuit|litigation|class)\b|\binvestors? (?:who|that) "
    r"(?:lost|purchased|bought)\b|\bon behalf of (?:investors|shareholders)\b|\blead plaintiff\b|\b(?:shareholder|investor) "
    r"(?:alert|rights|notice|reminder)\b|\binvestigates? (?:claims|potential)\b|\bsecure counsel\b|\b(?:Rosen|Pomerantz|"
    r"Levi & Korsinsky|Bragar Eagel|Faruqi|Glancy|Robbins|Kessler Topaz|Bernstein Liebhard|Schall|Gross Law|Hagens Berman|"
    r"Kirby McInerney|Johnson Fistel|Bronstein|Rigrodsky|Halper Sadeh|Kahn Swick|Block & Leviton|Holzer|Portnoy|Frank R\. Cruz)\b",
    re.I)
_TAKEOVER_VERB = (r"\b(?:to buy|to acquire|agrees? to (?:buy|acquire)|buys|acquires|bids? for|offers? to buy|takeover of|"
                  r"approach(?:ed|es)?|nears? (?:a )?deal (?:for|to buy)|in talks to (?:buy|acquire))\b[^.]{0,30}?")
_CLICKBAIT = re.compile(r"\b(?:time to buy|should you buy|stocks? to buy|reasons? to buy|good (?:stock )?to buy|buy now|"
                        r"worth buying|best \w+ (?:stocks? )?to buy)\b|\?\s*$", re.I)

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


def _company_rx(sym, name):
    alts = [r"\b%s\b" % re.escape(n).replace(r"\ ", r"[\s-]+") for n in _name_forms(name)]
    sym = str(sym or "").upper()
    if len(sym) >= 3:
        alts.append(r"(?<![A-Za-z])%s(?![A-Za-z])" % re.escape(sym))
    alts.append(r"\(%s\)" % re.escape(sym))
    return "(?:%s)" % "|".join(alts)


def headline_rule(title, sym, name=None):
    """(True, why) when a headline meets the serious rule, else (False, None or the reason it was let go)."""
    title = str(title or "")
    if not names_company(title, sym, name):
        return False, None
    gov = _GOV.search(title)
    if gov is None and re.search(r"\b%s\b" % _AGENCY, title) is None and _LAW_FIRM.search(title):
        return False, "a shareholder law-firm notice"
    if gov is not None:
        return True, "headline: charges or a probe by the authorities"
    for label, rx in _SERIOUS_RX:
        if rx.search(title):
            return True, "headline: " + label
    if not _CLICKBAIT.search(title) and re.search(_TAKEOVER_VERB + r"\b" + _company_rx(sym, name), title, re.I):
        return True, "headline: takeover"
    return False, None


def _sentences(text):
    # a full stop after Inc, Corp, Co, Mr, Ms, Mrs, Dr, Jr, No or a single letter (N.A.) does not end a sentence
    split = r"(?<!\bInc\.)(?<!\bCorp\.)(?<!\bCo\.)(?<!\bMr\.)(?<!\bMs\.)(?<!\bMrs\.)(?<!\bDr\.)(?<!\bJr\.)" \
            r"(?<!\bNo\.)(?<!\b[A-Z]\.)(?<=[.;])\s+"
    return [x for x in re.split(split, re.sub(r"\s+", " ", text or "")) if x]


def section(body, item):
    """An 8-K's "Item 5.02" part, up to the next item or the signatures; "" when it has none."""
    m = re.search(r"\bItem\s+%s\b" % re.escape(item), body or "", re.I)
    if not m:
        return ""
    end = re.search(r"\bItem\s+\d\.\d\d\b|\bSIGNATURES?\b", body[m.end():])
    return body[m.start():m.end() + (end.start() if end else 6000)]


def _company_role(sentence, m, name):
    """A role that is the company's own CEO or CFO, not "Chief Executive Officer of the Intel Products business"."""
    after = sentence[m.end():m.end() + 60]
    of = re.match(r"\s*(?:\([^)]{1,12}\)\s*)?,?\s*of\s+(.{0,40})", after)
    if not of:
        return True
    rest = of.group(1)
    if re.match(r"(?:the )?(?:Company|Registrant|Corporation)\b", rest, re.I):
        return True
    # "of Intel Corporation" is the company; "of Boeing Defense, Space & Security" is a division
    tail = r"(?:,?\s+(?:Company|Corporation|Corp|Inc|Co|plc|Holdings?|Group)\b\.?)?(?!\s+[A-Z&])"
    rest = re.sub(r"^the\s+", "", rest, flags=re.I)
    return any(re.match(re.escape(n) + tail, rest) or re.match(re.escape(n) + tail, rest, re.I)
               and not re.match(re.escape(n) + r"\s+[A-Z&]", rest, re.I) for n in _name_forms(name))


def filing_text_rule(body, items, name=None):
    """What a filing's own words show: ["its CEO or CFO is leaving", "a new share offering"] (or [])."""
    out = []
    if "5.02" in items:
        for s in _sentences(section(body, "5.02")):
            if _GOING.search(s) and any(_company_role(s, m, name) for m in _ROLE.finditer(s)):
                out.append("its CEO or CFO is leaving")
                break
    for it in ("1.01", "8.01"):
        if it in items and any(_OFFER.search(s) and _SHARES.search(s) for s in _sentences(section(body, it))):
            out.append("a new share offering")
            break
    return out


def serious(item, sym, name=None):
    """(True, why) when the item meets the serious rule (version RULE_VERSION), else (False, None)."""
    if item.get("kind") == "filing":
        hit = []
        if str(item.get("form") or "").upper().startswith("NT "):
            hit.append(LATE_FILING)
        hit += [SERIOUS_8K[i] for i in item.get("items") or [] if i in SERIOUS_8K]
        hit += [w for w in item.get("text_why") or [] if w not in hit]
        return (True, "SEC filing: " + ", ".join(hit)) if hit else (False, None)
    ok, why = headline_rule(item.get("title"), sym, name)
    return (True, why) if ok else (False, None)


def doc_url(cik, accession):
    return "https://www.sec.gov/Archives/edgar/data/%d/%s/%s.txt" % (int(cik), accession.replace("-", ""), accession)


def read_filing(fetcher, cik, accession):
    """The filing's text with the markup taken out (the SEC's full submission file)."""
    import html as _html
    raw = fetcher.get(doc_url(cik, accession), "sec-doc-%s.txt" % accession, sec=True)
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", text)))


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
                for form, keep in (("8-K", "8-K"), ("NT", "NT ")):
                    try:
                        data = fetcher.get(N.SEC_FEED % (cik, form, 10), "sec-%s-%s.xml" % (form, cik), sec=True)
                        items += [dict(i, cik=cik) for i in N.parse_sec_atom(data, sym)
                                  if str(i.get("form") or "").upper().startswith(keep)]
                    except Exception as e:
                        out["errors"].append("SEC %s filings for %s: %s" % (form, sym, str(e)[:160]))
        items = N.dedupe(N.tag(items, {sym: nm.get(sym)}))
        rec = doc["pages"].setdefault(p["id"], {"symbol": sym, "strategy": _hold().strategy_of(p), "items": []})
        have = {i["id"] for i in rec["items"]}
        for it in sorted(items, key=lambda i: i["time"] or now):
            day = _ny_day(it["time"])
            if it["id"] in have or day is None or (buy and day < buy) or it["time"] > now + datetime.timedelta(minutes=10):
                continue
            if it["kind"] == "filing" and set(it.get("items") or []) & set(READ_ITEMS):
                try:
                    body = read_filing(fetcher, it["cik"], it["id"].split(":", 1)[1])
                    it["text_why"] = filing_text_rule(body, it.get("items") or [], nm.get(sym))
                    it["text_read"] = True
                except Exception as e:
                    it["text_read"] = False
                    out["errors"].append("could not read the %s filing of %s: %s" % (it.get("day") or day, sym, str(e)[:120]))
            hit, why = serious(it, sym, nm.get(sym))
            note, note_why = (False, None) if hit else notable(it, sym, nm.get(sym))
            row = {"id": it["id"], "page": p["id"], "symbol": sym, "strategy": rec["strategy"], "kind": it["kind"],
                   "form": it.get("form"), "items": it.get("items") or [], "title": it.get("title") or "",
                   "link": it.get("link") or "", "time": it["time"].isoformat(), "day": day.isoformat(),
                   "tags": it.get("tags") or [], "serious": hit, "why": why or note_why, "rule": RULE_VERSION,
                   "notable": note, "notable_rule": NOTABLE_VERSION,
                   "text_read": it.get("text_read"),
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
            "notable": [i for i in items if i.get("notable") and not i.get("serious")],
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


def briefed(sd, since=None):
    """Every item that reached the briefing (found on or after `since`, a date), oldest first."""
    rows = []
    for rec in (load(sd).get("pages") or {}).values():
        for i in rec.get("items") or []:
            if "briefing" in (i.get("shown") or []) and (since is None or str(i.get("found_at"))[:10] >= str(since)):
                rows.append(i)
    return sorted(rows, key=lambda i: (i.get("found_at") or "", i.get("symbol") or ""))


def main(argv=None):
    ap = argparse.ArgumentParser(description="New SEC filings and headlines on what is held (report only).")
    ap.add_argument("cmd", choices=("show", "check", "briefed"))
    ap.add_argument("--state-dir", default=os.path.join(ROOT, "trader"))
    ap.add_argument("--since", help="briefed: from this date (YYYY-MM-DD)")
    a = ap.parse_args(argv)
    if a.cmd == "show":
        print(show(a.state_dir))
        return 0
    if a.cmd == "briefed":
        rows = briefed(a.state_dir, a.since)
        for i in rows:
            print("%s found %s  %-6s %s  [%s]  %s" % (i.get("day"), str(i.get("found_at"))[:16], i.get("symbol"),
                                                    i.get("title"), i.get("why"), i.get("link") or ""))
        print("%d item(s) reached the briefing%s" % (len(rows), " since %s" % a.since if a.since else ""))
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
