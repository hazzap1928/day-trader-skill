"""The cap on total risk ("max_total_risk_pct"): what ALL holdings could lose at their stops together,
as a share of the account. From 16 Nov 2026 it replaces the daily loss limit when it is set.
Also trader-risk's "strategy": a holding (news, drift) is not a day trade. No network."""

import unittest

from _load import load
import test_risk as R
import test_real_exit as RE
import test_eod_close
import test_drift_buy
from test_eod_close import STOP, held, record
from test_t212_place import FakeResp, SUMMARY

check = R.check
t212 = test_eod_close.t212
day_risk = load("trader-broker", "day_risk")

CAP = {"max_total_risk_pct": 4, "max_trades_per_day": 9}
THREE = {"orders": [RE.fp_placed("AAPL", "f1"), RE.fp_placed("AMD", "f2"), RE.fp_placed("NVDA", "f3")]}


def total(open_usd, account_usd=5000.0, at=R.NOW, day=R.TODAY):
    return {"date": day, "built_at": RE.built_at(at), "total": {"open_risk_usd": open_usd, "account_usd": account_usd}}


def no_target():
    o = R.tsla()
    o.pop("take_profit_limit_price", None)
    o["order_class"] = None
    o["plan"] = dict(o.get("plan") or {}, target=None)
    return o


class TheCap(unittest.TestCase):
    def test_it_replaces_the_daily_loss_limit(self):
        # three entries today: the old daily count (3 x 46.88 + this one over $150) would block
        old = check.check([R.tsla()], R.cfg(max_trades_per_day=9), state=THREE, now=R.at(R.NOW))
        self.assertIn("daily loss:", R.reasons(old))
        res = check.check([R.tsla()], R.cfg(**CAP), state=THREE, now=R.at(R.NOW), day_risk=total(0.0))
        self.assertEqual(R.verdicts(res), ["PASS"])
        self.assertEqual(res["today"]["daily_loss_count"], "total_risk")
        self.assertEqual(res["today"]["total_risk_cap"], 200.0)
        self.assertNotIn("daily loss", " ".join(res["results"][0]["info"]))

    def test_a_buy_that_takes_the_total_over_the_cap_is_refused(self):
        risk = check.check([R.tsla()], R.cfg(**CAP), now=R.at(R.NOW), day_risk=total(0.0))["results"][0]["max_loss"]
        res = check.check([R.tsla()], R.cfg(**CAP), now=R.at(R.NOW), day_risk=total(200.0 - risk + 0.02))
        self.assertEqual(R.verdicts(res), ["BLOCK"])
        self.assertIn("total risk:", R.reasons(res))
        self.assertIn("4% of the account", R.reasons(res))
        res = check.check([R.tsla()], R.cfg(**CAP), now=R.at(R.NOW), day_risk=total(200.0 - risk))
        self.assertEqual(R.verdicts(res), ["PASS"])                     # exactly at the cap fits

    def test_the_cap_follows_the_account_value(self):
        res = check.check([R.tsla()], R.cfg(**CAP), now=R.at(R.NOW), day_risk=total(100.0, account_usd=2500.0))
        self.assertIn("total risk:", R.reasons(res))                    # 4% of 2,500 is 100: full already

    def test_two_buys_in_one_batch_are_counted_together(self):
        risk = check.check([R.tsla()], R.cfg(**CAP), now=R.at(R.NOW), day_risk=total(0.0))["results"][0]["max_loss"]
        two = [R.tsla(), R.bracket("AAPL", 2, 300.0, 280.0, 340.0)]
        res = check.check(two, R.cfg(max_position_pct=100, **CAP), now=R.at(R.NOW), prices={"AAPL": 300.0},
                          day_risk=total(200.0 - risk - 10.0))
        self.assertEqual(R.verdicts(res), ["PASS", "BLOCK"])
        self.assertIn("total risk:", R.reasons(res, 1))

    def test_when_the_total_is_not_known_nothing_is_bought(self):
        for doc in (None, {}, total(0.0, day="2026-10-16"), total(0.0, at="2026-10-19 09:50"),
                    {"date": R.TODAY, "built_at": RE.built_at(R.NOW)},
                    {"date": R.TODAY, "built_at": RE.built_at(R.NOW), "total": {"open_risk_usd": None, "account_usd": 5000}}):
            res = check.check([R.tsla()], R.cfg(**CAP), now=R.at(R.NOW), day_risk=doc)
            self.assertEqual(R.verdicts(res), ["BLOCK"], doc)
            self.assertIn("is not known", R.reasons(res))

    def test_an_exit_is_never_blocked_by_it(self):
        sell = {"symbol": "TSLA", "side": "sell", "qty": "5", "type": "market", "time_in_force": "day"}
        port = {"cash": 1000.0, "positions": [{"ticker": "TSLA", "shares": 5, "lots": [{"shares": 5, "cost": 1800.0}]}]}
        res = check.check([sell], R.cfg(**CAP), portfolio=port, prices={"TSLA": 366.0}, now=R.at(R.NOW), day_risk=total(9999.0))
        self.assertEqual(R.verdicts(res), ["PASS"])

    def test_without_the_setting_nothing_changes(self):
        res = check.check([R.tsla()], R.cfg(), now=R.at(R.NOW), day_risk=total(9999.0))
        self.assertEqual(R.verdicts(res), ["PASS"])
        self.assertEqual(res["today"]["total_risk_cap"], None)

    def test_the_setting_is_checked(self):
        self.assertIsNone(day_risk.total_cap_pct({}))
        self.assertEqual(day_risk.total_cap_pct({"max_total_risk_pct": 4}), 4.0)
        for bad in ("lots", 0, 0.1, 50):
            with self.assertRaises(ValueError, msg=bad):
                day_risk.total_cap_pct({"max_total_risk_pct": bad})


