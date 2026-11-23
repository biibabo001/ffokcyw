import unittest

import numpy as np
import pandas as pd

import backtest
import wyckoff_screen as screen


def history_with_range():
    close = np.concatenate([
        np.full(100, 125.0),
        np.linspace(125, 105, 40),
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
