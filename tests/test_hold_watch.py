"""New SEC filings and headlines on what is held (trader-broker/hold_watch.py). REPORT ONLY.
A fake feed, no network, no order, no phone."""

import datetime
import json
import os
import re
import tempfile
import unittest

from _load import load, ROOT

hw = load("trader-broker", "hold_watch")
M = load("trader-broker", "messages")

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 11, 23, 22, 0, tzinfo=UTC)
CFG = {"hold_watch": {"enabled": True}, "sec_user_agent": "Test Person test@example.org"}
TICKERS = json.dumps({"0": {"cik_str": 1474432, "ticker": "P", "title": "Everpure"},
                      "1": {"cik_str": 37996, "ticker": "F", "title": "Ford Motor Co"}})


def rss(*items):
    """items: (title, RFC 822 date, guid)."""
    body = "".join("<item><title>%s</title><link>https://news.example/%s</link><guid>%s</guid>"
                   "<pubDate>%s</pubDate><description></description></item>" % (t, g, g, d) for t, d, g in items)
    return ("<?xml version='1.0'?><rss version='2.0'><channel>%s</channel></rss>" % body).encode()


def atom(*entries, form="8-K"):
    """entries: (items text, ISO time, accession)."""
    body = "".join("<entry><category term='%s'/><content type='text/xml'><accession-number>%s</accession-number>"
                   "<filing-type>%s</filing-type><form-name>Current report</form-name><items-desc>%s</items-desc>"
                   "</content><link href='https://www.sec.gov/%s'/><updated>%s</updated></entry>" % (form, a, form, it, a, t)
                   for it, t, a in entries)
    return ("<?xml version='1.0'?><feed xmlns='http://www.w3.org/2005/Atom'><company-info>"
            "<conformed-name>Everpure, Inc.</conformed-name></company-info>%s</feed>" % body).encode()


class Feed(object):
    """Stands in for trader-news's Fetcher: files by name, and a record of every request."""
    fake = True
    offline = None

    def __init__(self, files):
        self.files, self.calls = dict(files), []

    def get(self, url, name, sec=False):
        self.calls.append(name)
        if name not in self.files:
            raise hw.news().NewsError("no file " + name)
        return self.files[name]


def page(sym="P", strategy="drift", status="open", buy="2026-11-20", pid=None):
    return {"id": pid or "2026-11-20-%s-1" % sym, "symbol": sym, "strategy": strategy, "status": status,
            "buy_date": buy, "sell_rule": {"date": "2026-12-18"}}


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.dir, "state"))
        os.makedirs(os.path.join(self.dir, "universe"))
        with open(os.path.join(self.dir, "universe", "tradable.json"), "w") as fh:
            json.dump({"tickers": [{"symbol": "P", "name": "Everpure"}, {"symbol": "F", "name": "Ford Motor Co"}]}, fh)
        self.pages([page()])

    def pages(self, pages):
        with open(os.path.join(self.dir, "state", "holdings.json"), "w") as fh:
            json.dump({"pages": pages}, fh)

    def feed(self, headlines=(), filings=(), late=(), docs=None):
        files = {"company_tickers.json": TICKERS.encode(), "yahoo-P.xml": rss(*headlines),
                 "sec-8-K-0001474432.xml": atom(*filings), "sec-NT-0001474432.xml": atom(*late, form="NT 10-Q")}
        for acc, text in (docs or {}).items():
            files["sec-doc-%s.txt" % acc] = text.encode()
        return Feed(files)