class AHoldingIsNotADayTrade(unittest.TestCase):
    EARLY = "2026-10-19 07:00"            # hours before the open: the morning news run

    def test_news_passes_before_the_open_and_without_a_target(self):
        day = check.check([no_target()], R.cfg(), now=R.at(self.EARLY))
        self.assertIn("window:", R.reasons(day))                        # a day trade: outside its window, no target
        self.assertIn("reward:risk: no target", R.reasons(day))
        res = check.check([no_target()], R.cfg(), now=R.at(self.EARLY), strategy="news")
        self.assertEqual(R.verdicts(res), ["PASS"])
        self.assertEqual(res["today"]["strategy"], "news")

    def test_news_still_needs_a_stop_a_loss_limit_cash_and_the_lists(self):
        o = no_target()
        o.pop("stop_loss_stop_price", None)
        o["plan"] = dict(o["plan"], stop=None)
        self.assertIn("stop:", R.reasons(check.check([o], R.cfg(), now=R.at(self.EARLY), strategy="news")))
        self.assertIn("loss:", R.reasons(check.check([no_target()], R.cfg(max_loss_per_trade_usd=5), now=R.at(self.EARLY),
                                                     strategy="news")))
        self.assertIn("blocked:", R.reasons(check.check([no_target()], R.cfg(blocked_tickers=["TSLA"]), now=R.at(self.EARLY),
                                                        strategy="news")))
        self.assertIn("allowed_tickers:", R.reasons(check.check([no_target()], R.cfg(allowed_tickers=["AAPL"]),
                                                                now=R.at(self.EARLY), strategy="news")))
        self.assertIn("cash:", R.reasons(check.check([no_target()], R.cfg(), portfolio={"cash": 100.0, "positions": []},
                                                     now=R.at(self.EARLY), strategy="news")))

    def test_news_is_refused_when_the_market_is_closed_all_day(self):
        res = check.check([no_target()], R.cfg(), now=R.at("2026-10-17 11:00"), strategy="news")     # a Saturday
        self.assertIn("market is closed today", R.reasons(res))

    def test_trades_a_day_do_not_count_for_or_against_a_holding(self):
        res = check.check([no_target()], R.cfg(max_trades_per_day=3, max_daily_loss_usd=0), state=THREE,
                          now=R.at(R.NOW), strategy="news")
        self.assertEqual(R.verdicts(res), ["PASS"])
        held_news = {"orders": [dict(RE.fp_placed("AAPL", "n1"), strategy="news"),
                                dict(RE.fp_placed("AMD", "n2"), strategy="swing")]}
        res = check.check([R.tsla()], R.cfg(max_trades_per_day=1), state=held_news, now=R.at(R.NOW))
        self.assertEqual(R.verdicts(res), ["PASS"])                     # holdings bought today are not day trades
        self.assertEqual(res["today"]["entries_placed"], 0)

    def test_the_cap_applies_to_a_holding_too(self):
        res = check.check([no_target()], R.cfg(**CAP), now=R.at(self.EARLY), strategy="news",
                          day_risk=total(190.0, at=self.EARLY))
        self.assertIn("total risk:", R.reasons(res))

    def test_an_unknown_strategy_is_refused(self):
        with self.assertRaises(check.RiskError):
            check.check([R.tsla()], R.cfg(), now=R.at(R.NOW), strategy="swing")


