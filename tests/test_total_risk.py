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
    def test_the_daily_loss_limit_still_covers_day_trades(self):
        # three entries today: the daily count (3 x 46.88 + this one over $150) blocks a day trade, cap or no cap
        old = check.check([R.tsla()], R.cfg(max_trades_per_day=9), state=THREE, now=R.at(R.NOW))
        self.assertIn("daily loss:", R.reasons(old))
        res = check.check([R.tsla()], R.cfg(**CAP), state=THREE, now=R.at(R.NOW), day_risk=total(0.0))
        self.assertEqual(R.verdicts(res), ["BLOCK"])
        self.assertIn("daily loss:", R.reasons(res))
        self.assertNotIn("total risk:", R.reasons(res))
        self.assertEqual(res["today"]["total_risk_cap"], 200.0)

    def test_a_holding_is_not_stopped_by_the_daily_loss_limit(self):
        res = check.check([no_target()], R.cfg(**CAP), state=THREE, now=R.at(R.NOW), day_risk=total(0.0), strategy="news")
        self.assertEqual(R.verdicts(res), ["PASS"])
        self.assertNotIn("daily loss", " ".join(res["results"][0]["info"]))

    def test_with_two_entries_today_a_day_trade_still_fits_and_sees_both_limits(self):
        two = {"orders": THREE["orders"][:2]}
        res = check.check([R.tsla()], R.cfg(**CAP), state=two, now=R.at(R.NOW), day_risk=total(0.0))
        self.assertEqual(R.verdicts(res), ["PASS"])
        info = " ".join(res["results"][0]["info"])
        self.assertIn("daily loss", info)
        self.assertIn("total risk before this", info)

    def test_todays_losses_count_the_open_risk_way_beside_the_cap(self):
        # "daily_loss_count": "open_risk": two entries stopped out for $60 each and one still open with $20 at risk
        dr = total(20.0)
        dr["entries"] = [{"fingerprint": "f1", "counted_usd": 60.0}, {"fingerprint": "f2", "counted_usd": 60.0},
                         {"fingerprint": "f3", "counted_usd": 20.0}]
        res = check.check([R.tsla()], R.cfg(daily_loss_count="open_risk", **CAP), state=THREE, now=R.at(R.NOW), day_risk=dr)
        self.assertIn("daily loss: $140 lost or still at risk", R.reasons(res))
        dr["entries"][0]["counted_usd"] = 0.0                           # a winner: its risk is freed
        res = check.check([R.tsla()], R.cfg(daily_loss_count="open_risk", **CAP), state=THREE, now=R.at(R.NOW), day_risk=dr)
        self.assertEqual(R.verdicts(res), ["PASS"])

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


SPLIT = {"risk_caps_pct": {"drift": 2, "news": 2, "intraday": 1}, "max_trades_per_day": 9}


def split(at=R.NOW, day=R.TODAY, account_usd=5000.0, **by):
    """day-risk.json with the risk at the stops under each strategy, in dollars."""
    return {"date": day, "built_at": RE.built_at(at),
            "total": {"open_risk_usd": sum(by.values()), "account_usd": account_usd, "by_strategy_usd": dict(by)}}


