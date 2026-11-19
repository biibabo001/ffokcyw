#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
A股 Wyckoff 早期吸筹机会筛选器 4.1

功能：
1. 获取沪深主板股票日线数据
2. 可选流通市值过滤
3. 识别交易区间、Spring、Test、SOS、LPS
4. 输出 Spring+Test、Spring早期、LPS 三个独立名单
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


# ============================================================
# 一、基础配置
# ============================================================

ROOT = Path(__file__).resolve().parent

MAIN_PREFIXES = (
    "600", "601", "603", "605",
    "000", "001", "002", "003",
)

HISTORY_DAYS = 900
MIN_BARS = 250

CAP_MIN = 30.0
CAP_MAX = 300.0

VERSION = "4.1"

# 参数配置
P = {
    # 交易区间
    "range_min_days": 80,
    "range_max_days": 180,
    "range_max_height_pct": 25.0,
    "range_max_endpoint_change_pct": 30.0,

    # 支撑和阻力
    "support_tolerance": 0.03,
    "resistance_tolerance": 0.03,
    "support_min_touches": 5,
    "resistance_min_touches": 4,

    # 前期下跌背景
    "min_prior_drawdown_pct": 12.0,

    # Spring
    "spring_recent_days": 30,
    "early_spring_recent_days": 15,
    "spring_penetration_min_pct": 0.3,
    "spring_penetration_max_pct": 3.5,
    "spring_min_close_pos": 0.55,
    "spring_max_volume_vs_avg": 1.8,

    # Test
    "test_min_gap": 3,
    "test_max_gap": 20,
    "test_min_rebound_pct": 3.0,
    "test_max_volume_ratio": 0.80,
    "test_support_tolerance_pct": 3.5,

    # Spring+Test当前价格位置
    "main_max_spring_distance_pct": 5.0,
    "main_support_distance_min_pct": -1.0,
    "main_support_distance_max_pct": 4.0,

    # Spring早期观察
    "early_max_spring_distance_pct": 5.0,
    "early_support_distance_min_pct": -1.0,
    "early_support_distance_max_pct": 4.0,

    # SOS
    "sos_recent_days": 60,
    "sos_min_breakout_pct": 0.5,
    "sos_min_volume_ratio": 1.30,

    # LPS
    "lps_min_gap": 2,
    "lps_max_gap": 25,
    "lps_max_volume_ratio": 0.80,
    "lps_level_tolerance_pct": 3.0,
    "lps_current_max_distance_pct": 5.0,

    # Spring失效
    "spring_invalid_close_pct": 1.5,

    # 评分
    "score_threshold": 80,
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


def normalize_history(df):
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
        & (d["volume"] >= 0)
    ]

    if "date" in d.columns:
        d = d.drop_duplicates(
            subset=["date"]
        )

    if len(d) < MIN_BARS:
        return None

    return d.reset_index(drop=True)


def fetch_history(code):
    """
    获取前复权日线数据。
    """

    start = (
        datetime.now()
        - timedelta(days=HISTORY_DAYS)
    ).strftime("%Y%m%d")

    end = datetime.now().strftime("%Y%m%d")

    try:
        raw = ak.stock_zh_a_hist_tx(
            symbol=tencent_symbol(code),
            start_date=start,
            end_date=end,
            adjust="qfq",
        )

        return normalize_history(raw)

    except Exception as exc:
        LOG.warning(
            "%s 行情获取失败：%s",
            code,
            exc,
        )

        return None


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
# 五、Wyckoff结构识别
# ============================================================

