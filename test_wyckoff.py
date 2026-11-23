import unittest

import numpy as np
import pandas as pd

import backtest
from bottom_base import count_round_trips
import wyckoff_screen as screen


def history_with_range():
    close = np.concatenate([
        np.linspace(126, 130, 100),
        np.linspace(130, 105, 40),
        105 + 4 * np.sin(np.arange(180) * np.pi / 6),
    ])
    return pd.DataFrame({
        "date": pd.date_range("2024-01-01", periods=len(close)),
        "open": close,
        "close": close,
        "high": close + 0.4,
        "low": close - 0.4,
        "volume": 100.0,
    })


def append_bars(history, rows):
    bars = pd.DataFrame(rows, columns=["open", "close", "high", "low", "volume"])
    bars["date"] = pd.date_range(
        history["date"].iloc[-1] + pd.Timedelta(days=1), periods=len(bars),
    )
    return pd.concat([history, bars], ignore_index=True)


class StructureTests(unittest.TestCase):
    def test_bottom_base_requires_repeated_traversals(self):
        self.assertEqual(count_round_trips(np.linspace(12, 10, 90), 10, 12), 0)
        drift_then_bounce = np.r_[np.linspace(12, 10, 60), np.linspace(10, 11, 30)]
        self.assertEqual(count_round_trips(drift_then_bounce, 10, 12), 0)
        prices = np.repeat([10, 11, 12, 11, 10, 11, 12, 11, 10], 5)
        self.assertEqual(count_round_trips(prices, 10, 12), 2)

    def test_range_cannot_include_a_sustained_support_break(self):
        history = history_with_range()
        for days_below, expected in [(1, True), (5, False)]:
            with self.subTest(days_below=days_below):
                candidate = history.copy()
                candidate.loc[215:214 + days_below, ["open", "high", "low", "close"]] = [
                    95.0, 95.4, 94.6, 95.0,
                ]
                result = screen.detect_structure(candidate)
                self.assertTrue(result["in_range"])
                self.assertEqual(result["range_quality"], expected)

    def test_prior_drawdown_uses_two_years_before_range_start(self):
        history = history_with_range()
        close = np.r_[np.full(550, 115.0), history["close"].to_numpy()]
        high, low = close + 0.4, close - 0.4
        dates = pd.bdate_range("2023-01-02", periods=len(close))
        range_start = len(close) - 180
        high[range_start - 540] = 250.0  # 在两年前，不能作为高点。
        high[range_start - 300] = 150.0  # 超过旧的 120 根日线上限。
        opening_level = np.median(close[range_start:range_start + 5])

        context = screen._range_context(
            high, low, close, np.full(len(close), 100.0), len(close), dates,
        )
        self.assertEqual(context.range_start, range_start)
        self.assertEqual(context.decline_days, 300)
        self.assertAlmostEqual(context.drawdown_pct, (opening_level / 150 - 1) * 100)
        self.assertTrue(context.range_quality)

    def test_round_trip_rally_is_not_one_stable_range(self):
        close = np.repeat([100.0, 118.0, 100.0], [100, 60, 80])
        start, days, _, _, _, found = screen._find_trading_range(
            close + 0.4, close - 0.4, close, len(close),
        )
        self.assertTrue(found)
        self.assertLess(days, 180)
        self.assertGreater(start, 100)

    def test_sos_and_lps_do_not_move_the_breakout_level(self):
        history = history_with_range()
        breakout = len(history)
        bars = [
            (108, 110.5, 110.7, 107.8, 200),
            (110.5, 111, 111.2, 110.2, 100),
            (111, 110.8, 111.2, 110.4, 100),
            (110, 109.6, 110.2, 109, 75),
            (109.6, 112, 112.2, 109.5, 100),
            (110, 104, 110.3, 103.7, 100),
            (107, 111, 111.2, 107, 100),
        ]
        resistance = screen.detect_structure(history)["resistance"]
        for count, status in [(1, ""), (4, "观察中"), (5, "已确认"), (7, "失效")]:
            with self.subTest(status=status):
                result = screen.detect_structure(append_bars(history, bars[:count]))
                self.assertEqual(result["sos_idx"], breakout)
                self.assertEqual(result["range_end"], breakout - 1)
                self.assertAlmostEqual(result["sos_resistance"], resistance)
                self.assertEqual(result["lps_status"], status)
                self.assertEqual(result["sos_valid"], count in (4, 5))
                self.assertEqual(result["lps_valid"], count == 5)

    def test_spring_keeps_its_original_support(self):
        history = history_with_range()
        bars = [(102, 102, 102.3, 99.7, 90), (102, 104, 104.5, 101.8, 100)]
        on_event = screen.detect_structure(append_bars(history, bars[:1]))
        after_event = screen.detect_structure(append_bars(history, bars))
        self.assertEqual(on_event["spring_idx"], len(history))
        self.assertEqual(after_event["spring_idx"], len(history))
        self.assertEqual(after_event["range_end"], len(history) - 1)
        self.assertEqual(after_event["support"], on_event["support"])

    def test_backtest_uses_next_open_with_one_available_snapshot(self):
        history = history_with_range().tail(screen.MIN_BARS).reset_index(drop=True)
        history = append_bars(history, [(120, 126, 127, 119, 100)])
        samples = backtest.backtest_stock(history, snapshots=1, horizon=1)
        self.assertEqual(len(samples), 1)
        self.assertAlmostEqual(samples[0][1], 0.05)