class SeriousRule(unittest.TestCase):
    def head(self, title):
        return hw.serious({"kind": "headline", "title": title}, "P", "Everpure")

    def test_serious_filings_are_the_listed_items_only(self):
        self.assertEqual(hw.serious({"kind": "filing", "items": ["4.02", "9.01"]}, "P"),
                         (True, "SEC filing: past accounts no longer reliable"))
        for routine in (["2.02", "9.01"], ["5.02"], ["5.07"], ["1.01"], ["7.01", "8.01"], []):
            self.assertEqual(hw.serious({"kind": "filing", "items": routine}, "P"), (False, None), routine)

    def test_headlines_need_the_company_named_and_a_listed_word(self):
        for t in ("Everpure CEO to step down after 6 years", "Everpure cuts full-year revenue forecast",
                  "Everpure files for Chapter 11", "Everpure agrees to be acquired by Dell",
                  "SEC charges Everpure over accounting", "Trading halted in Everpure (P) shares",
                  "Everpure hit by ransomware attack", "Short seller report targets $P"):
            self.assertTrue(self.head(t)[0], t)
        for t in ("Everpure shares rise after strong quarter", "Everpure raises guidance",
                  "Everpure analyst upgrades to buy", "Dell CEO resigns",            # not about this company
                  "Market braces for recall data", "Everpure to present at conference"):
            self.assertFalse(self.head(t)[0], t)

    def test_the_company_is_named_by_name_first_word_or_ticker(self):
        self.assertTrue(hw.names_company("Ford recalls 1m trucks", "F", "Ford Motor"))
        self.assertTrue(hw.names_company("Shares of (F) fall", "F", "Ford Motor"))
        self.assertFalse(hw.names_company("F-150 sales", "F", None))                  # a one-letter ticker alone
        self.assertTrue(hw.names_company("AAPL slides", "AAPL", None))
        self.assertFalse(hw.names_company("General update on markets", "GM", "General Motors"))
        self.assertFalse(hw.names_company("aapl", "AAPL", None))                      # capitals only


