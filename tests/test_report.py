"""report.py: the closing report from Trading 212's fills. Fake history, no network."""

import io
import os
import unittest
from contextlib import redirect_stdout

from _load import load
import test_t212_place
from test_t212_place import Base, FakeResp, CREDS, NOW, TODAY

t212 = test_t212_place.t212
rp = load("trader-broker", "report")
rp.t212 = t212
rp.tk = t212.ticket_module()
rp.phone_mod = t212.phone_module()


def fill(side, qty, price, at, ticker="TSLA_US_EQ", typ="MARKET", fee=3.0, rate=1.25, frm="API", oid=1):
    return {"order": {"id": oid, "side": side, "type": typ, "ticker": ticker, "initiatedFrom": frm, "status": "FILLED",
                      "quantity": qty if side == "BUY" else -qty},
            "fill": {"quantity": qty, "price": price, "filledAt": at,
                     "walletImpact": {"currency": "GBP", "fxRate": rate,
                                      "taxes": [{"name": "CURRENCY_CONVERSION_FEE", "quantity": -fee}]}}}


class ReportBase(Base):
    def setUp(self):
        super().setUp()
        self._rp = rp._now
        rp._now = lambda: self.clock[0]
        self.clock[0] = NOW + 5.5 * 3600            # Thu 16:00 New York
        self.client = t212.Client("demo", CREDS["T212_API_KEY"], CREDS["T212_API_SECRET"], state_dir=self.dir,
                                  log=lambda m: None)

    def tearDown(self):
        rp._now = self._rp
        super().tearDown()

    def account(self, items, positions=(), pages=None):
        r = self.router
        if pages:
            r.add("GET", "/equity/history/orders", *[FakeResp(p) for p in pages])
        else:
            r.add("GET", "/equity/history/orders", FakeResp({"items": list(reversed(items)), "nextPagePath": None}))
        r.add("GET", "/equity/account/summary", FakeResp(test_t212_place.SUMMARY))
        r.add("GET", "/equity/positions", FakeResp(list(positions)))

    def build(self):
        return rp.build(self.client, self.dir)


