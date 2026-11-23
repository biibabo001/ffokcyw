#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
A股 Wyckoff 早期吸筹机会筛选器 4.1

功能：
1. 获取沪深主板股票日线数据
2. 可选流通市值过滤
3. 识别交易区间、Spring、Test、SOS、LPS
4. 输出深跌后横盘、Spring+Test、Spring早期、LPS 四个名单
5. 输出排除名单和全量扫描结果
6. 导出 Excel
7. 生成候选股票 K 线图

说明：
这是基于价格、成交量和事件顺序的规则型筛选工具。
筛选结果是候选结构，不代表确定存在主力吸筹行为。
参数尚未经过完整历史回测，不应直接作为买卖依据。
"""

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import NamedTuple
from concurrent.futures import ThreadPoolExecutor, as_completed

os.environ.setdefault("TQDM_DISABLE", "1")

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(errors="replace")
    except Exception:
        pass

import numpy as np
import pandas as pd
import requests
import akshare as ak

from bottom_base import detect_base


# ============================================================
# 一、基础配置
# ============================================================

ROOT = Path(__file__).resolve().parent

CACHE_DIR = ROOT / "cache"
USE_CACHE = True

MAIN_PREFIXES = (
    "600", "601", "603", "605",
    "000", "001", "002", "003",
)

# 行情还要覆盖交易区间及其后的事件，为前期高点留足两年回看。
HISTORY_DAYS = 4 * 366
MIN_BARS = 250
HISTORY_OVERLAP_BARS = 5

CAP_MIN = 30.0
CAP_MAX = 300.0

VERSION = "4.1"

# 参数配置
P = {
    # 交易区间
    "range_min_days": 60,
    "range_max_days": 180,
    "range_max_height_pct": 25.0,
    "range_max_endpoint_change_pct": 30.0,
    "range_max_internal_change_pct": 15.0,

    # 支撑和阻力
    "support_tolerance": 0.03,
    "resistance_tolerance": 0.03,
    "support_min_touches": 3,
    "sos_support_min_touches": 2,
    "resistance_min_touches": 3,
    "range_support_break_days": 3,

    # 前期下跌背景
    "min_prior_drawdown_pct": 15.0,
    "max_prior_drawdown_pct": 60.0,
    "decline_min_days": 30,
    "decline_lookback_years": 2,

    # Spring
    "spring_recent_days": 30,
    "early_spring_recent_days": 15,
    "spring_penetration_min_pct": 0.5,
    "spring_penetration_max_pct": 3.5,
    "spring_min_close_pos": 0.65,
    "spring_max_volume_vs_avg": 1.8,

    # Test
    "test_min_gap": 3,
    "test_max_gap": 20,
    "test_min_rebound_pct": 3.0,
    "test_max_volume_ratio": 0.80,
    "test_support_tolerance_pct": 3.5,
    "test_min_close_pos": 0.50,

    # Spring+Test当前价格位置
    "main_max_spring_distance_pct": 8.0,
    "main_support_distance_min_pct": -1.0,
    "main_support_distance_max_pct": 4.0,

    # Spring早期观察
    "early_max_spring_distance_pct": 5.0,
    "early_support_distance_min_pct": -1.0,
    "early_support_distance_max_pct": 4.0,

    # SOS
    "sos_recent_days": 60,
    "sos_min_breakout_pct": 0.5,
    "sos_max_breakout_pct": 2.5,
    "sos_min_volume_ratio": 1.30,
    "sos_min_close_pos": 0.70,
    "sos_failure_tolerance": 0.03,

    # LPS
    "lps_min_gap": 3,
    "lps_max_gap": 25,
    "lps_max_volume_ratio": 0.80,
    "lps_level_tolerance_pct": 3.0,
    "lps_current_max_distance_pct": 5.0,
    "lps_failure_tolerance": 0.02,

    # Spring失效
    "spring_invalid_close_pct": 1.5,
    "spring_invalid_atr_k": 0.3,

    # 评分
    "score_threshold": 80,

    # 评分权重（各名单独立评分，权重可回测调优）
    "score_weights": {
        "common_range_quality": 15,
        "common_vol_contract_strong": 10,
        "common_vol_contract_weak": 5,
        "spring_event": 25,
        "spring_rebound": 15,
        "spring_test_low_vol": 20,
        "spring_not_invalid": 10,
        "macd_turn": 5,
        "early_spring_event": 30,
        "early_recent_days": 20,
        "early_spring_distance": 15,
        "early_support_distance": 15,
        "lps_range": 20,
        "lps_sos": 25,
        "lps_valid": 30,
        "lps_low_vol": 15,
        "lps_ma20_up": 5,
        "volume_divergence": 5,
        "higher_low": 5,
    },
}


# ============================================================
# 二、日志
# ============================================================

logging.basicConfig(
    filename=ROOT / "wyckoff_screen.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    encoding="utf-8",
)

LOG = logging.getLogger("wyckoff")


# ============================================================
# 三、数据获取
# ============================================================

def tencent_symbol(code):
    code = str(code).zfill(6)

    if code.startswith(("6", "9", "5")):
        return "sh" + code

    return "sz" + code


def get_stock_universe():
    """
    获取沪深主板股票列表。
    排除ST和退市整理股票。
    """

    df = ak.stock_info_a_code_name().copy()

    df["code"] = df["code"].astype(str).str.zfill(6)
    df["name"] = df["name"].astype(str)

    df = df[
        df["code"].str.startswith(MAIN_PREFIXES)
    ]

    df = df[
        ~df["name"].str.contains(
            r"ST|退",
            case=False,
            na=False,
        )
    ]

    return list(
        zip(
            df["code"].tolist(),
            df["name"].tolist(),
        )
    )


def fetch_float_caps(codes, pause=0.15):
    """
    获取腾讯行情中的流通市值数据。
    """

    caps = {}
    session = requests.Session()

    for start in range(0, len(codes), 60):

        chunk = codes[start:start + 60]

        url = (
            "https://qt.gtimg.cn/q="
            + ",".join(
                tencent_symbol(code)
                for code in chunk
            )
        )

        try:
            resp = session.get(
                url,
                timeout=12,
            )

            resp.raise_for_status()
            resp.encoding = "gbk"

            for line in resp.text.split(";"):

                if '="' not in line:
                    continue

                parts = (
                    line.split("=", 1)[1]
                    .strip()
                    .strip('"')
                    .split("~")
                )

                if len(parts) <= 44:
                    continue

                code = parts[2].strip()

                try:
                    value = float(parts[44])

                    if (
                        code
                        and np.isfinite(value)
                        and value > 0
                    ):
                        caps[code] = value

                except (TypeError, ValueError):
                    continue

        except Exception as exc:
            LOG.warning(
                "腾讯市值批次获取失败：%s",
                exc,
            )

        time.sleep(pause)

    return caps


def normalize_history(df, *, min_bars=MIN_BARS):
    """
    统一日线数据字段。
    """

    if df is None or df.empty:
        return None

    d = df.copy()

    d.columns = [
        str(c).strip().lower()
        for c in d.columns
    ]

    rename = {
        "日期": "date",
        "开盘": "open",
        "收盘": "close",
        "最高": "high",
        "最低": "low",
        "成交量": "volume",
        "成交额": "amount",
        "换手率": "turnover",
    }

    d = d.rename(
        columns={
            k: v
            for k, v in rename.items()
            if k in d.columns
        }
    )

    required = {
        "open",
        "close",
        "high",
        "low",
    }

    if not required.issubset(d.columns):
        LOG.warning(
            "日线缺少必要字段：%s",
            list(d.columns),
        )
        return None

    if "volume" not in d.columns:

        if "amount" in d.columns:
            d["volume"] = (
                pd.to_numeric(
                    d["amount"],
                    errors="coerce",
                )
                /
                pd.to_numeric(
                    d["close"],
                    errors="coerce",
                )
            )
        else:
            return None

    for col in (
        "open",
        "close",
        "high",
        "low",
        "volume",
    ):
        d[col] = pd.to_numeric(
            d[col],
            errors="coerce",
        )

    if "date" in d.columns:
        d["date"] = pd.to_datetime(
            d["date"],
            errors="coerce",
        )

        d = d.sort_values("date")

    d = d.dropna(
        subset=[
            "open",
            "close",
            "high",
            "low",
            "volume",
        ]
    )

    d = d[
        (d["close"] > 0)
        & (d["low"] > 0)
        & (d["open"] > 0)
        & (d["volume"] >= 0)
    ]

    if "date" in d.columns:
        d = d.drop_duplicates(
            subset=["date"]
        )

    if len(d) < min_bars:
        return None

    return d.reset_index(drop=True)


def _request_history(code, start_day, end_day, *, min_bars=MIN_BARS):
    raw = ak.stock_zh_a_hist_tx(
        symbol=tencent_symbol(code),
        start_date=start_day.strftime("%Y%m%d"),
        end_date=end_day.strftime("%Y%m%d"),
        adjust="qfq",
    )
    history = normalize_history(raw, min_bars=min_bars)
    if history is None or "date" not in history:
        return None
    history = history.loc[history["date"].between(pd.Timestamp(start_day), pd.Timestamp(end_day))]
    return history.reset_index(drop=True) if len(history) >= min_bars else None


def _merge_recent_history(cached, recent, today):
    if recent is None:
        return None
    overlap = cached.tail(HISTORY_OVERLAP_BARS).set_index("date")
    incoming = recent.set_index("date")
    # 当天的日线可能从盘中状态更新到收盘，之前的数据才用来判断复权口径。
    confirmed = overlap.loc[overlap.index < pd.Timestamp(today)]
    prices = ["open", "high", "low", "close"]
    if not overlap.index.isin(incoming.index).all() or not np.allclose(
        confirmed[prices], incoming.loc[confirmed.index, prices], atol=1e-6, rtol=0,
    ):
        return None
    return pd.concat([cached.iloc[:-len(overlap)], recent], ignore_index=True)


def _save_history_cache(cache_path, history, start_day):
    temp_path = None
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(
            mode="w", dir=cache_path.parent, suffix=".tmp",
            encoding="utf-8", newline="", delete=False,
        ) as target:
            temp_path = Path(target.name)
            history.assign(history_request_start=start_day.isoformat()).to_csv(target, index=False)
        temp_path.replace(cache_path)
    except OSError as exc:
        LOG.warning("%s 缓存写入失败：%s", cache_path.name, exc)
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


def fetch_history(code, *, refresh=False):
    """复用历史缓存并补齐最近前复权日线。refresh=True 总会查询行情源。"""
    cache_path = CACHE_DIR / f"{code}.csv"
    now = datetime.now()
    today = now.date()
    market_close = now.replace(hour=15, minute=0, second=0, microsecond=0)
    after_close = today.weekday() < 5 and now >= market_close
    start_day = today - timedelta(days=HISTORY_DAYS)

    cached = None
    if USE_CACHE and cache_path.exists():
        try:
            saved_at = datetime.fromtimestamp(cache_path.stat().st_mtime)
            raw_cache = pd.read_csv(cache_path)
            if (
                not raw_cache.empty
                and "history_request_start" in raw_cache
                and pd.Timestamp(raw_cache["history_request_start"].iat[0]) <= pd.Timestamp(start_day)
            ):
                history = normalize_history(raw_cache.drop(columns="history_request_start"))
                if history is not None and "date" in history:
                    history = history.loc[
                        history["date"].between(pd.Timestamp(start_day), pd.Timestamp(today))
                    ].reset_index(drop=True)
                    if len(history) >= MIN_BARS:
                        cached = history
        except (OSError, ValueError, TypeError):
            pass

    if cached is not None and not refresh and saved_at.date() == today and (
        not after_close or saved_at >= market_close and (
            cached["date"].iat[-1].date() == today
            or now - saved_at < timedelta(minutes=10)
        )
    ):
        return cached

    history = None
    if cached is not None:
        recent_start = cached["date"].iat[-HISTORY_OVERLAP_BARS].date()
        try:
            recent = _request_history(code, recent_start, today, min_bars=1)
            history = _merge_recent_history(cached, recent, today)
        except Exception as exc:
            LOG.info("%s 增量日线获取失败，尝试重新拉取历史：%s", code, exc)

    if history is None:
        try:
            history = _request_history(code, start_day, today)
        except Exception as exc:
            LOG.warning("%s 行情获取失败：%s", code, exc)
            return None

    if history is not None and USE_CACHE:
        _save_history_cache(cache_path, history, start_day)
    return history


# ============================================================
# 四、基础指标函数
# ============================================================

def local_pivots(
    values,
    left=3,
    right=3,
    mode="low",
):
    """
    寻找局部高低点。
    """

    arr = np.asarray(
        values,
        dtype=float,
    )

    out = []

    for i in range(
        left,
        len(arr) - right,
    ):

        window = arr[
            i - left:i + right + 1
        ]

        if mode == "low":
            if arr[i] <= np.min(window):
                out.append(i)

        elif mode == "high":
            if arr[i] >= np.max(window):
                out.append(i)

    return out


def cluster_price_levels(
    levels,
    tolerance=0.03,
):
    """
    将相近价格聚类，识别支撑或阻力区域。
    """

    vals = sorted(
        float(x)
        for x in levels
        if np.isfinite(x) and x > 0
    )

    clusters = []

    for value in vals:

        if not clusters:
            clusters.append([value])
            continue

        median = np.median(
            clusters[-1]
        )

        if abs(value / median - 1) > tolerance:
            clusters.append([value])
        else:
            clusters[-1].append(value)

    if not clusters:
        return None, 0

    best = max(
        clusters,
        key=len,
    )

    return (
        float(np.median(best)),
        len(best),
    )


def pct(a, b):
    if (
        b is None
        or not np.isfinite(b)
        or b == 0
    ):
        return np.nan

    return (a / b - 1) * 100


def spaced_pivots(items, gap=5):
    result = []

    for idx in items:

        if (
            not result
            or idx - result[-1] >= gap
        ):
            result.append(idx)

    return result


# ============================================================
# 五、Wyckoff结构识别（子函数）
# ============================================================

def _find_trading_range(high, low, close, n):
    """在历史中寻找最近的合格交易区间。返回 (start, days, low, high, height_pct, in_range)。"""

    range_info = None

    max_len = min(
        P["range_max_days"],
        n - 40,
    )

    starts = np.arange(n - max_len, n - P["range_min_days"] + 1)
    if len(starts):
        smoothed = np.lib.stride_tricks.sliding_window_view(close[starts[0]:], 20).mean(axis=1)
        window_starts = starts - starts[0]
        highest = np.maximum.accumulate(smoothed[::-1])[::-1][window_starts]
        lowest = np.minimum.accumulate(smoothed[::-1])[::-1][window_starts]
        endpoint_change = np.abs(close[-1] / close[starts] - 1) * 100
        internal_change = (highest / lowest - 1) * 100
        starts = starts[
            (endpoint_change <= P["range_max_endpoint_change_pct"])
            & (internal_change <= P["range_max_internal_change_pct"])
        ]

    for start in starts:
        range_low = float(np.quantile(low[start:], 0.10))
        range_high = float(np.quantile(high[start:], 0.90))
        if range_low <= 0 or range_high <= range_low:
            continue

        height_pct = (range_high / range_low - 1) * 100
        if height_pct <= P["range_max_height_pct"]:
            range_info = (int(start), n - int(start), range_low, range_high, height_pct)
            break

    if range_info is None:

        range_start = n - min(
            P["range_max_days"],
            n,
        )

        range_days = 0

        range_low = float(
            np.quantile(
                low[range_start:],
                0.10,
            )
        )

        range_high = float(
            np.quantile(
                high[range_start:],
                0.90,
            )
        )

        range_height_pct = (
            (range_high / range_low - 1) * 100
            if range_low > 0
            else 999
        )

        in_range = False

    else:

        (
            range_start,
            range_days,
            range_low,
            range_high,
            range_height_pct,
        ) = range_info

        in_range = True

    return (
        range_start,
        range_days,
        range_low,
        range_high,
        range_height_pct,
        in_range,
    )


def _detect_test(close, high, low, vol, support, spring_idx, range_start, n):
    """识别 Test：Spring 后先反弹、再缩量回踩支撑。返回 (test_idx, rebound_pct)。"""

    test_idx = None
    rebound_pct = np.nan

    test_start = (
        spring_idx
        + P["test_min_gap"]
    )

    test_end = min(
        n,
        spring_idx
        + P["test_max_gap"]
        + 1,
    )

    for j in range(
        test_start,
        test_end,
    ):

        if j <= spring_idx + 1:
            continue

        interim_high = float(
            np.max(
                high[spring_idx + 1:j]
            )
        )

        rebound = pct(
            interim_high,
            low[spring_idx],
        )

        if rebound < P["test_min_rebound_pct"]:
            continue

        support_distance = abs(
            pct(
                low[j],
                support,
            )
        )

        near_support = (
            support_distance
            <= P["test_support_tolerance_pct"]
        )

        holds_spring_low = (
            low[j]
            >= low[spring_idx] * 0.995
        )

        lower_than_spring = (
            vol[j]
            <= vol[spring_idx]
            * P["test_max_volume_ratio"]
        )

        prior_volume = float(
            np.mean(
                vol[
                    max(range_start, j - 20):j
                ]
            )
        )

        volume_contracted = (
            prior_volume > 0
            and vol[j]
            <= prior_volume
            * P["test_max_volume_ratio"]
        )

        candle_range = max(
            high[j] - low[j],
            1e-9,
        )

        close_position = (
            (close[j] - low[j])
            / candle_range
        )

        stable_close = (
            close[j] >= support * 0.99
            and close_position
            >= P["test_min_close_pos"]
        )

        if (
            near_support
            and holds_spring_low
            and lower_than_spring
            and volume_contracted
            and stable_close
        ):

            test_idx = j
            rebound_pct = rebound

            break

    return test_idx, rebound_pct


def _atr(high, low, close, period=14):
    """ATR（真实波幅的指数移动平均），用于动态失效与突破质量。"""
    h = np.asarray(high, dtype=float)
    l = np.asarray(low, dtype=float)
    c = np.asarray(close, dtype=float)
    prev_close = np.roll(c, 1)
    prev_close[0] = c[0]
    tr = np.maximum(
        h - l,
        np.maximum(
            np.abs(h - prev_close),
            np.abs(l - prev_close),
        ),
    )
    atr = pd.Series(tr).ewm(
        alpha=1 / period,
        adjust=False,
    ).mean().to_numpy()
    return float(atr[-1]) if len(atr) else 0.0


def _check_spring_invalid(close, high, low, spring_idx, n):
    """判断 Spring 结构是否失效（ATR 动态破位 + 连续两日收盘跌破 Spring 低点）。"""

    later_closes = close[
        spring_idx + 1:
    ]

    if not len(later_closes):
        return False

    atr = _atr(
        high[:spring_idx + 1],
        low[:spring_idx + 1],
        close[:spring_idx + 1],
    )

    fail_price = (
        low[spring_idx]
        - P["spring_invalid_atr_k"] * atr
    )

    severe_break = (
        np.min(later_closes) < fail_price
    )

    below = (
        later_closes
        < low[spring_idx]
    )

    consecutive_break = any(
        below[k] and below[k + 1]
        for k in range(
            len(below) - 1
        )
    )

    return bool(
        severe_break
        or consecutive_break
    )


def _detect_lps(close, low, vol, resistance, sos_idx, range_start, n):
    """找出 SOS 后第一次缩量回踩，跟踪该次回踩直到确认或失效。"""

    lps_idx = None
    lps_volume_ratio = np.nan

    lps_start = (
        sos_idx
        + P["lps_min_gap"]
    )

    lps_end = min(
        n,
        sos_idx
        + P["lps_max_gap"]
        + 1,
    )

    for j in range(
        lps_start,
        lps_end,
    ):

        near_breakout_level = (
            resistance
            * (
                1
                - P["lps_level_tolerance_pct"]
                / 100
            )
            <= low[j]
            <= resistance
            * (
                1
                + P["lps_level_tolerance_pct"]
                / 100
            )
        )

        close_holds_level = (
            close[j] >= resistance * 0.99
        )

        ref_volume = float(
            np.mean(
                vol[
                    max(range_start, j - 20):j
                ]
            )
        )

        lower_than_sos = (
            vol[j]
            <= vol[sos_idx]
            * P["lps_max_volume_ratio"]
        )

        lower_than_average = (
            ref_volume > 0
            and vol[j] <= ref_volume
        )

        if (
            near_breakout_level
            and close_holds_level
            and lower_than_sos
            and lower_than_average
        ):

            lps_idx = j

            lps_volume_ratio = (
                vol[j] / vol[sos_idx]
                if vol[sos_idx] > 0
                else np.nan
            )
            break

    return lps_idx, lps_volume_ratio


def _compute_background(close, low, vol):
    """计算均线斜率、MACD 拐头、MACD/量价背离等背景指标。"""

    close_series = pd.Series(
        close
    )

    ma20 = close_series.rolling(
        20
    ).mean()

    ma60 = close_series.rolling(
        60
    ).mean()

    ma20_slope = (
        (
            ma20.iloc[-1]
            / ma20.iloc[-6]
            - 1
        ) * 100
        if (
            pd.notna(ma20.iloc[-6])
            and ma20.iloc[-6] != 0
        )
        else 0
    )

    ma60_slope = (
        (
            ma60.iloc[-1]
            / ma60.iloc[-6]
            - 1
        ) * 100
        if (
            pd.notna(ma60.iloc[-6])
            and ma60.iloc[-6] != 0
        )
        else 0
    )

    ema12 = close_series.ewm(
        span=12,
        adjust=False,
    ).mean()

    ema26 = close_series.ewm(
        span=26,
        adjust=False,
    ).mean()

    dif = ema12 - ema26

    dea = dif.ewm(
        span=9,
        adjust=False,
    ).mean()

    macd_turn = bool(
        dif.iloc[-1] > dea.iloc[-1]
        and dif.iloc[-1] > dif.iloc[-3]
    )

    pivots = local_pivots(
        low,
        3,
        3,
        "low",
    )

    macd_div = False
    volume_div = False

    if len(pivots) >= 2:

        a, b = pivots[-2], pivots[-1]

        if b - a >= 10:

            macd_div = bool(
                low[b] < low[a]
                and dif.iloc[b] > dif.iloc[a]
            )

            volume_div = bool(
                low[b] < low[a]
                and vol[b] < vol[a]
            )

    return (
        ma20_slope,
        ma60_slope,
        macd_turn,
        macd_div,
        volume_div,
        float(ma20.iloc[-1]),
        float(ma60.iloc[-1]),
    )


class RangeContext(NamedTuple):
    range_start: int
    range_days: int
    range_low: float
    range_high: float
    range_height_pct: float
    in_range: bool
    decline_days: int
    drawdown_pct: float
    support: float
    resistance: float
    support_touches: int
    resistance_touches: int
    level_distance_pct: float
    volume_contraction: float
    opens_near_range: bool
    support_held: bool
    base_quality: bool

    @property
    def range_quality(self):
        return self.base_quality and self.support_touches >= P["support_min_touches"]

    @property
    def sos_range_quality(self):
        return self.base_quality and self.support_touches >= P["sos_support_min_touches"]


def _range_context(high, low, close, vol, end, dates=None):
    """用 end 之前的行情估计区间，end 为触发事件日或样本末尾。"""
    # --------------------------------------------------------
    # 1. 寻找交易区间
    (
        range_start,
        range_days,
        range_low,
        range_high,
        range_height_pct,
        in_range,
    ) = _find_trading_range(
        high[:end],
        low[:end],
        close[:end],
        end,
    )

    # 2. 前期下跌背景
    # --------------------------------------------------------

    opening_level = float(np.median(close[range_start:range_start + 5]))
    lookback_years = P["decline_lookback_years"]
    if dates is None:
        prior_start = max(0, range_start - 252 * lookback_years)
    else:
        cutoff = dates[range_start] - pd.DateOffset(years=lookback_years)
        prior_start = dates.searchsorted(cutoff)
    prior_highs = high[prior_start:range_start]
    decline_days = 0
    drawdown_pct = 0.0
    if len(prior_highs):
        peak_offset = int(np.argmax(prior_highs))
        decline_days = len(prior_highs) - peak_offset
        drawdown_pct = pct(opening_level, prior_highs[peak_offset])

    # --------------------------------------------------------
    # 3. 支撑和阻力
    # --------------------------------------------------------

    segment_low = low[range_start:end]
    segment_high = high[range_start:end]

    low_pivots = local_pivots(
        segment_low,
        3,
        3,
        "low",
    )

    high_pivots = local_pivots(
        segment_high,
        3,
        3,
        "high",
    )

    low_pivots = spaced_pivots(
        low_pivots,
        5,
    )

    high_pivots = spaced_pivots(
        high_pivots,
        5,
    )

    support, support_touches = cluster_price_levels(
        [
            segment_low[i]
            for i in low_pivots
        ],
        P["support_tolerance"],
    )

    resistance, resistance_touches = cluster_price_levels(
        [
            segment_high[i]
            for i in high_pivots
        ],
        P["resistance_tolerance"],
    )

    if support is None:
        support = range_low

    if resistance is None:
        resistance = range_high

    level_distance_pct = (
        (
            resistance / support - 1
        ) * 100
        if support > 0
        else 999
    )

    # --------------------------------------------------------
    # 4. 成交量收缩
    # --------------------------------------------------------

    segment_vol = vol[range_start:end]

    third = max(
        10,
        len(segment_vol) // 3,
    )

    first_volume = float(
        np.mean(segment_vol[:third])
    )

    last_volume = float(
        np.mean(segment_vol[-third:])
    )

    volume_contraction = (
        last_volume / first_volume
        if first_volume > 0
        else 1.0
    )

    # --------------------------------------------------------
    # 5. 交易区间质量硬过滤
    # --------------------------------------------------------

    opens_near_range = bool(
        support * (1 - P["support_tolerance"]) <= opening_level
        <= resistance * (1 + P["resistance_tolerance"])
    )
    below_support = close[range_start:end] < support * (1 - P["support_tolerance"])
    break_days = P["range_support_break_days"]
    support_held = not np.any(
        np.convolve(below_support.astype(int), np.ones(break_days, dtype=int), mode="valid")
        >= break_days
    )
    base_quality = bool(
        in_range
        and opens_near_range
        and support_held
        and decline_days >= P["decline_min_days"]
        and range_days >= P["range_min_days"]
        and range_height_pct
        <= P["range_max_height_pct"]
        and resistance_touches
        >= P["resistance_min_touches"]
        and -P["max_prior_drawdown_pct"]
        <= drawdown_pct
        <= -P["min_prior_drawdown_pct"]
        and 5 <= level_distance_pct <= 30
    )

    return RangeContext(
        range_start,
        range_days,
        range_low,
        range_high,
        range_height_pct,
        in_range,
        decline_days,
        drawdown_pct,
        support,
        resistance,
        support_touches,
        resistance_touches,
        level_distance_pct,
        volume_contraction,
        opens_near_range,
        support_held,
        base_quality,
    )


def detect_structure(df):
    """
    使用触发日前的交易区间分别识别 Spring → Test 和 SOS → LPS。
    触发后的日线用于检查确认与失效。
    """

    close = df["close"].to_numpy(
        dtype=float
    )

    high = df["high"].to_numpy(
        dtype=float
    )

    low = df["low"].to_numpy(
        dtype=float
    )

    opn = df["open"].to_numpy(
        dtype=float
    )

    vol = df["volume"].to_numpy(
        dtype=float
    )

    n = len(close)

    if n < MIN_BARS:
        raise ValueError(
            "历史K线数量不足"
        )

    dates = None
    if "date" in df:
        index = pd.DatetimeIndex(pd.to_datetime(df["date"], errors="coerce"))
        if not index.hasnans and index.is_monotonic_increasing:
            dates = index
    ranges = {}

    def range_at(end):
        if end not in ranges:
            ranges[end] = _range_context(high, low, close, vol, end, dates)
        return ranges[end]

    spread = np.maximum(high - low, 1e-9)
    close_position = (close - low) / spread

    spring_idx = None
    spring_range = None
    for i in range(n - 1, max(20, n - P["spring_recent_days"]) - 1, -1):
        if close_position[i] < P["spring_min_close_pos"] or close[i] < opn[i] * 0.98:
            continue
        ref_volume = float(np.mean(vol[i - 20:i]))
        if not 0 < vol[i] <= ref_volume * P["spring_max_volume_vs_avg"]:
            continue
        candidate = range_at(i)
        if not candidate.range_quality:
            continue
        level = candidate.support
        penetration_pct = (level - low[i]) / level * 100
        if (
            P["spring_penetration_min_pct"] <= penetration_pct <= P["spring_penetration_max_pct"]
            and close[i - 1] >= level * 0.985
            and close[i] >= level * 1.001
        ):
            spring_idx, spring_range = i, candidate
            break

    sos_idx = None
    sos_range = None
    sos_volume_ratio = np.nan
    sos_spread = np.nan
    for i in range(n - 1, max(20, n - P["sos_recent_days"]) - 1, -1):
        if close_position[i] < P["sos_min_close_pos"] or close[i] <= close[i - 1]:
            continue
        ref_volume = float(np.mean(vol[i - 20:i]))
        if ref_volume <= 0 or vol[i] < ref_volume * P["sos_min_volume_ratio"]:
            continue
        candidate = range_at(i)
        if not candidate.sos_range_quality:
            continue
        level = candidate.resistance
        breakout_min = level * (1 + P["sos_min_breakout_pct"] / 100)
        breakout_max = level * (1 + P["sos_max_breakout_pct"] / 100)
        if close[i - 1] < breakout_min <= close[i] <= breakout_max:
            sos_idx, sos_range = i, candidate
            sos_volume_ratio = vol[i] / ref_volume
            atr = _atr(high[:i], low[:i], close[:i])
            sos_spread = (high[i] - low[i]) / atr if atr > 0 else np.nan
            break

    # Spring 和 SOS 各用触发日前的边界；主区间优先展示 Spring 的区间。
    selected_range = spring_range or sos_range or range_at(n)
    using_sos_range = spring_range is None and sos_range is not None
    min_support_touches = P["sos_support_min_touches" if using_sos_range else "support_min_touches"]
    range_quality = selected_range.sos_range_quality if using_sos_range else selected_range.range_quality
    (
        range_start,
        range_days,
        range_low,
        range_high,
        range_height_pct,
        in_range,
        decline_days,
        drawdown_pct,
        support,
        resistance,
        support_touches,
        resistance_touches,
        level_distance_pct,
        volume_contraction,
        opens_near_range,
        support_held,
        _,
    ) = selected_range

    # 7. Test
    if spring_idx is not None:
        (
            test_idx,
            rebound_pct,
        ) = _detect_test(
            close,
            high,
            low,
            vol,
            support,
            spring_idx,
            range_start,
            n,
        )
    else:
        test_idx = None
        rebound_pct = np.nan

    # 8. Spring失效
    spring_invalid = (
        _check_spring_invalid(
            close,
            high,
            low,
            spring_idx,
            n,
        )
        if spring_idx is not None
        else False
    )

    # Test 有效性：Test 后未有效跌破 Test 低点
    test_invalid = False
    if test_idx is not None and n > test_idx + 1:
        later = close[test_idx + 1:]
        if len(later) and np.min(later) < low[test_idx] * 0.99:
            test_invalid = True

    # 9. 当前价格位置
    # --------------------------------------------------------

    close_now = float(
        close[-1]
    )

    spring_distance = (
        pct(
            close_now,
            low[spring_idx],
        )
        if spring_idx is not None
        else np.nan
    )

    support_distance_now = pct(
        close_now,
        support,
    )

    days_since_spring = (
        n - 1 - spring_idx
        if spring_idx is not None
        else None
    )

    # Spring 后低点抬高：Spring 之后未再创出更低的低点
    higher_low = bool(
        spring_idx is not None
        and n > spring_idx + 1
        and float(np.min(low[spring_idx + 1:])) >= low[spring_idx]
    )

    # --------------------------------------------------------
    # 10. Spring早期观察名单
    # --------------------------------------------------------

    early_spring = bool(
        range_quality
        and spring_idx is not None
        and days_since_spring
        <= P["early_spring_recent_days"]
        and test_idx is None
        and not spring_invalid
        and -1 <= spring_distance
        <= P["early_max_spring_distance_pct"]
        and P["early_support_distance_min_pct"]
        <= support_distance_now
        <= P["early_support_distance_max_pct"]
    )

    # --------------------------------------------------------
    # 11. Spring+Test完成名单
    # --------------------------------------------------------

    main_spring_test = bool(
        range_quality
        and spring_idx is not None
        and test_idx is not None
        and not spring_invalid
        and not test_invalid
        and days_since_spring
        <= P["spring_recent_days"]
        and 0 <= spring_distance
        <= P["main_max_spring_distance_pct"]
        and P["main_support_distance_min_pct"]
        <= support_distance_now
        <= P["main_support_distance_max_pct"]
        and close_now >= support * 0.99
    )

    # SOS 任何一天有效跌回区间都使本次突破失效。
    sos_resistance = sos_range.resistance if sos_range else np.nan
    sos_failed = sos_idx is not None and bool(np.any(
        close[sos_idx + 1:] < sos_resistance * (1 - P["sos_failure_tolerance"])
    ))
    sos_valid = sos_idx is not None and not sos_failed and bool(np.any(
        close[sos_idx + 1:] >= sos_resistance
    ))

    lps_idx = None
    lps_volume_ratio = np.nan
    lps_status = ""
    if sos_idx is not None:
        lps_idx, lps_volume_ratio = _detect_lps(
            close, low, vol, sos_resistance, sos_idx, sos_range.range_start, n,
        )
        if lps_idx is not None:
            later = close[lps_idx + 1:]
            if sos_failed or np.any(later < low[lps_idx] * (1 - P["lps_failure_tolerance"])):
                lps_status = "失效"
            elif np.any(later > max(high[lps_idx], sos_resistance)):
                lps_status = "已确认"
            else:
                lps_status = "观察中"

    lps_distance = pct(close_now, sos_resistance) if lps_idx is not None else np.nan
    lps_valid = (
        sos_valid
        and lps_status == "已确认"
        and -100 * P["sos_failure_tolerance"] <= lps_distance <= P["lps_current_max_distance_pct"]
    )

    # 14. MACD和均线
    (
        ma20_slope,
        ma60_slope,
        macd_turn,
        macd_div,
        volume_div,
        ma20_last,
        ma60_last,
    ) = _compute_background(
        close,
        low,
        vol,
    )

    # 15. 日期
    # --------------------------------------------------------

    dates = None

    if "date" in df.columns:
        dates = pd.to_datetime(
            df["date"],
            errors="coerce",
        )

    def event_date(idx):

        if (
            idx is None
            or dates is None
            or pd.isna(dates.iloc[idx])
        ):
            return ""

        return dates.iloc[idx].strftime(
            "%Y-%m-%d"
        )

    # --------------------------------------------------------
    # 16. 排除原因
    # --------------------------------------------------------

    reasons = []

    if not in_range:
        reasons.append(
            "未识别到合格交易区间"
        )

    if in_range and not opens_near_range:
        reasons.append("区间起点未进入后续支撑阻力带")
    if in_range and not support_held:
        reasons.append("区间内连续收盘跌破支撑容差")
    if decline_days < P["decline_min_days"]:
        reasons.append("区间前最近高点至区间起点时间不足")

    if drawdown_pct > -P["min_prior_drawdown_pct"]:
        reasons.append(
            "前期下跌不足(跌幅过小)"
        )

    if drawdown_pct < -P["max_prior_drawdown_pct"]:
        reasons.append(
            "前期下跌过深(超跌)"
        )

    if support_touches < min_support_touches:
        reasons.append(
            "支撑触点不足"
        )

    if resistance_touches < P["resistance_min_touches"]:
        reasons.append(
            "阻力触点不足"
        )

    if level_distance_pct < 5:
        reasons.append(
            "支撑阻力距离过近"
        )

    if spring_idx is not None and spring_invalid:
        reasons.append(
            "Spring结构失效"
        )

    if spring_idx is not None and test_idx is None:
        reasons.append(
            "尚未形成合格Spring Test"
        )

    if sos_failed:
        reasons.append("SOS突破失效")
    if lps_status in ("观察中", "失效"):
        reasons.append(f"LPS{lps_status}")

    if not range_quality:
        reasons.append(
            "交易区间未通过结构硬过滤"
        )

    if range_quality and spring_idx is None:
        reasons.append(
            "未发现有效Spring"
        )

    # --------------------------------------------------------
    # 17. 返回结果
    # --------------------------------------------------------

    return {
        "range_start": int(range_start),
        "range_end": range_start + range_days - 1 if in_range else None,
        "range_days": int(range_days),
        "range_low": float(range_low),
        "range_high": float(range_high),
        "range_height_pct": float(range_height_pct),
        "in_range": bool(in_range),
        "range_quality": bool(range_quality),
        "drawdown_pct": float(drawdown_pct),
        "decline_days": int(decline_days),

        "support": float(support),
        "resistance": float(resistance),
        "support_touches": int(support_touches),
        "resistance_touches": int(resistance_touches),
        "volume_contraction": float(volume_contraction),

        "spring_idx": spring_idx,
        "test_idx": test_idx,
        "spring_date": event_date(spring_idx),
        "test_date": event_date(test_idx),

        "spring_low": (
            float(low[spring_idx])
            if spring_idx is not None
            else np.nan
        ),

        "spring_volume": (
            float(vol[spring_idx])
            if spring_idx is not None
            else np.nan
        ),

        "spring_vol_vs_avg20": (
            float(
                vol[spring_idx]
                / np.mean(vol[spring_idx - 20:spring_idx])
            )
            if (
                spring_idx is not None
                and spring_idx >= 20
                and np.mean(vol[spring_idx - 20:spring_idx]) > 0
            )
            else np.nan
        ),

        "spring_vol_vs_prev": (
            float(vol[spring_idx] / vol[spring_idx - 1])
            if (
                spring_idx is not None
                and spring_idx >= 1
                and vol[spring_idx - 1] > 0
            )
            else np.nan
        ),

        "spring_pos_in_range": (
            float(
                (low[spring_idx] - support)
                / (resistance - support)
            )
            if (
                spring_idx is not None
                and resistance > support
            )
            else np.nan
        ),

        "test_volume_ratio": (
            float(
                vol[test_idx]
                / vol[spring_idx]
            )
            if (
                test_idx is not None
                and vol[spring_idx] > 0
            )
            else np.nan
        ),

        "spring_distance_pct": float(
            spring_distance
        ),

        "distance_to_support_pct": float(
            support_distance_now
        ),

        "days_since_spring": days_since_spring,

        "rebound_pct": float(
            rebound_pct
        ),

        "spring_invalid": bool(
            spring_invalid
        ),

        "test_invalid": bool(
            test_invalid
        ),

        "early_spring": bool(
            early_spring
        ),

        "main_spring_test": bool(
            main_spring_test
        ),

        "sos_idx": sos_idx,
        "lps_idx": lps_idx,

        "sos_date": event_date(sos_idx),
        "lps_date": event_date(lps_idx),

        "sos_volume_ratio": float(
            sos_volume_ratio
        ),

        "sos_spread": float(
            sos_spread
        ),

        "lps_volume_ratio": float(
            lps_volume_ratio
        ),

        "lps_valid": bool(
            lps_valid
        ),

        "sos_valid": bool(
            sos_valid
        ),
        "sos_resistance": float(sos_resistance),
        "lps_status": lps_status,

        "lps_distance_pct": float(
            lps_distance
        ),

        "sc": False,
        "ar": False,
        "st": False,

        "spring": spring_idx is not None,
        "spring_test": test_idx is not None,
        "sos": sos_idx is not None,
        "lps": bool(lps_valid),

        "macd_divergence": bool(
            macd_div
        ),

        "volume_divergence": bool(
            volume_div
        ),

        "higher_low": bool(
            higher_low
        ),

        "macd_turn_up": bool(
            macd_turn
        ),

        "ma20_slope_pct": float(
            ma20_slope
        ),

        "ma60_slope_pct": float(
            ma60_slope
        ),

        "close": close_now,

        "ma20": ma20_last,

        "ma60": ma60_last,

        "exclude_reasons": "；".join(
            dict.fromkeys(reasons)
        ),
    }


# ============================================================
# 六、独立评分
# ============================================================

def score_lists(x):
    """
    三个名单独立评分。
    评分不能覆盖结构硬过滤。
    """

    w = P["score_weights"]

    def add(cond, key):
        return w[key] if cond else 0

    common = (
        add(x["range_quality"], "common_range_quality")
        + add(
            x["volume_contraction"] <= 0.85,
            "common_vol_contract_strong",
        )
        + add(
            0.85 < x["volume_contraction"] <= 1.0,
            "common_vol_contract_weak",
        )
        + add(bool(x.get("volume_divergence")), "volume_divergence")
    )

    spring_score = (
        common
        + add(x["spring"], "spring_event")
        + add(
            np.isfinite(x["rebound_pct"]) and x["rebound_pct"] >= 3,
            "spring_rebound",
        )
        + add(
            x["spring_test"]
            and np.isfinite(x["test_volume_ratio"])
            and x["test_volume_ratio"] <= 0.80,
            "spring_test_low_vol",
        )
        + add(not x["spring_invalid"], "spring_not_invalid")
        + add(x["macd_turn_up"], "macd_turn")
        + add(bool(x.get("higher_low")), "higher_low")
    )

    early_score = (
        common
        + add(x["spring"], "early_spring_event")
        + add(
            x["days_since_spring"] is not None
            and x["days_since_spring"] <= 15,
            "early_recent_days",
        )
        + add(
            np.isfinite(x["spring_distance_pct"])
            and -1 <= x["spring_distance_pct"] <= 5,
            "early_spring_distance",
        )
        + add(
            np.isfinite(x["distance_to_support_pct"])
            and -1 <= x["distance_to_support_pct"] <= 4,
            "early_support_distance",
        )
        + add(x["macd_turn_up"], "macd_turn")
        + add(bool(x.get("higher_low")), "higher_low")
    )

    lps_score = (
        add(x["range_quality"], "lps_range")
        + add(x["sos_valid"], "lps_sos")
        + add(x["lps_valid"], "lps_valid")
        + add(
            np.isfinite(x["lps_volume_ratio"])
            and x["lps_volume_ratio"] <= 0.80,
            "lps_low_vol",
        )
        + add(x["macd_turn_up"], "macd_turn")
        + add(x["ma20_slope_pct"] > 0, "lps_ma20_up")
    )

    common_keys = [
        "common_range_quality",
        "common_vol_contract_strong",
        "volume_divergence",
    ]
    spring_keys = [
        "spring_event", "spring_rebound", "spring_test_low_vol",
        "spring_not_invalid", "macd_turn", "higher_low",
    ]
    early_keys = [
        "early_spring_event", "early_recent_days", "early_spring_distance",
        "early_support_distance", "macd_turn", "higher_low",
    ]
    lps_keys = [
        "lps_range", "lps_sos", "lps_valid",
        "lps_low_vol", "macd_turn", "lps_ma20_up",
    ]

    def norm(raw, keys):
        mx = sum(w[k] for k in keys)
        return min(100, round(raw / mx * 100)) if mx > 0 else 0

    return (
        norm(spring_score, common_keys + spring_keys),
        norm(early_score, common_keys + early_keys),
        norm(lps_score, lps_keys),
    )


# ============================================================
# 七、单只股票分析
# ============================================================

def classify_passes(x, spring_score, early_score, lps_score):
    """选股、诊股和回测共用的入选规则。"""
    threshold = P["score_threshold"]
    return {
        "main": x["main_spring_test"] and spring_score >= threshold,
        "early": x["early_spring"] and early_score >= threshold,
        "lps": x["sos_valid"] and x["lps_valid"] and lps_score >= threshold,
    }


CANDIDATE_LISTS = (
    ("深跌后横盘", "_base_pass", "base_days"),
    ("Spring_Test主名单", "_main_pass", "Spring_Test_score"),
    ("Spring早期观察", "_early_pass", "Spring_early_score"),
    ("LPS备选", "_lps_pass", "LPS_score"),
)

BASE_COLUMNS = {
    "base_peak_date": "横盘前高点日期",
    "base_peak": "横盘前高点",
    "base_rally_pct": "高点前涨幅%",
    "base_drawdown_pct": "高点后跌幅%",
    "base_start_date": "横盘起点",
    "base_days": "横盘交易日数",
    "base_lower": "主要收盘区间下沿",
    "base_upper": "主要收盘区间上沿",
    "base_round_trips": "区间往返次数",
}


def event_result(hist):
    x = detect_structure(hist)

    (
        spring_score,
        early_score,
        lps_score,
    ) = score_lists(x)

    return {

        "Spring_Test_score": spring_score,
        "Spring_early_score": early_score,
        "LPS_score": lps_score,

        "range_quality": x["range_quality"],
        "range_days": x["range_days"],
        "range_low": x["range_low"],
        "range_high": x["range_high"],
        "range_height_pct": x["range_height_pct"],
        "drawdown_pct": x["drawdown_pct"],

        "support": x["support"],
        "resistance": x["resistance"],
        "support_touches": x["support_touches"],
        "resistance_touches": x["resistance_touches"],

        "volume_contraction": x["volume_contraction"],

        "Spring": x["spring"],
        "Spring_date": x["spring_date"],
        "Spring_low": x["spring_low"],
        "Spring_distance_pct": x["spring_distance_pct"],
        "days_since_spring": x["days_since_spring"],
        "Spring_rebound_pct": x["rebound_pct"],
        "Spring_invalid": x["spring_invalid"],

        "Spring_Test": x["spring_test"],
        "Test_date": x["test_date"],
        "Test_volume_ratio": x["test_volume_ratio"],

        "distance_to_support_pct": x["distance_to_support_pct"],

        "SOS": x["sos"],
        "SOS_date": x["sos_date"],
        "SOS_volume_ratio": x["sos_volume_ratio"],

        "LPS": x["lps"],
        "LPS_status": x["lps_status"],
        "SOS_resistance": x["sos_resistance"],
        "LPS_date": x["lps_date"],
        "LPS_volume_ratio": x["lps_volume_ratio"],
        "LPS_distance_pct": x["lps_distance_pct"],

        "MACD_divergence": x["macd_divergence"],
        "volume_divergence": x["volume_divergence"],
        "higher_low": x["higher_low"],
        "MACD_turn_up": x["macd_turn_up"],

        "MA20_slope_pct": x["ma20_slope_pct"],
        "MA60_slope_pct": x["ma60_slope_pct"],

        "ma20": x["ma20"],
        "ma60": x["ma60"],

        "exclude_reasons": x["exclude_reasons"],

        **{
            f"_{name}_pass": passed
            for name, passed in classify_passes(x, spring_score, early_score, lps_score).items()
        },
    }


def analyze_one(code, name, *, event_eligible=True, keep_chart_data=False):
    hist = fetch_history(code, refresh=True)
    if hist is None:
        return None

    try:
        base = detect_base(hist)
        row = {
            "code": code,
            "name": name,
            "as_of": hist["date"].iat[-1].date().isoformat(),
            "close": float(hist["close"].iat[-1]),
            "event_eligible": event_eligible,
            **{column: getattr(base, column.removeprefix("base_"), None) for column in BASE_COLUMNS},
            **{flag: False for _, flag, _ in CANDIDATE_LISTS},
            "_base_pass": base is not None,
        }
        if event_eligible:
            row.update(event_result(hist))
        if keep_chart_data and any(row[flag] for _, flag, _ in CANDIDATE_LISTS):
            row["_chart_data"] = hist, base
        return row
    except Exception as exc:
        LOG.exception("%s 分析失败：%s", code, exc)
        return None


# ============================================================
# 八、K线图
# ============================================================

def draw_chart(code, name, out_dir, *, hist=None, base=None, show_events=True):

    try:

        import matplotlib

        matplotlib.use("Agg")

        import matplotlib.pyplot as plt

    except Exception:

        LOG.warning(
            "matplotlib未安装，无法绘图"
        )

        return False

    if hist is None:
        hist = fetch_history(code)

    if hist is None:
        return False

    try:


        d = hist.tail(180).reset_index(
            drop=True
        )

        offset = len(hist) - len(d)

        plt.rcParams["font.sans-serif"] = [
            "Microsoft YaHei",
            "SimHei",
            "SimSun",
            "DejaVu Sans",
        ]

        plt.rcParams["axes.unicode_minus"] = False

        fig, (ax, av) = plt.subplots(
            2,
            1,
            figsize=(14, 9),
            sharex=True,
            gridspec_kw={
                "height_ratios": [3, 1]
            },
        )

        xx = np.arange(len(d))

        # K线
        for i, row in d.iterrows():

            color = (
                "red"
                if row["close"] >= row["open"]
                else "green"
            )

            ax.vlines(
                i,
                row["low"],
                row["high"],
                color=color,
                linewidth=0.8,
            )

            body_height = max(
                abs(
                    row["close"]
                    - row["open"]
                ),
                0.001,
            )

            ax.bar(
                i,
                body_height,
                bottom=min(
                    row["open"],
                    row["close"],
                ),
                width=0.6,
                color=color,
                edgecolor=color,
            )

        # 均线
        ax.plot(
            xx,
            d["close"].rolling(20).mean(),
            label="MA20",
            linewidth=1,
        )

        ax.plot(
            xx,
            d["close"].rolling(60).mean(),
            label="MA60",
            linewidth=1,
        )

        if base is not None:
            base_start = max(0, len(d) - base.days)
            ax.fill_between(
                xx[base_start:], base.lower, base.upper,
                color="tab:blue", alpha=0.16,
                label=f"Bottom base from {base.start_date}",
            )

        if show_events:
            x = detect_structure(hist)
            # 支撑和阻力
            ax.axhline(
                x["support"],
                linestyle="--",
                linewidth=0.9,
                label="Support",
            )

            ax.axhline(
                x["resistance"],
                linestyle="--",
                linewidth=0.9,
                label="Resistance",
            )

            if x["sos"] and not np.isclose(x["sos_resistance"], x["resistance"]):
                ax.axhline(x["sos_resistance"], linestyle=":", linewidth=0.9, label="SOS level")

            # 事件标注
            lps_label = {"已确认": "LPS confirmed", "观察中": "LPS watching", "失效": "LPS failed"}
            events = [
                ("Spring", x["spring_idx"]),
                ("Test", x["test_idx"]),
                ("SOS", x["sos_idx"]),
                (lps_label.get(x["lps_status"], "LPS"), x["lps_idx"]),
            ]

            for label, idx in events:

                if (
                    idx is not None
                    and offset <= idx < len(hist)
                ):

                    xi = idx - offset

                    event_price = float(
                        hist.iloc[idx]["low"]
                    )

                    ax.annotate(
                        label,
                        (xi, event_price),
                        xytext=(0, -16),
                        textcoords="offset points",
                        ha="center",
                        fontsize=8,
                        arrowprops={
                            "arrowstyle": "->",
                            "lw": 0.6,
                        },
                    )

        ax.set_title(
            f"{name} ({code}) - Wyckoff {VERSION}"
        )

        ax.legend(
            loc="best",
            fontsize=8,
            ncol=3,
        )

        # 成交量
        colors = [
            "red"
            if row["close"] >= row["open"]
            else "green"
            for _, row in d.iterrows()
        ]

        av.bar(
            xx,
            d["volume"],
            color=colors,
            width=0.7,
        )

        av.set_ylabel("Volume")

        out_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        fig.tight_layout()

        fig.savefig(
            out_dir / f"{code}_{name}.png",
            dpi=130,
        )

        plt.close(fig)

        return True

    except Exception as exc:

        LOG.exception(
            "%s K线图生成失败：%s",
            code,
            exc,
        )

        return False


# ============================================================
# 九、Excel导出
# ============================================================

def candidate_frames(result_df):
    for title, flag, order_by in CANDIDATE_LISTS:
        frame = result_df.loc[result_df[flag]]
        if not frame.empty:
            order = ["as_of", order_by] if flag == "_base_pass" else [order_by]
            frame = frame.sort_values([*order, "code"], ascending=[False] * len(order) + [True])
        yield title, frame


def export_excel(
    result_df,
    output,
    stats,
):

    rename = {
        "code": "代码",
        "name": "名称",
        "as_of": "行情日期",
        "event_eligible": "纳入事件筛选",
        **BASE_COLUMNS,

        "Spring_Test_score": "Spring_Test质量分",
        "Spring_early_score": "Spring早期质量分",
        "LPS_score": "LPS质量分",

        "range_quality": "交易区间质量通过",
        "range_days": "有效区间天数",
        "range_low": "区间下沿",
        "range_high": "区间上沿",
        "range_height_pct": "区间振幅%",
        "drawdown_pct": "距前期高点涨跌%",

        "support": "支撑区域",
        "resistance": "阻力区域",
        "support_touches": "支撑触点数",
        "resistance_touches": "阻力触点数",

        "volume_contraction": "后段/前段均量比",

        "Spring": "Spring候选",
        "Spring_date": "Spring日期",
        "Spring_low": "Spring最低价",
        "Spring_distance_pct": "现价距Spring低点%",
        "days_since_spring": "Spring距今天数",
        "Spring_rebound_pct": "Spring后反弹%",
        "Spring_invalid": "Spring结构失效",

        "Spring_Test": "Spring_Test完成",
        "Test_date": "Test日期",
        "Test_volume_ratio": "Test量能/Spring量能",

        "distance_to_support_pct": "现价距支撑%",

        "SOS": "SOS候选",
        "SOS_date": "SOS日期",
        "SOS_volume_ratio": "SOS量能/前20日均量",

        "LPS": "LPS确认且位置合格",
        "LPS_status": "LPS状态",
        "SOS_resistance": "SOS突破位",
        "LPS_date": "LPS日期",
        "LPS_volume_ratio": "LPS量能/SOS量能",
        "LPS_distance_pct": "现价距阻力%",

        "MACD_divergence": "MACD底背离",
        "volume_divergence": "量价背离",
        "higher_low": "Spring后低点抬高",
        "MACD_turn_up": "MACD拐头",

        "MA20_slope_pct": "MA20斜率%",
        "MA60_slope_pct": "MA60斜率%",

        "close": "收盘价",
        "ma20": "MA20",
        "ma60": "MA60",

        "exclude_reasons": "未入选/复核原因",
    }

    with pd.ExcelWriter(
        output,
        engine="openpyxl",
    ) as writer:

        flags = [flag for _, flag, _ in CANDIDATE_LISTS]
        included = result_df[flags].any(axis=1)
        sheets = [
            *candidate_frames(result_df),
            ("排除名单", result_df.loc[~included]),
            ("全量扫描数据", result_df.sort_values(flags, ascending=False)),
        ]
        for sheet_name, frame in sheets:
            if sheet_name == "深跌后横盘":
                columns = ["code", "name", "as_of", "close", *BASE_COLUMNS]
            else:
                columns = list(rename)
            export_df = frame.reindex(columns=columns).rename(columns=rename)

            export_df.to_excel(
                writer,
                sheet_name=sheet_name,
                index=False,
            )

        description = pd.DataFrame(
            [
                [
                    "扫描时间",
                    datetime.now().strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                ],
                [
                    "扫描股票数",
                    stats["universe"],
                ],
                [
                    "成功分析",
                    stats["success"],
                ],
                [
                    "失败数量",
                    stats["failed"],
                ],
                ["最新日线", result_df["as_of"].max()],
                ["早于最新日线的股票数", int(result_df["as_of"].lt(result_df["as_of"].max()).sum())],
                ["纳入事件筛选的股票数", stats["event_eligible"]],
                ["深跌后横盘名单数", stats["base"]],
                [
                    "Spring+Test名单数",
                    stats["main"],
                ],
                [
                    "Spring早期名单数",
                    stats["early"],
                ],
                [
                    "LPS备选名单数",
                    stats["lps"],
                ],
                [
                    "策略说明",
                    "深跌后横盘独立筛选，不限流通市值。三个事件名单先检查结构，再按各自门槛评分。",
                ],
                [
                    "参数说明",
                    str(P),
                ],
            ],
            columns=[
                "项目",
                "内容",
            ],
        )

        description.to_excel(
            writer,
            sheet_name="扫描说明",
            index=False,
        )

        # 自动调整列宽
        for worksheet in writer.sheets.values():

            worksheet.freeze_panes = "A2"
            worksheet.auto_filter.ref = (
                worksheet.dimensions
            )

            for column_cells in worksheet.columns:

                max_length = 0

                column_letter = (
                    column_cells[0].column_letter
                )

                for cell in column_cells:

                    value = (
                        ""
                        if cell.value is None
                        else str(cell.value)
                    )

                    max_length = max(
                        max_length,
                        len(value),
                    )

                worksheet.column_dimensions[
                    column_letter
                ].width = min(
                    max(max_length + 2, 10),
                    35,
                )


# ============================================================
# 十、主程序
# ============================================================

def main():

    parser = argparse.ArgumentParser(
        description=(
            "A股 Wyckoff 早期吸筹机会筛选器 "
            + VERSION
        )
    )

    parser.add_argument(
        "--threads",
        type=int,
        default=5,
        help="并发线程数",
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="只扫描前N只，0为全量",
    )

    parser.add_argument(
        "--top",
        type=int,
        default=30,
        help="终端显示前N只",
    )

    parser.add_argument(
        "--charts",
        type=int,
        default=5,
        help="生成K线图数量，0关闭",
    )

    parser.add_argument(
        "--no-cap-filter",
        action="store_true",
        help="跳过流通市值过滤",
    )

    parser.add_argument(
        "--min-cap",
        type=float,
        default=CAP_MIN,
        help="最小流通市值，单位亿元",
    )

    parser.add_argument(
        "--max-cap",
        type=float,
        default=CAP_MAX,
        help="最大流通市值，单位亿元",
    )

    parser.add_argument(
        "--score-threshold",
        type=int,
        default=P["score_threshold"],
        help="三个事件名单的最低评分",
    )

    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="完整下载历史日线，不读取或写入缓存",
    )

    args = parser.parse_args()

    global USE_CACHE
    USE_CACHE = not args.no_cache

    P["score_threshold"] = max(
        0,
        min(
            100,
            args.score_threshold,
        ),
    )

    print("=" * 72)

    print(
        f"A股 Wyckoff 早期吸筹机会筛选器 {VERSION}"
    )

    print(
        "四个名单：深跌后横盘 / Spring+Test / Spring早期 / LPS"
    )

    print(
        "结构硬过滤优先，评分不能覆盖结构失效"
    )

    print(
        "规则型初筛，不代表确定的主力吸筹；请人工复核K线"
    )

    print("=" * 72)

    # --------------------------------------------------------
    # 1. 股票池
    # --------------------------------------------------------

    print(
        "[1/4] 获取沪深主板股票列表..."
    )

    stocks = get_stock_universe()
    universe = stocks[:args.limit] if args.limit > 0 else stocks
    print(f"主板非ST：{len(stocks)} 只，本次扫描 {len(universe)} 只")
    print("深跌后横盘名单不限制流通市值。")

    if args.no_cap_filter:
        event_codes = {code for code, _ in universe}
        print("[2/4] 事件名单跳过流通市值过滤")
    else:
        print("[2/4] 获取事件名单所需的流通市值...")
        caps = fetch_float_caps([code for code, _ in universe])
        event_codes = {
            code for code, cap in caps.items() if args.min_cap <= cap <= args.max_cap
        }
        print(f"纳入事件筛选：{len(event_codes)} 只")

    # --------------------------------------------------------
    # 3. 扫描
    # --------------------------------------------------------

    print(
        f"[3/4] 扫描 {len(universe)} 只股票，并发 {args.threads}..."
    )

    results = []
    chart_data = {}
    failed = 0

    with ThreadPoolExecutor(
        max_workers=max(1, args.threads)
    ) as pool:

        futures = {
            pool.submit(
                analyze_one,
                code,
                name,
                event_eligible=code in event_codes,
                keep_chart_data=args.charts > 0,
            ): (code, name)
            for code, name in universe
        }

        for idx, future in enumerate(
            as_completed(futures),
            1,
        ):

            try:

                result = future.result()

                if result is None:
                    failed += 1
                else:
                    chart = result.pop("_chart_data", None)
                    if chart is not None:
                        chart_data[result["code"]] = chart
                    results.append(result)

            except Exception as exc:

                failed += 1

                LOG.exception(
                    "任务异常：%s",
                    exc,
                )

            if (
                idx % 50 == 0
                or idx == len(futures)
            ):

                print(
                    f"进度 {idx}/{len(futures)} "
                    f"| 成功 {len(results)} "
                    f"| 失败 {failed}",
                    flush=True,
                )

    if not results:

        print(
            "没有成功分析的股票，请检查网络、AkShare版本和日志。"
        )

        return

    df = pd.DataFrame(
        results
    )
    for column in ("base_days", "base_round_trips"):
        df[column] = df[column].astype("Int64")

    # --------------------------------------------------------
    # 4. 统计
    # --------------------------------------------------------

    stats = {
        "universe": len(universe),
        "success": len(df),
        "failed": failed,
        "base": int(df["_base_pass"].sum()),
        "event_eligible": int(df["event_eligible"].sum()),

        "main": int(
            df["_main_pass"].sum()
        ),

        "early": int(
            df["_early_pass"].sum()
        ),

        "lps": int(
            df["_lps_pass"].sum()
        ),
    }

    today = datetime.now().strftime(
        "%Y%m%d"
    )

    output = (
        ROOT
        / f"wyckoff_screen_4_1_{today}.xlsx"
    )

    export_excel(
        df,
        output,
        stats,
    )

    print(
        f"\n[4/4] Excel已导出：{output}"
    )

    print(
        f"成功分析 {len(df)} 只，失败 {failed} 只"
    )

    latest = df["as_of"].max()
    older = int(df["as_of"].lt(latest).sum())
    print(f"本次最新日线 {latest}，另有 {older} 只日线早于该日期。")
    print(
        f"深跌后横盘：{stats['base']} 只 | Spring+Test：{stats['main']} 只 "
        f"| Spring早期：{stats['early']} 只 | LPS备选：{stats['lps']} 只"
    )

    frames = list(candidate_frames(df))
    for (title, flag, score_col), (_, frame) in zip(CANDIDATE_LISTS, frames):
        print(f"\n--- {title} ---")
        if frame.empty:
            print("本次没有符合硬性条件的股票。")
            continue
        if flag == "_base_pass":
            columns = [
                "code", "name", "as_of", "base_start_date", "base_days",
                "base_lower", "base_upper", "base_round_trips",
            ]
            display = frame[columns].rename(columns={
                "code": "代码", "name": "名称", "as_of": "行情日期", **BASE_COLUMNS,
            })
        else:
            columns = [
                "code", "name", "as_of", score_col, "Spring_date",
                "Test_date", "SOS_date", "LPS_date",
            ]
            display = frame[columns]
        print(display.head(args.top).to_string(index=False))

    if args.charts > 0:
        # 从各名单轮流选图，同一只股票只画一次。
        chart_candidates = (
            pd.concat([
                frame.head(args.charts).assign(chart_rank=np.arange(min(len(frame), args.charts)))
                for _, frame in frames if not frame.empty
            ]).sort_values("chart_rank", kind="stable").drop_duplicates("code").head(args.charts)
            if chart_data else df.iloc[:0]
        )
        timestamp = datetime.now().strftime("%Y%m%d%H_%M")
        chart_dir = ROOT / f"charts_{timestamp}"
        count = 0

        for _, row in chart_candidates.iterrows():
            hist, base = chart_data[row["code"]]
            if draw_chart(
                row["code"], row["name"], chart_dir, hist=hist, base=base,
                show_events=any(row[flag] for _, flag, _ in CANDIDATE_LISTS[1:]),
            ):
                count += 1

        if count:
            print(f"K线图生成：{count} 张，目录：{chart_dir}")
        else:
            print("没有生成候选 K 线图。")


if __name__ == "__main__":
    main()
