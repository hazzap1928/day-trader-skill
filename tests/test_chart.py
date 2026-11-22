"""Chart labels at the buy (trader-scanner/chart.py): daily closes before the buy day only. Made-up prices."""

import unittest

from _load import load

chart = load("trader-scanner", "chart")


class Labels(unittest.TestCase):
    def test_a_steady_rise_is_an_uptrend_and_not_falling(self):
        lab = chart.labels([100 * 1.004 ** i for i in range(80)])
        self.assertIn("uptrend", lab["tags"])
        self.assertIn("breakout_20d", lab["tags"])                 # the old scanner's own pattern, reused
        self.assertFalse(lab["falling"])
        self.assertEqual(lab["version"], 1)

    def test_a_collapse_is_falling_apart(self):
        closes = [100.0] * 70 + [99, 97, 95, 92, 88, 84]
        lab = chart.labels(closes)
        for t in ("below_ma50", "down_10pct_5d", "new_20d_low"):
            self.assertIn(t, lab["tags"])
        self.assertTrue(lab["falling"])
        self.assertAlmostEqual(lab["nums"]["change_5d_pct"], 100 * (84 / 99 - 1), places=3)
        self.assertIn("down 10% or more in the last week", chart.words(lab))

    def test_flat_has_no_trend_label(self):
        lab = chart.labels([100.0 + (i % 2) * 0.1 for i in range(60)])
        self.assertNotIn("uptrend", lab["tags"])
        self.assertFalse(lab["falling"])

    def test_needs_enough_history(self):
        with self.assertRaises(ValueError):
            chart.labels([100.0] * 50)
        self.assertEqual(chart.words(None), "no chart labels")


if __name__ == "__main__":
    unittest.main()