class ACapForEachStrategy(unittest.TestCase):
    """"risk_caps_pct": drift 2%, news 2%, intraday 1% of the account. One strategy cannot block another."""

    def test_a_full_news_or_drift_book_does_not_block_a_day_trade(self):
        res = check.check([R.tsla()], R.cfg(**SPLIT), now=R.at(R.NOW), day_risk=split(news=100.0, drift=100.0))
        self.assertEqual(R.verdicts(res), ["PASS"])                     # intraday: $46.88 against its own $50
        self.assertEqual(res["today"]["total_risk_cap"], 50.0)
        self.assertIn("the day trades held", " ".join(res["results"][0]["info"]))

    def test_a_day_trade_is_held_to_intradays_one_percent(self):
        res = check.check([R.tsla()], R.cfg(**SPLIT), now=R.at(R.NOW), day_risk=split(intraday=10.0))
        self.assertEqual(R.verdicts(res), ["BLOCK"])
        self.assertIn("total risk:", R.reasons(res))
        self.assertIn("1% of the account", R.reasons(res))

    def test_news_is_held_to_its_own_two_percent_whatever_the_others_hold(self):
        full = check.check([no_target()], R.cfg(**SPLIT), now=R.at(R.NOW), day_risk=split(news=60.0), strategy="news")
        self.assertIn("the news holdings", R.reasons(full))             # 60 + 46.88 is over 2% of 5,000
        self.assertIn("2% of the account", R.reasons(full))
        ok = check.check([no_target()], R.cfg(**SPLIT), now=R.at(R.NOW), strategy="news",
                         day_risk=split(news=50.0, drift=100.0, intraday=50.0))
        self.assertEqual(R.verdicts(ok), ["PASS"])
        self.assertEqual(ok["today"]["total_risk_cap"], 100.0)

    def test_the_overall_cap_is_not_what_decides_once_each_strategy_has_its_own(self):
        cfg = R.cfg(max_total_risk_pct=4, **SPLIT)
        res = check.check([R.tsla()], cfg, now=R.at(R.NOW), day_risk=split(news=100.0, drift=100.0))
        self.assertEqual(R.verdicts(res), ["PASS"])                     # 4% is used up by the others; intraday has its own 1%

    def test_unknown_blocks(self):
        no_split = total(0.0)                                           # a total, but not by strategy
        for doc in (None, no_split, split(day="2026-10-16")):
            res = check.check([R.tsla()], R.cfg(**SPLIT), now=R.at(R.NOW), day_risk=doc)
            self.assertEqual(R.verdicts(res), ["BLOCK"], doc)
            self.assertIn("is not known", R.reasons(res))
        cfg = R.cfg(risk_caps_pct={"drift": 2}, max_trades_per_day=9)   # no cap written for intraday
        res = check.check([R.tsla()], cfg, now=R.at(R.NOW), day_risk=split())
        self.assertIn("no cap for 'intraday'", R.reasons(res))

    def test_the_setting_is_checked(self):
        for bad in ("2", [], {}, {"news": "lots"}, {"news": -1}, {"news": 50}, {"news": True}):
            with self.assertRaises(check.RiskError, msg=bad):
                check.check([R.tsla()], R.cfg(risk_caps_pct=bad), now=R.at(R.NOW), day_risk=split())
            with self.assertRaises(ValueError, msg=bad):
                day_risk.caps_pct({"risk_caps_pct": bad})
        self.assertIsNone(day_risk.caps_pct({}))
        self.assertEqual(day_risk.caps_pct(SPLIT), {"drift": 2.0, "news": 2.0, "intraday": 1.0})
        self.assertTrue(day_risk.caps_on(SPLIT))
        self.assertFalse(day_risk.caps_on({}))


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
        self.assertEqual(sorted(t["by_strategy_usd"]), ["drift", "news"])            # each strategy's own share
        self.assertAlmostEqual(sum(t["by_strategy_usd"].values()), t["open_risk_usd"], places=1)
        self.assertAlmostEqual(t["by_strategy_gbp"]["drift"], round(3 * (10.0 + 0.0015 * 510.0) / 1.25, 2), places=2)


class TheFileForTraderRisk(test_eod_close.Close):
    def test_the_total_is_read_again_every_time_never_reused(self):
        import test_real_exit
        self.write("fx.json", {"usd_per_gbp": 1.25})
        self.write("state/broker.json", {"orders": []})
        r = self.router
        naked = {"instrument": {"ticker": "AAPL_US_EQ"}, "quantity": 1, "currentPrice": 100.0, "averagePricePaid": 100.0}
        r.add("GET", "/equity/positions", FakeResp([]), FakeResp([naked]))
        r.add("GET", "/equity/orders", FakeResp([]))
        r.add("GET", "/equity/account/summary", FakeResp(SUMMARY))
        rep = load("trader-broker", "report")
        rep.t212, rep.tk = t212, t212.ticket_module()
        day_risk._now = lambda: self.clock[0]
        try:
            first = day_risk.build(self.client, self.dir, "2026-11-05", report_mod=rep, with_total=True)
            second = day_risk.build(self.client, self.dir, "2026-11-05", report_mod=rep, with_total=True)
        finally:
            import time
            day_risk._now = time.time
        self.assertEqual(first["total"]["open_risk_usd"], 0.0)
        self.assertEqual(second["total"]["open_risk_usd"], 100.0)       # a position bought a moment ago counts
        self.assertEqual(self.read("state/day-risk.json")["total"]["account_usd"], 6250.0)


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

    def test_with_its_own_cap_another_strategys_risk_does_not_stop_drift(self):
        cfg = dict(self.cfg, max_total_risk_pct=4, risk_caps_pct={"drift": 2, "news": 2, "intraday": 1})
        self.write("config.json", cfg)
        naked = {"instrument": {"ticker": "AAPL_US_EQ"}, "quantity": 10, "quantityAvailableForTrading": 10,
                 "currentPrice": 200.0, "averagePricePaid": 200.0}      # not a drift position: not drift's risk
        self.account(positions=[naked])
        res = self.buy(cfg)
        self.assertEqual([b["symbol"] for b in res["bought"]], ["TSLA"], res)

    def test_drift_is_held_to_its_own_cap(self):
        cfg = dict(self.cfg, risk_caps_pct={"drift": 0.1, "news": 2, "intraday": 1})     # GBP 5: under one drift buy
        self.write("config.json", cfg)
        self.account()
        res = self.buy(cfg)
        self.assertEqual(res["bought"], [])
        self.assertIn("risk_caps_pct.drift", res["skipped"][0][1])
        self.assertEqual(self.router.posts(), [])
        bad = dict(self.cfg, risk_caps_pct="two")
        self.write("config.json", bad)
        self.assertEqual(self.buy(bad)["status"], "bad risk_caps_pct")

    def test_inside_the_cap_it_buys_as_before(self):
        cfg = dict(self.cfg, max_total_risk_pct=4)
        self.write("config.json", cfg)
        self.account()
        res = self.buy(cfg)
        self.assertEqual([b["symbol"] for b in res["bought"]], ["TSLA"])


if __name__ == "__main__":
    unittest.main()
