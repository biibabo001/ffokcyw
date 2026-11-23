"""寻找大涨、深跌后进入稳定横盘的主板股票。"""

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import numpy as np
import pandas as pd

@dataclass(frozen=True)
class BaseMatch:
    peak_date: str
    peak: float
    rally_pct: float
    drawdown_pct: float
    start_date: str
    days: int
    lower: float
    upper: float
    close: float
    round_trips: int


def count_round_trips(close, lower, upper):
    """统计收盘价在区间上下各 20% 区域之间完成的往返次数。"""
    band = (upper - lower) * 0.20
    if band <= 0:
        return 0
    last_side = 0
    last_touch = -5
    crossings = 0
    for index, price in enumerate(close):
        side = -1 if price <= lower + band else 1 if price >= upper - band else 0
        if side == 0 or side == last_side or index - last_touch < 5:
            continue
        if last_side:
            crossings += 1
        last_side = side
        last_touch = index
    return crossings // 2


def detect_base(history):
    """使用当前及以前的行情，寻找最早通过条件的底部整理起点。"""
    close = history["close"].to_numpy(dtype=float)
    highs = history["high"].to_numpy(dtype=float)
    lows = history["low"].to_numpy(dtype=float)
    dates = pd.DatetimeIndex(history["date"])
    n = len(close)

    for start in range(max(60, n - 120), n - 60 + 1):
        opening = np.median(close[start:start + 10])
        previous = np.median(close[start - 10:start])
        earlier = np.median(close[start - 40:start - 20])
        # 前一段仍在下跌，进入候选区间时价格中枢已经接近持平。
        if opening > earlier * 0.90 or abs(opening / previous - 1) > 0.04:
            continue

        base = close[start:]
        lower, upper = np.quantile(base, [0.10, 0.90])
        if upper / lower - 1 > 0.25 or not lower * 0.97 <= base[-1] <= upper * 1.03:
            continue
        centers = [np.median(block) for block in np.array_split(base, 3)]
        if max(centers) / min(centers) - 1 > 0.15:
            continue
        below = base < lower * 0.97
        if np.any(below[:-2] & below[1:-1] & below[2:]):
            continue
        round_trips = count_round_trips(base, lower, upper)
        if round_trips < 2:
            continue

        peak_start = dates.searchsorted(dates[start] - pd.DateOffset(years=2))
        if peak_start >= start:
            continue
        peak_idx = peak_start + int(np.argmax(highs[peak_start:start]))
        peak = highs[peak_idx]
        if start - peak_idx < 60 or opening / peak > 0.55 or peak_idx == 0:
            continue
        before_peak = lows[max(0, peak_idx - 120):peak_idx]
        rally_pct = (peak / before_peak.min() - 1) * 100
        if rally_pct < 100:
            continue

        return BaseMatch(
            peak_date=dates[peak_idx].date().isoformat(),
            peak=float(peak),
            rally_pct=float(rally_pct),
            drawdown_pct=float((1 - opening / peak) * 100),
            start_date=dates[start].date().isoformat(),
            days=n - start,
            lower=float(lower),
            upper=float(upper),
            close=float(base[-1]),
            round_trips=round_trips,
        )
    return None


def main(argv=None):
    import wyckoff_screen as screen

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--code", help="只检查一只股票，例如 601929")
    parser.add_argument("--cache-only", action="store_true", help="只使用本地日线缓存")
    parser.add_argument("--threads", type=int, default=5, help="并发线程数")
    args = parser.parse_args(argv)
    if args.code and (len(args.code) != 6 or not args.code.isascii() or not args.code.isdecimal()):
        parser.error("--code 请输入 6 位数字股票代码")
    if args.threads < 1:
        parser.error("--threads 必须大于 0")

    if args.code:
        universe = [(args.code, "")]
    elif args.cache_only:
        universe = [(path.stem, "") for path in sorted(screen.CACHE_DIR.glob("[0-9]" * 6 + ".csv"))]
    else:
        universe = screen.get_stock_universe()
    if not universe:
        parser.error("没有可扫描的股票")

    def scan(stock):
        code, name = stock
        try:
            if args.cache_only:
                data = screen.normalize_history(pd.read_csv(screen.CACHE_DIR / f"{code}.csv"))
            else:
                data = screen.fetch_history(code, refresh=True)
            if data is not None:
                return code, name, data["date"].iloc[-1].date(), detect_base(data)
        except Exception:
            screen.LOG.exception("%s 底部横盘扫描失败", code)
        return code, name, None, None

    mode = "本地缓存" if args.cache_only else "沪深主板非ST"
    print(f"开始扫描 {mode} {len(universe)} 只…", flush=True)
    results = []
    with ThreadPoolExecutor(max_workers=args.threads) as pool:
        for count, result in enumerate(pool.map(scan, universe), 1):
            results.append(result)
            if count % 100 == 0:
                print(f"进度 {count}/{len(universe)}", flush=True)

    matches = sorted(
        (row for row in results if row[3] is not None),
        key=lambda row: (row[2], row[3].days),
        reverse=True,
    )
    unavailable = sum(row[2] is None for row in results)
    print(f"{mode}扫描 {len(results)} 只，符合 {len(matches)} 只，数据不可用 {unavailable} 只。")
    date_counts = Counter(row[2] for row in results if row[2] is not None)
    if date_counts:
        latest = max(date_counts)
        older = sum(date_counts.values()) - date_counts[latest]
        print(f"本次最新日线 {latest}（{date_counts[latest]} 只），另有 {older} 只日线早于该日期。")
    for code, name, as_of, match in matches:
        print(
            f"{code} {name} | 高点 {match.peak_date} {match.peak:.2f}，"
            f"前涨 {match.rally_pct:.1f}%，后跌 {match.drawdown_pct:.1f}% | "
            f"横盘约从 {match.start_date} 开始，共 {match.days} 根日线，"
            f"主要收盘区间 {match.lower:.2f}–{match.upper:.2f}，往返 {match.round_trips} 次，"
            f"{as_of} 收盘 {match.close:.2f}"
        )