class Check(Base):
    def test_off_by_default_nothing_is_fetched(self):
        f = self.feed()
        self.assertEqual(hw.run(self.dir, {}, "review", now=NOW, fetcher=f), {"on": False, "by_page": {}, "errors": []})
        self.assertEqual(f.calls, [])

    def test_items_since_the_buy_day_are_kept_once_and_shown_once_in_each_place(self):
        f = self.feed(headlines=[("Everpure shares rise", "Mon, 23 Nov 2026 14:00:00 GMT", "a"),
                                 ("Everpure CFO resigns", "Fri, 20 Nov 2026 16:00:00 GMT", "b"),
                                 ("Old story on Everpure fraud", "Tue, 17 Nov 2026 14:00:00 GMT", "c")],
                      filings=[("Items 5.02 and 9.01", "2026-11-21T08:00:00-05:00", "0001-26-1"),
                               ("Item 4.02", "2026-11-22T17:00:00-05:00", "0001-26-2")],
                      docs={"0001-26-1": "Item 5.02 Election of Directors. Jo Bloggs joined the Board. SIGNATURES"})
        res = hw.check(self.dir, CFG, now=NOW, fetcher=f)
        self.assertEqual(res["errors"], [])
        got = {i["title"][:20]: i["serious"] for i in res["new"]}
        self.assertEqual(len(res["new"]), 4)                     # the 17 Nov story is before the buy day
        self.assertTrue(got["Everpure CFO resigns"])
        self.assertFalse(got["Everpure shares rise"])
        self.assertEqual(hw.check(self.dir, CFG, now=NOW, fetcher=f)["new"], [])     # never twice
        brief = hw.take(self.dir, "briefing", serious_only=True)
        self.assertEqual(sorted(i["why"] for i in brief["2026-11-20-P-1"]),
                         ["SEC filing: past accounts no longer reliable", "headline: CEO or CFO leaving"])
        self.assertEqual(hw.take(self.dir, "briefing", serious_only=True), {})
        self.assertEqual(len(hw.briefed(self.dir)), 2)
        self.assertEqual(hw.briefed(self.dir, since="2026-11-24"), [])
        review = hw.take(self.dir, "review")
        self.assertEqual(len(review["2026-11-20-P-1"]), 4)       # the review shows everything, once
        self.assertEqual(hw.take(self.dir, "review"), {})
        with open(hw.log_path(self.dir)) as fh:
            self.assertEqual(len(fh.readlines()), 4)

    def test_a_notable_headline_goes_to_the_review_not_the_briefing(self):
        f = self.feed(headlines=[("Everpure gives weak outlook for next quarter", "Mon 23 Nov 2026 14:00:00 GMT", "w")])
        f.files["yahoo-P.xml"] = rss(("Everpure gives weak outlook for next quarter", "Mon, 23 Nov 2026 14:00:00 GMT", "w"))
        res = hw.check(self.dir, CFG, now=NOW, fetcher=f)
        self.assertEqual([(i["serious"], i["notable"], i["why"]) for i in res["new"]],
                         [(False, True, "headline: weak forecast or results")])
        self.assertEqual(hw.take(self.dir, "briefing", serious_only=True), {})
        review = hw.take(self.dir, "review")["2026-11-20-P-1"]
        w = M.watch_words(hw.summary(review))
        self.assertIn("\U0001f538 Headline (Mon 23 Nov): Everpure gives weak outlook for next quarter", w)
        self.assertNotIn("other headline", w)

    def test_the_briefing_takes_at_most_three_serious_items_a_holding(self):
        f = self.feed(filings=[("Item 1.05", "2026-11-2%dT10:00:00-05:00" % d, "acc-%d" % d) for d in range(0, 4)]
                      + [("Item 2.06", "2026-11-23T10:00:00-05:00", "acc-9")])
        hw.check(self.dir, CFG, now=NOW, fetcher=f)
        self.assertEqual(len(hw.take(self.dir, "briefing", serious_only=True)["2026-11-20-P-1"]), 3)
        self.assertEqual(len(hw.take(self.dir, "briefing", serious_only=True)["2026-11-20-P-1"]), 2)   # 5 in all

    def test_only_held_drift_and_news_pages_are_watched(self):
        self.pages([page(), page("F", "news", pid="n1"), page("AAPL", "intraday", pid="i1"),
                    page("MSFT", status="closed", pid="c1"), page("KO", status="waiting", pid="w1")])
        f = self.feed()
        res = hw.check(self.dir, CFG, now=NOW, fetcher=f)
        self.assertEqual(res["watched"], ["P", "F"])
        self.assertIn("headlines for F: no file yahoo-F.xml", res["errors"])

    def test_without_an_sec_name_the_headlines_still_run(self):
        f = self.feed(headlines=[("Everpure news", "Mon, 23 Nov 2026 14:00:00 GMT", "a")])
        f.fake = False
        res = hw.check(self.dir, {"hold_watch": {"enabled": True}}, now=NOW, fetcher=f, environ={})
        self.assertEqual(len(res["new"]), 1)
        self.assertIn("SEC filings skipped", res["errors"][0])
        self.assertNotIn("company_tickers.json", f.calls)

    def test_a_feed_failure_is_a_note_never_a_crash(self):
        class Broken(Feed):
            def get(self, url, name, sec=False):
                raise RuntimeError("boom")
        out = hw.run(self.dir, CFG, "review", now=NOW, fetcher=Broken({}))
        self.assertTrue(out["on"])
        self.assertEqual(out["by_page"], {})
        self.assertTrue(out["errors"])

    def test_nothing_held_means_nothing_fetched(self):
        self.pages([])
        f = self.feed()
        self.assertEqual(hw.run(self.dir, CFG, "review", now=NOW, fetcher=f)["by_page"], {})
        self.assertEqual(f.calls, [])

    def test_it_never_changes_the_holdings_record(self):
        path = os.path.join(self.dir, "state", "holdings.json")
        with open(path) as fh:
            before = fh.read()
        hw.run(self.dir, CFG, "review", now=NOW, fetcher=self.feed(filings=[("Item 1.03", "2026-11-23T10:00:00Z", "z")]))
        with open(path) as fh:
            self.assertEqual(fh.read(), before)


