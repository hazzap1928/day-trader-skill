"""messages.py: every word sent to the phone, plain English, Telegram HTML; phone.py: HTML, fallback, splitting.
Offline: a fake Telegram, never the network."""

import datetime
import email.message
import io
import json
import os
import re
import shutil
import tempfile
import unittest
import urllib.error

from _load import load
import test_phone

phone = load("trader-broker", "phone")
M = phone.messages()

JARGON = re.compile(r"\b(\d+(\.\d+)?R|Time Machine|EVIDENCE MODE|SHADOW|end-of-day close|at market|order id|"
                    r"traceId|_US_EQ|target|reward:risk)\b")


def well_formed(text):
    """Every <b>/<i> opened is closed, and no other tag appears: Telegram would reject anything else."""
    stack = []
    for m in re.finditer(r"<(/?)([a-z]+)>", text):
        close, tag = m.group(1), m.group(2)
        if tag not in ("b", "i"):
            return False
        if close:
            if not stack or stack.pop() != tag:
                return False
        else:
            stack.append(tag)
    return not stack and "<" not in re.sub(r"</?(b|i)>", "", text)


class Names(unittest.TestCase):
    def setUp(self):
        M.set_names({"ADBE": "Adobe", "CHTR": "Charter Communications", "AT&T": "AT&T <Wireless>"})

    def tearDown(self):
        M.use(None)

    def test_company_name_with_the_ticker_in_brackets(self):
        self.assertEqual(M.company("ADBE_US_EQ"), "Adobe (ADBE)")
        self.assertEqual(M.company("adbe"), "Adobe (ADBE)")
        self.assertEqual(M.company("ZZZ"), "ZZZ")                  # unknown: the ticker alone

    def test_names_come_from_the_universe_file_with_suffixes_off(self):
        d = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(d, "universe"))
            with open(os.path.join(d, "universe", "tradable.json"), "w") as fh:
                json.dump({"tickers": [{"symbol": "SLB", "name": "SLB N.V."}, {"symbol": "BBY", "name": "Best Buy Co., Inc."}]}, fh)
            M.use(d)
            self.assertEqual(M.company("BBY_US_EQ"), "Best Buy (BBY)")
            self.assertEqual(M.company("SLB"), "SLB")
        finally:
            shutil.rmtree(d)

    def test_odd_characters_are_escaped(self):
        text = M.bought("AT&T", 3, 20.5, 50, 19.9, 2.3)
        self.assertIn("AT&amp;T &lt;Wireless&gt; (AT&amp;T)", text)
        self.assertTrue(well_formed(text), text)
        self.assertIn("AT&T <Wireless>", M.plain(text))


