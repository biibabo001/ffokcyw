#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
回测 wyckoff_screen 三个名单在历史时点的后续表现。

原理：对每只股票，在其自身历史中取多个"时点快照"，每个快照只用该时点之前的数据
使用选股器的规则分类，并从下一根日线开盘到第 N 根日线收盘计算收益。
（与 wyckoff_screen.analyze_one 的过选判定保持一致）

用法:
  python backtest.py --snapshots 6 --horizon 20 --limit 0 --threads 5
  python backtest.py --snapshots 12 --horizon 10 --no-cap-filter --limit 300
"""

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import wyckoff_screen as ws


def backtest_stock(hist, snapshots, horizon):
    """对单只股票在多个历史时点做回测。返回 [(flags, fwd_return), ...]。"""
    n = len(hist)
    usable = n - horizon - ws.MIN_BARS + 1
    if usable < 1:
        return []

    spacing = max(1, usable // snapshots)
    out = []

    for k in range(snapshots):
        as_of_idx = n - 1 - horizon - k * spacing
        if as_of_idx < ws.MIN_BARS - 1:
            break

        sub = hist.iloc[:as_of_idx + 1].reset_index(drop=True)

        x = ws.detect_structure(sub)
        flags = ws.classify_passes(x, *ws.score_lists(x))

        entry = float(hist["open"].iloc[as_of_idx + 1])
        exit_ = float(hist["close"].iloc[as_of_idx + horizon])
        fwd = exit_ / entry - 1

        out.append((flags, fwd))

    return out


def _work(code, snapshots, horizon, cache_only):
    if cache_only:
        hist = ws.normalize_history(pd.read_csv(ws.CACHE_DIR / f"{code}.csv"))
    else:
        hist = ws.fetch_history(code)
    if hist is None:
        return None
    return backtest_stock(hist, snapshots, horizon)


def main():
    parser = argparse.ArgumentParser(description="回测 wyckoff_screen 名单后续表现")
    parser.add_argument("--snapshots", type=int, default=6, help="每只股票的历史时点快照数")
    parser.add_argument("--horizon", type=int, default=20, help="持有交易日数")
    parser.add_argument("--limit", type=int, default=0, help="只回测前N只，0为全量")
    parser.add_argument("--threads", type=int, default=5)
    parser.add_argument("--no-cap-filter", action="store_true", help="跳过流通市值过滤")
    parser.add_argument("--min-cap", type=float, default=ws.CAP_MIN)
    parser.add_argument("--max-cap", type=float, default=ws.CAP_MAX)
    cache = parser.add_mutually_exclusive_group()
    cache.add_argument("--no-cache", action="store_true", help="忽略本地缓存")
    cache.add_argument("--cache-only", action="store_true", help="离线使用现有缓存，不筛选市值")
    args = parser.parse_args()
    if args.snapshots < 1 or args.horizon < 1:
        parser.error("--snapshots 和 --horizon 必须为正整数")

    ws.USE_CACHE = not args.no_cache

    print("=" * 66)
    print(f"  Wyckoff 名单回测 | 快照 {args.snapshots} | 持有 {args.horizon} 交易日")
    print("=" * 66)

    if args.cache_only:
        universe = [(path.stem, "") for path in sorted(ws.CACHE_DIR.glob("*.csv"))]
        if not universe:
            parser.error("本地没有行情缓存")
        print("  股票池：本地缓存，未应用市值过滤")
    else:
        universe = ws.get_stock_universe()
        print("  股票池：运行时的主板非ST股票")
        if not args.no_cap_filter:
            print("  市值过滤：运行时的流通市值")
            caps = ws.fetch_float_caps([c for c, _ in universe])
            universe = [
                (c, n) for c, n in universe
                if c in caps and args.min_cap <= caps[c] <= args.max_cap
            ]

    if args.limit > 0:
        universe = universe[:args.limit]
    print(f"  纳入回测：{len(universe)} 只")

    samples = {"main": [], "early": [], "lps": []}
    bench = []
    failed = 0
    done = 0

    with ThreadPoolExecutor(max_workers=max(1, args.threads)) as pool:
        futures = {
            pool.submit(_work, c, args.snapshots, args.horizon, args.cache_only): c
            for c, _ in universe
        }
        for fut in as_completed(futures):
            done += 1
            try:
                r = fut.result()
            except Exception:
                ws.LOG.exception("%s 回测失败", futures[fut])
                r = None
            if r is None:
                failed += 1
            else:
                for flags, fwd in r:
                    bench.append(fwd)
                    for key in samples:
                        if flags[key]:
                            samples[key].append(fwd)
            if done % 100 == 0 or done == len(universe):
                print(f"  进度 {done}/{len(universe)} 失败 {failed}", flush=True)

    print(f"\n  完成：{len(universe)} 只，失败 {failed}")

    def stats(arr):
        arr = np.asarray(arr, dtype=float)
        if len(arr) == 0:
            return None
        return {
            "n": len(arr),
            "win": float((arr > 0).mean() * 100),
            "avg": float(arr.mean() * 100),
            "med": float(np.median(arr) * 100),
        }

    names = {"main": "Spring+Test", "early": "Spring早期", "lps": "LPS"}
    print(f"\n  {'名单':<14}{'样本数':>7}{'胜率%':>9}{'平均收益%':>11}{'中位收益%':>11}")
    for key, label in names.items():
        s = stats(samples[key])
        if s is None:
            print(f"  {label:<14}{0:>7}{'-':>9}{'-':>11}{'-':>11}")
        else:
            print(
                f"  {label:<14}{s['n']:>7}{s['win']:>8.1f}%{s['avg']:>10.2f}%{s['med']:>10.2f}%"
            )

    b = stats(bench)
    if b:
        print(
            f"  {'全部快照':<14}{b['n']:>7}{b['win']:>8.1f}%{b['avg']:>10.2f}%{b['med']:>10.2f}%"
        )

    print(
        f"\n  说明：胜率=持有{args.horizon}交易日收益>0的样本占比；"
        "从信号后首根日线开盘到第 N 根日线收盘。未模拟涨跌停、停牌成交限制和交易费用。"
    )


if __name__ == "__main__":
    main()