class FilingsAndLateNotices(Base):
    def test_late_filing_notices_and_the_filings_own_words(self):
        docs = {"0001-26-5": "Item 5.02 Departure of Directors. Jane Doe, the Company's Chief Financial Officer, "
                             "resigned on November 21, 2026. SIGNATURES",
                "0001-26-6": "Item 8.01 Other Events. On November 22, 2026 the Company entered into an underwriting "
                             "agreement to sell 5,000,000 shares of common stock in a public offering. SIGNATURES"}
        f = self.feed(filings=[("Items 5.02 and 9.01", "2026-11-21T16:00:00-05:00", "0001-26-5"),
                               ("Items 8.01 and 9.01", "2026-11-22T16:00:00-05:00", "0001-26-6"),
                               ("Item 1.02", "2026-11-23T08:00:00-05:00", "0001-26-7")],
                      late=[("", "2026-11-23T09:00:00-05:00", "0001-26-8")], docs=docs)
        res = hw.check(self.dir, CFG, now=NOW, fetcher=f)
        self.assertEqual(res["errors"], [])
        self.assertEqual(sorted(i["why"] for i in res["new"]), [
            "SEC filing: a late-filing notice (it cannot file its accounts on time)",
            "SEC filing: a major agreement ended", "SEC filing: a new share offering",
            "SEC filing: its CEO or CFO is leaving"])
        self.assertNotIn("sec-doc-0001-26-7.txt", f.calls)          # 1.02 is serious by its number alone

    def test_an_unreadable_filing_is_a_note_and_not_serious(self):
        f = self.feed(filings=[("Item 5.02", "2026-11-21T16:00:00-05:00", "0001-26-9")])
        res = hw.check(self.dir, CFG, now=NOW, fetcher=f)
        self.assertFalse(res["new"][0]["serious"])
        self.assertIs(res["new"][0]["text_read"], False)
        self.assertIn("could not read", res["errors"][0])


def fixture(name):
    with open(os.path.join(ROOT, "tests", "fixtures", "hold-watch", name + ".txt"), encoding="utf-8") as fh:
        return fh.read()