def detect_structure(df):
    """
    识别顺序：

    交易区间
        ↓
    Spring
        ↓
    Test
        ↓
    SOS
        ↓
    LPS

    不满足结构条件时，不会仅凭评分进入名单。
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

    # --------------------------------------------------------
    # 1. 寻找交易区间
    # --------------------------------------------------------

    range_info = None

    max_len = min(
        P["range_max_days"],
        n - 40,
    )

    for length in range(
        max_len,
        P["range_min_days"] - 1,
        -1,
    ):

        start = n - length

        segment_high = high[start:]
        segment_low = low[start:]
        segment_close = close[start:]

        range_low = float(
            np.quantile(
                segment_low,
                0.10,
            )
        )

        range_high = float(
            np.quantile(
                segment_high,
                0.90,
            )
        )

        if (
            range_low <= 0
            or range_high <= range_low
        ):
            continue

        height_pct = (
            range_high / range_low - 1
        ) * 100

        endpoint_change = abs(
            (
                segment_close[-1]
                / segment_close[0]
                - 1
            ) * 100
        )

        first_close = float(
            np.mean(segment_close[:20])
        )

        last_close = float(
            np.mean(segment_close[-20:])
        )

        internal_change = (
            abs(
                (
                    last_close
                    / first_close
                    - 1
                ) * 100
            )
            if first_close > 0
            else 999
        )

        if (
            height_pct
            <= P["range_max_height_pct"]
            and endpoint_change
            <= P["range_max_endpoint_change_pct"]
            and internal_change <= 18
        ):

            range_info = (
                start,
                length,
                range_low,
                range_high,
                height_pct,
            )

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

    # --------------------------------------------------------
    # 2. 前期下跌背景
    # --------------------------------------------------------

    if range_start >= 30:

        prior_high = float(
            np.max(
                high[:range_start]
            )
        )

    else:

        prior_high = float(
            np.max(
                high[:max(1, range_start + 1)]
            )
        )

    drawdown_pct = (
        (
            close[range_start]
            / prior_high
            - 1
        ) * 100
        if prior_high > 0
        else 0
    )

    # --------------------------------------------------------
    # 3. 支撑和阻力
    # --------------------------------------------------------

    segment_low = low[range_start:]
    segment_high = high[range_start:]

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

    segment_vol = vol[range_start:]

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

    range_quality = bool(
        in_range
        and range_days >= P["range_min_days"]
        and range_height_pct
        <= P["range_max_height_pct"]
        and support_touches
        >= P["support_min_touches"]
        and resistance_touches
        >= P["resistance_min_touches"]
        and drawdown_pct
        <= -P["min_prior_drawdown_pct"]
        and 5 <= level_distance_pct <= 30
    )

    # --------------------------------------------------------
    # 6. Spring
    # --------------------------------------------------------

    spring_idx = None

    if range_quality:

        begin = max(
            range_start + 10,
            n - P["spring_recent_days"],
        )

        for i in range(begin, n):

            penetration_pct = (
                (support - low[i])
                / support
                * 100
            )

            candle_range = max(
                high[i] - low[i],
                1e-9,
            )

            close_position = (
                (close[i] - low[i])
                / candle_range
            )

            previous_holds = (
                close[i - 1]
                >= support * 0.985
            )

            penetration_ok = (
                P["spring_penetration_min_pct"]
                <= penetration_pct
                <= P["spring_penetration_max_pct"]
            )

            reclaim_support = (
                close[i] >= support * 1.001
            )

            strong_close = (
                close_position
                >= P["spring_min_close_pos"]
            )

            base_volume = float(
                np.mean(
                    vol[
                        max(range_start, i - 20):i
                    ]
                )
            )

            volume_ok = (
                base_volume > 0
                and vol[i]
                <= base_volume
                * P["spring_max_volume_vs_avg"]
            )

            candle_ok = (
                close[i] >= opn[i] * 0.98
            )

            if (
                previous_holds
                and penetration_ok
                and reclaim_support
                and strong_close
                and volume_ok
                and candle_ok
            ):
                spring_idx = i

    # --------------------------------------------------------
    # 7. Test
    # --------------------------------------------------------

    test_idx = None
    rebound_pct = np.nan

    if spring_idx is not None:

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
                and close_position >= 0.45
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

    # --------------------------------------------------------
    # 8. Spring失效
    # --------------------------------------------------------

    spring_invalid = False

    if spring_idx is not None:

        later_closes = close[
            spring_idx + 1:
        ]

        if len(later_closes):

            severe_break = (
                np.min(later_closes)
                < low[spring_idx]
                * (
                    1
                    - P["spring_invalid_close_pct"]
                    / 100
                )
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

            spring_invalid = bool(
                severe_break
                or consecutive_break
            )

    # --------------------------------------------------------
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
        and days_since_spring
        <= P["spring_recent_days"]
        and 0 <= spring_distance
        <= P["main_max_spring_distance_pct"]
        and P["main_support_distance_min_pct"]
        <= support_distance_now
        <= P["main_support_distance_max_pct"]
        and close_now >= support * 0.99
    )

    # --------------------------------------------------------
    # 12. SOS
    # --------------------------------------------------------

    sos_idx = None
    sos_volume_ratio = np.nan

    if range_quality and resistance > 0:

        sos_begin = max(
            range_start + 20,
            n - P["sos_recent_days"],
        )

        for i in range(
            sos_begin,
            n,
        ):

            # 如果有Spring Test，SOS必须在Test之后
            if (
                test_idx is not None
                and i <= test_idx
            ):
                continue

            ref_volume = float(
                np.mean(
                    vol[
                        max(range_start, i - 20):i
                    ]
                )
            )

            if ref_volume <= 0:
                continue

            candle_range = max(
                high[i] - low[i],
                1e-9,
            )

            close_position = (
                (close[i] - low[i])
                / candle_range
            )

            breakout = (
                close[i]
                >= resistance
                * (
                    1
                    + P["sos_min_breakout_pct"]
                    / 100
                )
            )

            volume_expansion = (
                vol[i]
                >= ref_volume
                * P["sos_min_volume_ratio"]
            )

            strong_close = (
                close_position >= 0.65
            )

            if (
                breakout
                and volume_expansion
                and strong_close
            ):

                sos_idx = i
                sos_volume_ratio = (
                    vol[i] / ref_volume
                )

    # --------------------------------------------------------
    # 13. LPS
    # --------------------------------------------------------

    lps_idx = None
    lps_volume_ratio = np.nan

    if sos_idx is not None:

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

    lps_distance = (
        pct(
            close_now,
            resistance,
        )
        if lps_idx is not None
        else np.nan
    )

    lps_valid = bool(
        range_quality
        and sos_idx is not None
        and lps_idx is not None
        and lps_idx > sos_idx
        and close_now >= resistance * 0.97
        and close_now
        <= resistance
        * (
            1
            + P["lps_current_max_distance_pct"]
            / 100
        )
        and close_now >= low[lps_idx] * 0.98
    )

    # --------------------------------------------------------
    # 14. MACD和均线
    # --------------------------------------------------------

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

    if len(pivots) >= 2:

        a, b = pivots[-2], pivots[-1]

        if b - a >= 10:

            macd_div = bool(
                low[b] < low[a]
                and dif.iloc[b] > dif.iloc[a]
            )

    # --------------------------------------------------------
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

    if drawdown_pct > -P["min_prior_drawdown_pct"]:
        reasons.append(
            "前期下跌背景不足"
        )

    if support_touches < P["support_min_touches"]:
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
        "range_days": int(range_days),
        "range_low": float(range_low),
        "range_high": float(range_high),
        "range_height_pct": float(range_height_pct),
        "in_range": bool(in_range),
        "range_quality": bool(range_quality),
        "drawdown_pct": float(drawdown_pct),

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

        "lps_volume_ratio": float(
            lps_volume_ratio
        ),

        "lps_valid": bool(
            lps_valid
        ),

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

        "ma20": float(
            ma20.iloc[-1]
        ),

        "ma60": float(
            ma60.iloc[-1]
        ),

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

    common = (
        (15 if x["range_quality"] else 0)
        +
        (
            10
            if x["volume_contraction"] <= 0.85
            else 5
            if x["volume_contraction"] <= 1.0
            else 0
        )
    )

    spring_score = (
        common
        + (25 if x["spring"] else 0)
        + (
            15
            if (
                np.isfinite(x["rebound_pct"])
                and x["rebound_pct"] >= 3
            )
            else 0
        )
        + (
            20
            if (
                x["spring_test"]
                and np.isfinite(x["test_volume_ratio"])
                and x["test_volume_ratio"] <= 0.80
            )
            else 0
        )
        + (10 if not x["spring_invalid"] else 0)
        + (5 if x["macd_turn_up"] else 0)
    )

    early_score = (
        common
        + (30 if x["spring"] else 0)
        + (
            20
            if (
                x["days_since_spring"] is not None
                and x["days_since_spring"] <= 15
            )
            else 0
        )
        + (
            15
            if (
                np.isfinite(x["spring_distance_pct"])
                and -1 <= x["spring_distance_pct"] <= 5
            )
            else 0
        )
        + (
            15
            if (
                np.isfinite(x["distance_to_support_pct"])
                and -1 <= x["distance_to_support_pct"] <= 4
            )
            else 0
        )
        + (5 if x["macd_turn_up"] else 0)
    )

    lps_score = (
        (20 if x["range_quality"] else 0)
        + (25 if x["sos"] else 0)
        + (30 if x["lps_valid"] else 0)
        + (
            15
            if (
                np.isfinite(x["lps_volume_ratio"])
                and x["lps_volume_ratio"] <= 0.80
            )
            else 0
        )
        + (5 if x["macd_turn_up"] else 0)
        + (5 if x["ma20_slope_pct"] > 0 else 0)
    )

    return (
        min(100, int(spring_score)),
        min(100, int(early_score)),
        min(100, int(lps_score)),
    )


# ============================================================
# 七、单只股票分析
# ============================================================

def analyze_one(code, name):

    hist = fetch_history(code)

    if hist is None:
        return None

    try:

        x = detect_structure(hist)

        (
            spring_score,
            early_score,
            lps_score,
        ) = score_lists(x)

        row = {
            "code": code,
            "name": name,

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
            "LPS_date": x["lps_date"],
            "LPS_volume_ratio": x["lps_volume_ratio"],
            "LPS_distance_pct": x["lps_distance_pct"],

            "MACD_divergence": x["macd_divergence"],
            "MACD_turn_up": x["macd_turn_up"],

            "MA20_slope_pct": x["ma20_slope_pct"],
            "MA60_slope_pct": x["ma60_slope_pct"],

            "close": x["close"],
            "ma20": x["ma20"],
            "ma60": x["ma60"],

            "exclude_reasons": x["exclude_reasons"],

            "_main_pass": (
                x["range_quality"]
                and x["main_spring_test"]
                and spring_score >= P["score_threshold"]
            ),

            "_early_pass": (
                x["range_quality"]
                and x["early_spring"]
                and early_score >= P["score_threshold"]
            ),

            "_lps_pass": (
                x["range_quality"]
                and x["sos"]
                and x["lps_valid"]
                and lps_score >= P["score_threshold"]
            ),
        }

        return row

    except Exception as exc:

        LOG.exception(
            "%s 分析失败：%s",
            code,
            exc,
        )

        return None


# ============================================================
# 八、K线图
# ============================================================

def draw_chart(code, name, out_dir):

    try:

        import matplotlib

        matplotlib.use("Agg")

        import matplotlib.pyplot as plt

    except Exception:

        LOG.warning(
            "matplotlib未安装，无法绘图"
        )

        return False

    hist = fetch_history(code)

    if hist is None:
        return False

    try:

        x = detect_structure(hist)

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

        # 事件标注
        events = [
            ("Spring", x["spring_idx"]),
            ("Test", x["test_idx"]),
            ("SOS", x["sos_idx"]),
            ("LPS", x["lps_idx"]),
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

def export_excel(
    result_df,
    output,
    stats,
):

    rename = {
        "code": "代码",
        "name": "名称",

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

        "LPS": "LPS候选",
        "LPS_date": "LPS日期",
        "LPS_volume_ratio": "LPS量能/SOS量能",
        "LPS_distance_pct": "现价距阻力%",

        "MACD_divergence": "MACD底背离",
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

        sheets = [
            (
                "Spring_Test主名单",
                result_df[
                    result_df["_main_pass"]
                ].sort_values(
                    "Spring_Test_score",
                    ascending=False,
                ),
            ),

            (
                "Spring早期观察",
                result_df[
                    result_df["_early_pass"]
                ].sort_values(
                    "Spring_early_score",
                    ascending=False,
                ),
            ),

            (
                "LPS备选",
                result_df[
                    result_df["_lps_pass"]
                ].sort_values(
                    "LPS_score",
                    ascending=False,
                ),
            ),

            (
                "排除名单",
                result_df[
                    ~(
                        result_df["_main_pass"]
                        | result_df["_early_pass"]
                        | result_df["_lps_pass"]
                    )
                ],
            ),

            (
                "全量扫描数据",
                result_df.sort_values(
                    [
                        "_main_pass",
                        "_early_pass",
                        "_lps_pass",
                    ],
                    ascending=False,
                ),
            ),
        ]

        for sheet_name, frame in sheets:

            export_df = (
                frame.drop(
                    columns=[
                        "_main_pass",
                        "_early_pass",
                        "_lps_pass",
                    ],
                    errors="ignore",
                )
                .rename(columns=rename)
            )

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
                    "先硬过滤、后独立评分；名单不凑数；所有事件均为规则候选，需人工看图复核。",
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
        help="名单最低评分",
    )

    args = parser.parse_args()

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
        "三个独立名单：Spring+Test / Spring早期 / LPS"
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

    print(
        f"主板非ST：{len(stocks)} 只"
    )

    # --------------------------------------------------------
    # 2. 市值过滤
    # --------------------------------------------------------

    if args.no_cap_filter:

        universe = stocks

        print(
            "已跳过流通市值过滤"
        )

    else:

        print(
            "[2/4] 获取腾讯行情流通市值..."
        )

        caps = fetch_float_caps(
            [
                code
                for code, _ in stocks
            ]
        )

        universe = [
            (code, name)
            for code, name in stocks
            if (
                code in caps
                and args.min_cap
                <= caps[code]
                <= args.max_cap
            )
        ]

        print(
            f"市值过滤后：{len(universe)} 只"
        )

        if not universe:

            print(
                "没有通过市值过滤的股票，可使用 --no-cap-filter 验证。"
            )

            return

    if args.limit > 0:
        universe = universe[:args.limit]

    # --------------------------------------------------------
    # 3. 扫描
    # --------------------------------------------------------

    print(
        f"[3/4] 扫描 {len(universe)} 只股票，并发 {args.threads}..."
    )

    results = []
    failed = 0

    with ThreadPoolExecutor(
        max_workers=max(1, args.threads)
    ) as pool:

        futures = {
            pool.submit(
                analyze_one,
                code,
                name,
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

    # --------------------------------------------------------
    # 4. 统计
    # --------------------------------------------------------

    stats = {
        "universe": len(universe),
        "success": len(df),
        "failed": failed,

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

    print(
        f"Spring+Test：{stats['main']} 只 "
        f"| Spring早期：{stats['early']} 只 "
        f"| LPS备选：{stats['lps']} 只"
    )

    # --------------------------------------------------------
    # 5. 打印名单
    # --------------------------------------------------------

    lists = [
        (
            "Spring+Test主名单",
            "_main_pass",
            "Spring_Test_score",
        ),
        (
            "Spring早期观察",
            "_early_pass",
            "Spring_early_score",
        ),
        (
            "LPS备选",
            "_lps_pass",
            "LPS_score",
        ),
    ]

    for title, flag, score_col in lists:

        part = (
            df[df[flag]]
            .sort_values(
                score_col,
                ascending=False,
            )
            .head(args.top)
        )

        print(
            f"\n--- {title} ---"
        )

        if part.empty:

            print(
                "本次没有符合硬性条件的股票。"
            )

        else:

            columns = [
                "code",
                "name",
                score_col,
                "Spring_date",
                "Test_date",
                "SOS_date",
                "LPS_date",
            ]

            print(
                part[columns].to_string(
                    index=False
                )
            )

    # --------------------------------------------------------
    # 6. 生成K线图
    # --------------------------------------------------------

    if args.charts > 0:

        chart_candidates = pd.concat(
            [
                df[
                    df["_main_pass"]
                ].sort_values(
                    "Spring_Test_score",
                    ascending=False,
                ).head(args.charts),

                df[
                    df["_early_pass"]
                ].sort_values(
                    "Spring_early_score",
                    ascending=False,
                ).head(args.charts),

                df[
                    df["_lps_pass"]
                ].sort_values(
                    "LPS_score",
                    ascending=False,
                ).head(args.charts),
            ]
        ).drop_duplicates(
            "code"
        ).head(
            args.charts
        )
        timestamp = datetime.now().strftime("%Y%m%d%H_%M")
        chart_dir = ROOT / f"charts_{timestamp}"

        count = 0

        for _, row in chart_candidates.iterrows():

            if draw_chart(
                row["code"],
                row["name"],
                chart_dir,
            ):
                count += 1

        print(
            f"K线图生成：{count} 张，目录：{chart_dir}"
        )


if __name__ == "__main__":
    main()