class Money(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(M.gbp(1079.984), "£1,079.98")
        self.assertEqual(M.signed_gbp(-26.5), "−£26.50")
        self.assertEqual(M.signed_gbp(6.71), "+£6.71")
        self.assertEqual(M.usd(240.13), "$240.13")
        self.assertEqual(M.shares(5.94), "5.94 shares")
        self.assertEqual(M.shares(44.5), "44.5 shares")
        self.assertEqual(M.shares(1), "1 share")
        self.assertEqual(M.shares(20.6549), "20.65 shares")


class PastTrades(unittest.TestCase):
    def test_honest_and_in_pounds_never_r(self):
        line = M.past_trades({"avg_R": 0.05, "avg_R_net": -0.23}, 18.61)
        self.assertEqual(line, "📉 Past similar trades: a small profit before fees, around −£4 a trade after them")
        self.assertEqual(M.past_trades({"avg_R": 0.01, "avg_R_net": -0.02}, 10),
                         "📉 Past similar trades: about break-even before fees, around −20p a trade after them")
        self.assertTrue(M.past_trades({"avg_R": 0.4, "avg_R_net": 0.2}, 20).startswith("📈"))
        self.assertIsNone(M.past_trades({"p_target": 0.3, "p_stop": 0.6}))           # old tickets: no line
        self.assertNotRegex(line, JARGON)


class Messages(unittest.TestCase):
    def setUp(self):
        M.set_names({"ADBE": "Adobe", "QCOM": "Qualcomm", "STX": "Seagate Technology", "BBY": "Best Buy",
                     "BDX": "Becton Dickinson", "GDDY": "GoDaddy"})

    def tearDown(self):
        M.use(None)

    def check(self, text):
        self.assertTrue(well_formed(text), text)
        self.assertNotRegex(M.plain(text), JARGON)
        self.assertLess(len(text), 4096)
        return text

    def test_buy(self):
        t = self.check(M.bought("ADBE", 5.94, 240.13, 1079.98, 236.75, 18.61, tm={"avg_R": 0.05, "avg_R_net": -0.23},
                                trail_from=243.51, close_uk="8:50pm"))
        for want in ("🟢 <b>Bought Adobe (ADBE)</b>", "<b>5.94 shares</b> at <b>$240.13</b> each",
                     "Cost: <b>£1,079.98</b> (including the currency fee)",
                     "🛡️ Safety net: auto-sells if it drops to $236.75. Most I could lose: <b>about £18.61</b>",
                     "If it climbs past $243.51", "(8:50pm UK)", "Reply /pause"):
            self.assertIn(want, t)
        self.assertNotIn("Aim", t)                      # the guard never sells at a fixed target on this config

    def test_sale(self):
        t = self.check(M.sold("QCOM_US_EQ", 7.95, 177.74, 177.20, "stop", -6.65))
        self.assertEqual(t, "🔴 <b>Sold Qualcomm (QCOM)</b>: hit the safety net\n"
                            "• 7.95 shares: bought at $177.74, sold at $177.20\n• Result: <b>−£6.65</b>")
        t = self.check(M.sold("STX", 2.65, 539.62, 544.19, "trailing stop", 6.71))
        self.assertIn("🟢 <b>Sold Seagate Technology (STX)</b>: the safety net locked in a profit", t)
        self.check(M.sold_unknown("BBY", 34.2, 105.38))

    def test_errors_are_one_human_sentence(self):
        raw = ('NOT PLACED: 400: Trading 212 error on POST /equity/orders/market ({"type":"/api-errors/insufficient-'
               'free-for-stocks-buy","status":400,"detail":"Insufficient funds","traceId":"9fd58af4"}). Not retried.')
        self.assertEqual(self.check(M.not_bought("BDX", raw)),
                         "⚠️ Couldn't buy Becton Dickinson (BDX): not enough cash in the account. Skipped it.")
        self.assertIn("fallen to the safety-net level",
                      M.not_bought("EL", "EL is at $113.09, at or under the $113.95 stop"))
        self.assertIn("didn't accept", M.not_bought("X", "something new <weird> & odd"))

    def test_guard_alarms_keep_the_action(self):
        t = self.check(M.guard_alert("STOP GONE at Trading 212 (CANCELLED) for GDDY_US_EQ (SELL 44.5 at 112.26). The guard "
                                     "now ENFORCES IT IN SOFTWARE: it sells at market ..."))
        self.assertIn("GoDaddy (GDDY): Trading 212 dropped the safety net", t)
        self.assertIn("$112.26", t)
        self.assertIn("set a stop-loss in the Trading 212 app", t)
        t = self.check(M.guard_alert("END-OF-DAY CLOSE FAILED: TSLA_US_EQ: the end-of-day SELL 7 was REFUSED (x)"))
        self.assertIn("Couldn't sell everything at the close", t)
        self.check(M.guard_alert("something nobody planned for: <b>"))       # unknown text still escaped

    def test_guard_news_from_one_pass(self):
        t212 = load("trader-broker", "t212")
        res = {"positions": [{"ticker": "A_US_EQ", "status": "covered"},
                             {"ticker": "B_US_EQ", "status": "stop placed", "placed_qty": 5, "stop": 10.0},
                             {"ticker": "C_US_EQ", "status": "covered", "trail": "RAISED the stop 1.00 -> 2.00 (high 3.00)"},
                             {"ticker": "D_US_EQ", "status": "SOLD AT STOP", "trail": "market SELL 2 at the 9.00 stop"}],
               "warnings": ["w1"]}
        out = t212.guard_phone_texts(M, res)
        self.assertEqual([o for _, o in out], [None, "trail-C_US_EQ", "w1"])      # a raise: once a day per stock
        self.assertIn("Safety net is now in for B", out[0][0])
        self.assertIn("raised its safety net from $1.00 to <b>$2.00</b>", out[1][0])

    def test_evening_report(self):
        r = {"day": datetime.date(2026, 11, 12), "fills_ok": True, "total": 4878.78,
             "today": [{"symbol": "QCOM", "entry": 177.74, "exit": 177.20, "reason": "stop", "pl": -6.65,
                        "tm": {"avg_R": 0.12}, "r": -1.08},
                       {"symbol": "STX", "entry": 539.62, "exit": 544.19, "reason": "trailing stop", "pl": 6.71,
                        "tm": {"avg_R": 0.06}, "r": 0.35},
                       {"symbol": "BBY", "entry": 105.19, "exit": 105.23, "reason": "end-of-day close", "pl": -8.64}],
             "week": [{"pl": -100.0}, {"pl": -8.65}], "alerts": [], "errors": []}
        watch = M.watchlist({"rows": 151, "names": [], "report_day": "2026-11-11"}, "closing")
        t = self.check(M.evening(r, "trader/reports/2026-11-12.md", M.compare_line(r["today"]), watch, M.market_line(0.43)))
        for want in ("📊 <b>Thursday 12 Nov: day summary</b>", "🔴 <b>Down £8.58 today</b> (3 trades, 1 winner)",
                     "This week: <b>−£108.65</b>", "Account value: <b>£4,878.78</b>",
                     "1. 🔴 Qualcomm (QCOM): $177.74 → $177.20, safety net triggered → <b>−£6.65</b>",
                     "the safety net locked in a profit", "sold at the close",
                     "(price barely moved, so fees made it a small loss)",
                     "the US market (S&amp;P 500) rose 0.4% today",
                     "None. 151 companies reported yesterday, none fitted the rules.",
                     "Full report: trader/reports/2026-11-12.md", "<i>Practice account</i>"):
            self.assertIn(want, t)

    def test_watchlist_states(self):
        self.assertEqual(M.watchlist({"off": True}, "closing"), [])
        self.assertIn("hasn't run", M.watchlist({"missing": True}, "briefing")[1])
        w = M.watchlist({"names": ["ADBE"], "exit_day": "2026-12-10", "buying": True}, "closing")
        self.assertIn("will buy at the open", w[0])
        self.assertIn("Adobe (ADBE)", w[1])

    def test_commands_and_status(self):
        self.assertIn("/resume", M.paused())
        self.assertIn("/pause", M.help_text())
        t = self.check(M.status("2026-11-12", True, "open", True, "", True, False, 2,
                                [("ADBE_US_EQ", 5.94, 241.0, M.safety_words("order", 236.75))]))
        self.assertIn("Adobe (ADBE): 5.94 shares, now $241.00. 🛡️ Safety net at $236.75", t)

    def test_friday_has_no_r(self):
        p = {"status": "TOO FEW TRADES TO JUDGE", "n": 9, "min_trades": 20, "lean": "NOT ON TRACK",
             "total_gbp": -121.22, "strategy_ret": -2.4, "tracker_ret": 0.6}
        r = {"today": "2026-11-13", "start": "2026-11-09", "week": p, "since_start": p, "real": [1] * 9,
             "real_avg_gbp": -13.47, "real_total_gbp": -121.22, "virtual": 183, "virtual_avg_ret": -0.31,
             "virtual_win": 41.0, "slip_entry": 0.04, "slip_exit": 0.02,
             "kronos": {"agree": [3, 0.1], "disagree": [5, -0.2], "other": 100}}
        t = self.check(M.friday(r, M.friday_drift(None, {"n": 4, "avg": 1.2, "vs": -0.5, "status": "too few"})))
        self.assertIn("too few trades to judge yet (9 of 20), not on track so far", t)
        self.assertIn("the ones it agreed with did better", t)


class Telegram(test_phone.Base):
    def test_sends_html(self):
        p = self.paired()
        p.send("<b>hi</b> &amp; bye")
        method, body, _ = self.tg.calls[-1]
        self.assertEqual((method, body["parse_mode"], body["text"]), ("sendMessage", "HTML", "<b>hi</b> &amp; bye"))

    def test_falls_back_to_plain_text_when_telegram_rejects_the_formatting(self):
        hdrs = email.message.Message()
        bad = urllib.error.HTTPError("u", 400, "Bad Request", hdrs, io.BytesIO(
            b'{"ok":false,"description":"Bad Request: can\'t parse entities: unclosed tag"}'))
        self.tg.add("sendMessage", None, bad)
        self.tg.add("sendMessage", {"message_id": 9})
        p = self.paired()
        self.assertEqual(p.send("<b>Bought AT&amp;T"), 9)
        last = self.tg.calls[-1][1]
        self.assertNotIn("parse_mode", last)
        self.assertEqual(last["text"], "Bought AT&T")          # the same words, without the formatting

    def test_other_errors_are_not_retried_as_plain(self):
        hdrs = email.message.Message()
        self.tg.add("sendMessage", None, urllib.error.HTTPError("u", 403, "Forbidden", hdrs, io.BytesIO(b"{}")))
        p = self.paired()
        with self.assertRaises(test_phone.phone.PhoneError):
            p.send("x")
        self.assertEqual(len(self.tg.calls), 1)

    def test_a_long_message_is_split_cleanly_with_buttons_on_the_last_piece(self):
        p = self.paired()
        lines = ["%d. 🔴 <b>Trade %d</b>: result" % (i, i) for i in range(400)]
        p.send("\n".join(lines[:200]) + "\n\n" + "\n".join(lines[200:]), [("Approve", "ap:x")])
        sends = [c[1] for c in self.tg.calls if c[0] == "sendMessage"]
        self.assertGreater(len(sends), 1)
        self.assertTrue(all(len(s["text"]) <= 4096 and well_formed(s["text"]) for s in sends))
        self.assertEqual("\n".join(s["text"] for s in sends).replace("\n\n", "\n"), "\n".join(lines))
        self.assertTrue(all("reply_markup" not in s for s in sends[:-1]))
        self.assertIn("reply_markup", sends[-1])

    def test_split_keeps_short_text_whole(self):
        self.assertEqual(phone.split("a\n\nb"), ["a\n\nb"])
        self.assertEqual(phone.split("x" * 9000, 4000), ["x" * 4000, "x" * 4000, "x" * 1000])


if __name__ == "__main__":
    unittest.main()