class PastExamples(unittest.TestCase):
    """The owner's check (22 Nov 2026): known past events, offline. The filings are real SEC text (Intel, Boeing);
    the headlines are real past headlines, worded as published or very close to it."""

    HEADLINES = [   # (ticker, name, headline, flagged?)
        ("INTC", "Intel", "Intel CEO Pat Gelsinger ousted as board loses confidence in turnaround plan", True),
        ("NKE", "Nike", "Nike CEO John Donahoe to retire, Elliott Hill to return as chief executive", True),
        ("SBUX", "Starbucks", "Starbucks replaces CEO Laxman Narasimhan with Chipotle's Brian Niccol", True),
        ("INTC", "Intel", "Qualcomm approached Intel about a takeover in recent days", True),
        ("K", "Kellanova", "Mars to buy Kellanova in $36 billion deal", True),
        ("ANSS", "ANSYS", "Synopsys to buy Ansys for $35 billion", True),
        ("SNPS", "Synopsys", "Synopsys to buy Ansys for $35 billion", False),          # the buyer, not the target
        ("NKE", "Nike", "Nike pulls full-year guidance, postpones investor day", True),
        ("WBA", "Walgreens Boots Alliance", "Walgreens cuts annual profit forecast as consumers pull back", True),
        ("INTC", "Intel", "Intel forecasts quarterly revenue below estimates", False),  # weak, but not a cut
        ("SMCI", "Super Micro Computer", "INVESTOR ALERT: Pomerantz Law Firm Investigates Claims On Behalf of "
                                         "Investors of Super Micro Computer, Inc. - SMCI", False),
        ("SMCI", "Super Micro Computer", "ROSEN, A TOP RANKED LAW FIRM, Encourages Super Micro Computer, Inc. "
                                         "Investors to Secure Counsel Before Important Deadline in Securities Class Action - SMCI", False),
        ("SMCI", "Super Micro Computer", "Levi & Korsinsky Reminds Super Micro Computer Shareholders of a Lead "
                                         "Plaintiff Deadline in Securities Fraud Lawsuit", False),
        ("SMCI", "Super Micro Computer", "Super Micro Computer faces Justice Department probe after short seller report", True),
        ("V", "Visa", "Justice Department sues Visa, alleging debit card monopoly", True),
        ("BA", "Boeing", "Boeing raises $21 billion in one of the largest-ever share sales", True),
        ("SMCI", "Super Micro Computer", "Super Micro announces $700 million convertible notes offering", False),
        ("NKE", "Nike", "Nike raises full-year guidance on strong demand", False),
        ("INTC", "Intel", "Is it time to buy Intel stock?", False),
        ("INTC", "Intel", "Analyst downgrades Intel to sell", False),
    ]

    def test_weak_forecast_headlines_are_notable_not_serious(self):
        for sym, name, title in (("INTC", "Intel", "Intel forecasts quarterly revenue below estimates"),
                                 ("NKE", "Nike", "Nike misses estimates as China sales slump"),
                                 ("TGT", "Target", "Target gives disappointing holiday-quarter outlook"),
                                 ("AAPL", "Apple", "Apple revenue falls short of Wall Street expectations")):
            item = {"kind": "headline", "title": title}
            self.assertEqual(hw.serious(item, sym, name), (False, None), title)
            self.assertEqual(hw.notable(item, sym, name), (True, "headline: weak forecast or results"), title)
        for sym, name, title in (("NKE", "Nike", "Nike raises full-year guidance on strong demand"),
                                 ("INTC", "Intel", "AMD forecasts revenue below estimates"),           # another company
                                 ("SMCI", "Super Micro Computer", "Rosen Law Firm: Super Micro Computer results "
                                                                  "below estimates, investors who lost money"),
                                 ("INTC", "Intel", "Intel revenue beats estimates")):
            self.assertFalse(hw.notable({"kind": "headline", "title": title}, sym, name)[0], title)
        self.assertFalse(hw.notable({"kind": "filing", "items": ["2.02"]}, "INTC", "Intel")[0])

    def test_headlines(self):
        for sym, name, title, want in self.HEADLINES:
            self.assertEqual(hw.headline_rule(title, sym, name)[0], want, title)
        self.assertEqual(hw.headline_rule(self.HEADLINES[10][2], "SMCI", "Super Micro Computer"),
                         (False, "a shareholder law-firm notice"))

    def test_real_filings(self):
        self.assertEqual(hw.filing_text_rule(fixture("intel-0000050863-24-000173"), ["5.02", "7.01", "9.01"], "Intel"),
                         ["its CEO or CFO is leaving"])                             # Gelsinger, 1 Dec 2024
        self.assertEqual(hw.filing_text_rule(fixture("intel-0000050863-24-000180"), ["5.02", "7.01", "9.01"], "Intel"),
                         [])                                                        # two new directors (a bio says "retirement")
        self.assertEqual(hw.filing_text_rule(fixture("boeing-0000012927-24-000064"), ["5.02"], "Boeing"),
                         [])                                                        # the head of a division, not Boeing's CEO
        self.assertEqual(hw.filing_text_rule(fixture("boeing-0001193125-24-247611"), ["1.01", "7.01", "8.01", "9.01"], "Boeing"),
                         ["a new share offering"])                                  # the October 2024 share sale
        self.assertEqual(hw.filing_text_rule(fixture("boeing-0000012927-24-000068"), ["1.01", "2.03", "9.01"], "Boeing"),
                         [])                                                        # a loan agreement

    def test_a_real_late_filing_notice(self):
        with open(os.path.join(ROOT, "tests", "fixtures", "hold-watch", "smci-nt-10k-2024-08-30.xml"), "rb") as fh:
            items = hw.news().parse_sec_atom(fh.read(), "SMCI")                  # Super Micro, 30 Aug 2024
        self.assertEqual([i["form"] for i in items], ["NT 10-K"])
        self.assertEqual(hw.serious(items[0], "SMCI", "Super Micro Computer"),
                         (True, "SEC filing: a late-filing notice (it cannot file its accounts on time)"))

    def test_role_and_sentence_edges(self):
        rule = hw.filing_text_rule
        self.assertTrue(rule("Item 5.02 John Smith, Chief Financial Officer of The Boeing Company, will resign.", ["5.02"], "Boeing"))
        self.assertTrue(rule("Item 5.02 The Chief Executive Officer of Acme Inc. notified the Board of his decision to retire.",
                             ["5.02"], "Acme"))
        self.assertFalse(rule("Item 5.02 Jane Doe, President and Chief Executive Officer of Acme Cloud, resigned.", ["5.02"], "Acme"))
        self.assertFalse(rule("Item 8.01 The Company sold $500 million of 5% notes under an underwriting agreement.", ["8.01"], "Acme"))
        self.assertFalse(rule("Item 5.02 Jane Doe, the Company's CFO, resigned.", ["1.01"], "Acme"))     # not a 5.02


