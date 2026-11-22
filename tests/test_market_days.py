"""Full NYSE holidays and 1:00 p.m. early closes (21 Nov 2026).

On a full holiday (Thanksgiving 26 Nov, Christmas 25 Dec, New Year 1 Jan) the guard does nothing at all:
no stop check, no approved sale, no end-of-day sale. On an early close (27 Nov, 24 Dec) the end-of-day
sale moves from 15:50 to 12:50 New York, the guard stops at the 13:00 close, and the autopilot's evening
report and "sold at the close" timing follow it.
"""

import calendar
import datetime
import io
import unittest
from contextlib import redirect_stdout, redirect_stderr

import test_t212_place
from test_t212_place import FakeResp, CREDS
import test_eod_close
from test_eod_close import held, STOP
import test_autopilot

t212 = test_t212_place.t212
ap = test_autopilot.ap


def at(day, h, m, s=0):
    """Epoch seconds for New York time h:m on a 2026/2027 date in winter time (UTC-5)."""
    return float(calendar.timegm((day.year, day.month, day.day, h + 5, m, s)))


THANKSGIVING = datetime.date(2026, 11, 26)
BLACK_FRIDAY = datetime.date(2026, 11, 27)        # 1:00 p.m. close
CHRISTMAS_EVE = datetime.date(2026, 12, 24)       # 1:00 p.m. close
CHRISTMAS = datetime.date(2026, 12, 25)
NEW_YEAR = datetime.date(2027, 1, 1)
MONDAY = datetime.date(2026, 11, 30)              # a normal day


class Calendar(unittest.TestCase):
    def test_the_close_each_day(self):
        for d in (THANKSGIVING, CHRISTMAS, NEW_YEAR, datetime.date(2026, 11, 28)):
            self.assertIsNone(t212.market_close(d), d)
        for d in (BLACK_FRIDAY, CHRISTMAS_EVE):
            self.assertEqual(t212.market_close(d), (13, 0), d)
        self.assertEqual(t212.market_close(MONDAY), (16, 0))

    def test_the_end_of_day_sale_moves_before_an_early_close(self):
        self.assertEqual(t212.eod_time((15, 50), MONDAY), (15, 50))
        self.assertEqual(t212.eod_time((15, 50), BLACK_FRIDAY), (12, 50))
        self.assertEqual(t212.eod_time((15, 50), CHRISTMAS_EVE), (12, 50))
        self.assertEqual(t212.eod_time((12, 30), BLACK_FRIDAY), (12, 30))     # already earlier: kept
        self.assertIsNone(t212.eod_time((15, 50), THANKSGIVING))


