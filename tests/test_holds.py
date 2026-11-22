"""The Time Machine for held trades (trader-timemachine/holds.py): daily bars, offline, made-up prices."""

import datetime
import unittest

from _load import load

holds = load("trader-timemachine", "holds")
drift = load("trader-broker", "drift")


def flat(n, px=100.0, rng=1.0):
    return [(px, px + rng, px - rng, px) for _ in range(n)]


class Walk(unittest.TestCase):
    def test_sold_on_the_date_with_costs_each_way(self):
        rows = [(100, 101, 99, 100), (100, 103, 99, 102), (102, 106, 101, 105)]
        w = holds.walk(rows, 0, 2, "fixed", dist=10)
        self.assertEqual((w["why"], w["exit_i"], w["exit_px"]), ("date", 2, 105))
        self.assertAlmostEqual(w["net"], 105 * 0.998 / (100 * 1.002) - 1)

    def test_a_low_at_the_stop_sells_at_the_stop(self):
        rows = [(100, 101, 99, 100), (100, 100, 89, 95), (95, 120, 94, 118)]
        w = holds.walk(rows, 0, 2, "fixed", dist=10)
        self.assertEqual((w["why"], w["exit_i"], w["exit_px"]), ("stop", 1, 90))

    def test_a_gap_under_the_stop_sells_at_the_open(self):
        rows = [(100, 101, 99, 100), (80, 85, 78, 84)]
        w = holds.walk(rows, 0, 1, "fixed", dist=10)
        self.assertEqual((w["why"], w["exit_px"]), ("stop", 80))

    def test_the_buy_day_itself_can_hit_the_stop(self):
        w = holds.walk([(100, 100, 85, 95), (95, 96, 94, 95)], 0, 1, "fixed", dist=10)
        self.assertEqual((w["why"], w["exit_i"], w["exit_px"]), ("stop", 0, 90))

    def test_no_stop_ignores_every_fall(self):
        w = holds.walk([(100, 100, 50, 60), (60, 61, 40, 45)], 0, 1, "none")
        self.assertEqual((w["why"], w["exit_px"]), ("date", 45))

    def test_the_rising_stop_follows_the_highest_close_and_never_drops(self):
        rows = [(100, 101, 99, 100), (100, 121, 100, 120), (120, 121, 115, 116), (116, 117, 109, 112)]
        fixed = holds.walk(rows, 0, 3, "fixed", dist=10)
        rising = holds.walk(rows, 0, 3, "rising", dist=10)
        self.assertEqual(fixed["why"], "date")
        self.assertEqual((rising["why"], rising["exit_i"], rising["exit_px"]), ("stop", 3, 110))   # 120 - 10

    def test_an_explicit_stop_and_bad_rules(self):
        self.assertEqual(holds.walk([(100, 101, 94, 96)], 0, 0, "fixed", stop=95)["exit_px"], 95)
        with self.assertRaises(ValueError):
            holds.walk(flat(3), 0, 2, "fixed")
        with self.assertRaises(ValueError):
            holds.walk(flat(3), 0, 2, "trailing", dist=1)


class Atr(unittest.TestCase):
    def test_the_same_as_the_live_drift_atr_and_only_the_days_before(self):
        rows = [(100 + i, 102 + i + (i % 3), 98 + i - (i % 2), 101 + i) for i in range(30)]
        d0 = datetime.date(2026, 1, 1)
        bars = {d0 + datetime.timedelta(days=i): r + (0,) for i, r in enumerate(rows)}
        self.assertAlmostEqual(holds.atr(rows, 20), drift.atr(bars, d0 + datetime.timedelta(days=20), 14))
        changed = list(rows)
        changed[20] = (1, 999, 0.5, 2)                           # the buy day itself never counts
        self.assertEqual(holds.atr(changed, 20), holds.atr(rows, 20))
        self.assertIsNone(holds.atr(rows, 14))


class NoiseTest(unittest.TestCase):
    def history(self, n=120):
        import random
        r, px, out = random.Random(3), 100.0, []
        for _ in range(n):
            o = px * (1 + r.uniform(-0.01, 0.01))
            c = o * (1 + r.uniform(-0.02, 0.02))
            out.append((o, max(o, c) * 1.01, min(o, c) * 0.99, c))
            px = c
        return out

    def test_repeatable_and_complete(self):
        h = self.history()
        a = holds.noise_test(h, 100.0, 20, runs=200)
        self.assertEqual(a, holds.noise_test(h, 100.0, 20, runs=200))
        self.assertEqual(set(a["rules"]), {"fixed", "rising", "none"})
        self.assertEqual(a["rules"]["none"]["stopped_pct"], 0.0)
        self.assertEqual(a["stop_first_pct"], a["rules"]["fixed"]["stopped_pct"])
        self.assertGreaterEqual(a["rules"]["rising"]["stopped_pct"], a["rules"]["fixed"]["stopped_pct"])
        self.assertIn("replays of its own past ups and downs", holds.words(a))

    def test_the_past_trend_is_taken_out(self):
        up = [(100 * 1.01 ** i, 100 * 1.01 ** i * 1.005, 100 * 1.01 ** i * 0.995, 100 * 1.01 ** i) for i in range(100)]
        mv = holds.moves(up, detrend=True)
        self.assertAlmostEqual(mv[5][3], 1.0)
        self.assertAlmostEqual(holds.moves(up)[5][3], 1.01)

    def test_a_tight_stop_is_hit_more_often(self):
        h = self.history()
        wide = holds.noise_test(h, 100.0, 20, stop=80.0, runs=200)
        tight = holds.noise_test(h, 100.0, 20, stop=99.0, runs=200)
        self.assertGreater(tight["stop_first_pct"], wide["stop_first_pct"])

    def test_refuses_without_history_or_a_stop_under_the_price(self):
        with self.assertRaises(ValueError):
            holds.noise_test(self.history(30), 100.0, 20)
        with self.assertRaises(ValueError):
            holds.noise_test(self.history(), 100.0, 20, stop=101.0)
        with self.assertRaises(ValueError):
            holds.noise_test(self.history(), 100.0, 0)

    def test_the_path_opens_at_the_entry(self):
        import random
        p = holds.path(holds.moves(self.history()), 7, random.Random(1), 50.0)
        self.assertEqual(len(p), 7)
        self.assertAlmostEqual(p[0][0], 50.0)
        self.assertEqual(holds.words(None), "no replay (none yet)")


if __name__ == "__main__":
    unittest.main()