class TotalFromTheAccount(test_eod_close.Close):
    def test_every_holding_counts_whatever_its_label_or_age(self):
        old = record("AAPL_US_EQ", strategy="drift", sell_date="2026-12-01", date="2026-10-20", stop=250.0, entry=260.0)
        self.write("state/broker.json", {"orders": [record(strategy="news", sell_date="2026-12-01"), old]})
        aapl = {"instrument": {"ticker": "AAPL_US_EQ"}, "quantity": 3, "currentPrice": 262.0, "averagePricePaid": 260.0}
        tsla = dict(held(7), averagePricePaid=364.18)
        r = self.router
        r.add("GET", "/equity/positions", FakeResp([tsla, aapl]))
        r.add("GET", "/equity/orders", FakeResp([STOP]))
        r.add("GET", "/equity/account/summary", FakeResp(SUMMARY))
        t = day_risk.total(self.client, self.dir, 1.25, t212)
        by = {x["symbol"]: x for x in t["rows"]}
        self.assertEqual(by["TSLA"]["how"], "stop order")
        self.assertEqual(by["AAPL"]["how"], "planned stop")             # no order in: its planned stop still counts
        want = 7 * (364.18 - 358.32 + 0.0015 * (364.18 + 358.32)) + 3 * (10.0 + 0.0015 * 510.0)
        self.assertAlmostEqual(t["open_risk_usd"], round(round(want / 1.25, 2) * 1.25, 2), places=1)
        self.assertEqual(t["account_usd"], 6250.0)
        self.assertEqual([c[0] for c in self.router.calls if c[0] != "GET"], [])     # read-only


class DriftObeysTheCap(test_drift_buy.DriftBase):
    def test_a_drift_buy_over_the_total_cap_is_skipped(self):
        cfg = dict(self.cfg, max_total_risk_pct=4)                      # 4% of 5,000 = GBP 200
        self.write("config.json", cfg)
        naked = {"instrument": {"ticker": "AAPL_US_EQ"}, "quantity": 10, "quantityAvailableForTrading": 10,
                 "currentPrice": 200.0, "averagePricePaid": 200.0}      # no stop, no record: its whole value is at risk
        self.account(positions=[naked])
        res = self.buy(cfg)
        self.assertEqual(res["bought"], [])
        self.assertIn("max_total_risk_pct", res["skipped"][0][1])
        self.assertEqual(self.router.posts(), [])

    def test_inside_the_cap_it_buys_as_before(self):
        cfg = dict(self.cfg, max_total_risk_pct=4)
        self.write("config.json", cfg)
        self.account()
        res = self.buy(cfg)
        self.assertEqual([b["symbol"] for b in res["bought"]], ["TSLA"])


if __name__ == "__main__":
    unittest.main()