class NoOrders(unittest.TestCase):
    def test_the_source_has_no_broker_and_no_order_code(self):
        with open(os.path.join(ROOT, "trader-broker", "hold_watch.py")) as fh:
            src = fh.read()
        for rx in (r"t212", r"\bplace\(", r"\bcancel\w*\(", r"\bpush\(", r"\bPhone\b",
                   r"holdings\.save|H\.save|_hold\(\)\.save"):
            self.assertIsNone(re.search(rx, src), rx)


class Words(unittest.TestCase):
    def setUp(self):
        M.set_names({"P": "Everpure"})

    def test_the_briefing_lines_show_serious_items_only(self):
        self.assertEqual(M.hold_watch_briefing({}), [])
        self.assertEqual(M.hold_watch_briefing({"x": [{"symbol": "P", "kind": "headline", "title": "fine", "serious": False}]}), [])
        out = M.join(M.hold_watch_briefing({"x": [{"symbol": "P", "kind": "filing", "items": ["4.02", "9.01"],
                                                   "day": "2026-11-22", "serious": True}]}))
        self.assertIn("Everpure (P): SEC filing (Sun 22 Nov): past accounts no longer reliable", out)
        self.assertIn("nothing is sold or asked", out)
        self.assertNotIn("8-K", out)

    def test_the_review_words(self):
        items = [{"kind": "filing", "items": ["5.02"], "day": "2026-11-21", "serious": False},
                 {"kind": "headline", "title": "Everpure CFO resigns", "day": "2026-11-20", "serious": True},
                 {"kind": "headline", "title": "a", "day": "2026-11-20", "serious": False},
                 {"kind": "headline", "title": "b <x>", "day": "2026-11-20", "serious": False}]
        w = M.watch_words(hw.summary(items))
        self.assertEqual(w, "\n    ↳ New since the last review: \U0001f6a9 Headline (Fri 20 Nov): Everpure CFO resigns; "
                            "SEC filing (Sat 21 Nov): a director or senior manager joining or leaving; 2 other headlines")
        self.assertEqual(M.watch_words(None), "")
        self.assertIn("&lt;x&gt;", M.watch_item(items[3]))


class Review(unittest.TestCase):
    def test_the_review_row_gets_its_own_pages_items_only(self):
        review = load("trader-broker", "review")
        items = [{"kind": "filing", "items": ["1.03"], "day": "2026-11-21", "serious": True}]
        w = review._watch({"p1": items}, {"id": "p1"})
        self.assertEqual((len(w["serious"]), len(w["filings"]), w["headlines"]), (1, 1, 0))
        self.assertIsNone(review._watch({"p1": items}, {"id": "p2"}))
        self.assertIsNone(review._watch(None, None))


if __name__ == "__main__":
    unittest.main()