class Trades(ReportBase):
    def test_pl_in_gbp_after_fees_from_the_fills(self):
        self.account([fill("BUY", 7, 364.18, "2026-11-05T15:20:00.000Z", fee=3.06),
                      fill("SELL", 7, 370.04, "2026-11-05T17:40:00.000Z", typ="STOP", fee=3.11)])
        r = self.build()
        t = r["today"][0]
        self.assertAlmostEqual(t["pl"], 7 * 370.04 / 1.25 - 3.11 - (7 * 364.18 / 1.25 + 3.06), places=6)
        self.assertEqual(round(t["pl"], 2), 26.65)
        self.assertEqual(t["reason"], "stop")
        self.assertIn("TSLA: 7 shares, in $364.18, out $370.04, stop; P&L +£26.65", rp.full_text(r))

    def test_first_in_first_out_across_days(self):
        self.account([fill("BUY", 5, 100.0, "2026-11-03T15:00:00.000Z", fee=0.4),
                      fill("BUY", 5, 110.0, "2026-11-05T15:00:00.000Z", fee=0.44),
                      fill("SELL", 6, 120.0, "2026-11-05T18:00:00.000Z", fee=0.58)])
        t = self.build()["today"][0]
        self.assertAlmostEqual(t["entry"], (5 * 100 + 1 * 110) / 6.0)
        cost = 5 * 100 / 1.25 + 0.4 + (5 * 110 / 1.25 + 0.44) / 5
        self.assertAlmostEqual(t["pl"], 6 * 120 / 1.25 - 0.58 - cost, places=6)

    def test_exit_reasons(self):
        self.write("state/broker.json", {"orders": [{"symbol": "TSLA", "kind": "entry", "status": "stop_placed",
                                                     "date": TODAY, "stop": 358.32, "target": 380.0}]})
        cases = [({"eod": {"date": TODAY, "closed": ["TSLA_US_EQ: SELL 7 at market, order id 1"]}}, "MARKET", "API",
                  "end-of-day close"),
                 ({"trail": {"TSLA_US_EQ": {"stop": 362.0}}}, "STOP", "API", "trailing stop"),
                 ({}, "STOP", "API", "stop"),
                 ({}, "MARKET", "WEB", "sold in the Trading 212 app")]
        for guard, typ, frm, want in cases:
            with self.subTest(want):
                self.router.routes.clear()
                self.write("state/t212-guard.json", guard)
                self.account([fill("BUY", 7, 364.18, "2026-11-05T15:20:00.000Z"),
                              fill("SELL", 7, 363.0, "2026-11-05T20:50:00.000Z", typ=typ, frm=frm)])
                self.assertEqual(self.build()["today"][0]["reason"], want)

    def test_week_totals_and_win_rate(self):
        self.account([fill("BUY", 1, 100.0, "2026-11-02T15:00:00.000Z", fee=0),
                      fill("SELL", 1, 90.0, "2026-11-02T16:00:00.000Z", fee=0),        # Mon: -8.00
                      fill("BUY", 1, 100.0, "2026-11-05T15:00:00.000Z", fee=0),
                      fill("SELL", 1, 125.0, "2026-11-05T16:00:00.000Z", fee=0),       # Thu: +20.00
                      fill("BUY", 1, 100.0, "2026-10-30T15:00:00.000Z", fee=0),
                      fill("SELL", 1, 50.0, "2026-10-30T16:00:00.000Z", fee=0)])       # last week: not counted
        r = self.build()
        d, w = rp.totals(r["today"]), rp.totals(r["week"])
        self.assertEqual((d["n"], round(d["pl"], 2), d["rate"]), (1, 20.0, 100.0))
        self.assertEqual((w["n"], round(w["pl"], 2), w["rate"]), (2, 12.0, 50.0))
        self.assertIn("This week (from Mon 02 Nov): 2 trades, +£12.00, win rate 50 % (1 of 2)", rp.full_text(r))

    def test_against_the_time_machine(self):
        self.write("state/broker.json", {"orders": [{"symbol": "TSLA", "kind": "entry", "status": "stop_placed",
                                                     "date": TODAY, "stop": 358.32, "target": 370.04}]})
        self.write("state/autopilot.json", {"tickets": [{"symbol": "TSLA", "tm": {"p_target": 0.6, "p_stop": 0.4}}]})
        self.account([fill("BUY", 7, 364.18, "2026-11-05T15:20:00.000Z"),
                      fill("SELL", 7, 358.10, "2026-11-05T17:00:00.000Z", typ="STOP")])
        r = self.build()
        text = rp.full_text(r)
        self.assertIn("Time Machine: target first 60.0 %, stop first 40.0 %; it hit the stop", text)
        self.assertIn("gave the target first 60 % on average over 1 trade; 0 of 1 reached the target first", text)
        self.assertIn("-1.04R", text)

    def test_still_open_alerts_and_a_buy_today_still_held(self):
        os.makedirs(os.path.join(self.dir, "logs"), exist_ok=True)
        with open(os.path.join(self.dir, "logs", "guard-alerts.jsonl"), "w") as fh:
            fh.write('{"at": "2026-11-05T20:51:00Z", "text": "END-OF-DAY CLOSE FAILED: TSLA"}\n')
        self.account([fill("BUY", 7, 364.18, "2026-11-05T15:20:00.000Z")],
                     positions=[{"instrument": {"ticker": "TSLA_US_EQ"}, "quantity": 7, "currentPrice": 366.0}])
        r = self.build()
        text = rp.full_text(r)
        self.assertIn("Bought today, still open: TSLA 7 at $364.18", text)
        self.assertIn("STILL OPEN: TSLA_US_EQ 7", text)
        self.assertIn("END-OF-DAY CLOSE FAILED: TSLA", text)
        short = rp.short_text(r, os.path.join(self.dir, "x.md"))
        self.assertIn("!!! STILL OPEN: TSLA_US_EQ 7", short)
        self.assertIn("!!! 1 guard alert today", short)

    def test_pages_are_followed(self):
        p1 = {"items": [fill("SELL", 7, 370.0, "2026-11-05T17:00:00.000Z")],
              "nextPagePath": "/api/v0/equity/history/orders?limit=50&cursor=abc"}
        p2 = {"items": [fill("BUY", 7, 364.0, "2026-11-05T15:00:00.000Z")], "nextPagePath": None}
        self.account([], pages=[p1, p2])
        r = self.build()
        self.assertEqual(len(r["today"]), 1)
        self.assertIn("cursor=abc", [c[3] for c in self.router.calls if c[1] == "/equity/history/orders"][1])


class NoData(ReportBase):
    def test_no_history_says_no_data_not_zero(self):
        self.router.add("GET", "/equity/history/orders", test_t212_place.http_error(403, "history:orders"))
        self.router.add("GET", "/equity/account/summary", FakeResp(test_t212_place.SUMMARY))
        self.router.add("GET", "/equity/positions", FakeResp([]))
        r = self.build()
        short = rp.short_text(r, os.path.join(self.dir, "x.md"))
        self.assertIn("today no data", short)
        self.assertIn("no data: order history", short)
        self.assertNotIn("+£0.00", short)

    def test_read_only(self):
        self.account([fill("BUY", 7, 364.18, "2026-11-05T15:20:00.000Z")])
        self.build()
        self.assertEqual([c for c in self.router.calls if c[0] != "GET"], [])


if __name__ == "__main__":
    unittest.main()