class Guard(test_eod_close.Close):
    # Close's own tests run in test_eod_close.py; only its set-up is wanted here
    locals().update({n: None for n in dir(test_eod_close.Close) if n.startswith("test_")})

    def watch(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = t212.main(["guard", "--watch", "--every", "5", "--state-dir", self.dir], environ=CREDS)
        return code, out.getvalue()

    def test_a_full_holiday_sends_nothing_and_reads_nothing(self):
        for day in (THANKSGIVING, CHRISTMAS, NEW_YEAR):
            for hm in ((9, 0), (10, 0), (15, 51)):
                self.router.routes.clear()
                del self.router.calls[:]
                self.clock[0] = at(day, *hm)
                code, text = self.watch()
                self.assertEqual(code, 0)
                self.assertIn("US market holiday", text)
                self.assertNotIn("END-OF-DAY", text)
                self.assertEqual(self.router.posts(), [], (day, hm))
                self.assertEqual(self.router.calls, [], (day, hm))     # not even a read

    def test_an_approved_sale_waits_for_the_next_trading_day(self):
        self.write("state/holdings.json", {"pages": [{"symbol": "TSLA", "status": "selling", "strategy": "news",
                                                       "broker_ref": {"ref": "TSLA_US_EQ"}}]})
        self.clock[0] = at(THANKSGIVING, 10, 0)
        code, text = self.watch()
        self.assertIn("US market holiday", text)
        self.assertEqual(self.router.posts(), [])

    def test_an_early_close_sells_at_1250_and_stops_at_1300(self):
        self.clock[0] = at(BLACK_FRIDAY, 12, 47)
        r = self.router
        r.add("GET", "/equity/positions", FakeResp([held(7)]), FakeResp([held(7)]), FakeResp([]))
        r.add("GET", "/equity/orders", FakeResp([STOP]), FakeResp([STOP]), FakeResp([]))
        r.add("DELETE", "/equity/orders/7001", FakeResp({}))
        r.add("POST", "/equity/orders/market", FakeResp({"id": 9300}))
        code, text = self.watch()
        self.assertEqual(code, 0)
        self.assertEqual(len(self.router.posts()), 1)                 # the one sale, before the 13:00 close
        self.assertIn("END-OF-DAY CLOSE", text)
        self.assertTrue(any(abs(s - 181) < 2 for s in self.sleeps))  # 12:47 -> woke at 12:50
        self.assertIn("past 13:00 New York: the guard stops", text)
        self.assertLess(self.clock[0], at(BLACK_FRIDAY, 13, 10))

    def test_a_normal_day_does_not_sell_at_1250(self):
        self.clock[0] = at(MONDAY, 12, 51)
        self.router.add("GET", "/equity/positions", FakeResp([held(7)]))
        self.router.add("GET", "/equity/orders", FakeResp([STOP]))
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            t212.main(["guard", "--watch", "--every", "5", "--until", "12:55", "--state-dir", self.dir],
                      environ=CREDS)
        self.assertNotIn("END-OF-DAY", out.getvalue())                # 12:51 on a normal day is not the close
        self.assertEqual(self.router.posts(), [])


class Autopilot(test_autopilot.PilotBase):
    def setUp(self):
        super().setUp()
        self.cfg = dict(self.cfg, eod_close={"enabled": True, "at": "15:50"})
        self.write("config.json", self.cfg)

    def test_a_full_holiday_is_a_closed_day(self):
        for day in (THANKSGIVING, CHRISTMAS, NEW_YEAR):
            self.clock[0] = at(day, 9, 0)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(self.pilot().day(), "closed")
        self.assertEqual(self.pipe.calls, [])
        self.assertEqual(self.router.posts(), [])

    def test_the_close_time_follows_an_early_close(self):
        p = self.pilot()
        self.clock[0] = at(BLACK_FRIDAY, 9, 0)
        self.assertEqual(p.close_uk(), "5:50pm")
        self.clock[0] = at(MONDAY, 9, 0)
        self.assertEqual(p.close_uk(), "8:50pm")

    def test_sales_after_1250_on_an_early_close_count_as_the_close(self):
        p = self.pilot()
        self.clock[0] = at(BLACK_FRIDAY, 12, 51)
        self.assertTrue(p.at_close("TSLA_US_EQ"))
        self.clock[0] = at(BLACK_FRIDAY, 12, 40)
        self.assertFalse(p.at_close("TSLA_US_EQ"))
        self.clock[0] = at(MONDAY, 12, 51)
        self.assertFalse(p.at_close("TSLA_US_EQ"))

    def test_the_evening_report_comes_at_1255_on_an_early_close(self):
        sent = []
        p = self.pilot()
        p.send_summary = lambda: (sent.append(ap._ny().strftime("%H:%M")), p.state.__setitem__("summary_sent", True))
        p.apply_budget = lambda: None
        self.clock[0] = at(BLACK_FRIDAY, 12, 56)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(p.day(), "after the close")
        self.assertEqual(sent, ["12:56"])
        sent.clear()
        p.state["summary_sent"] = False
        p.state["open_sent"] = p.state.get("day")
        self.clock[0] = at(MONDAY, 15, 56)                             # a normal day: at 15:55
        with redirect_stdout(io.StringIO()):
            self.assertEqual(p.day(), "after the close")
        self.assertEqual(sent, ["15:56"])

if __name__ == "__main__":
    unittest.main()
