#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A股 Wyckoff 早期吸筹机会筛选器 3.3

升级重点：
1. 三个独立名单：Spring+Test完成、Spring早期观察、LPS备选。
2. Spring Test 必须满足“Spring -> 明确反弹 -> 缩量回踩”的顺序。
3. 结构硬过滤先于评分；结构失效或价格偏离不再靠高分进入主名单。
4. 输出全量扫描、排除原因、参数说明、三个策略名单及K线图。

说明：这是规则型初筛工具，不是对主力行为的确定性识别。
参数为第一轮回测初值，不是经过历史回测验证的最优参数。
"""

import argparse
import os
import sys
import time
import logging
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

ROOT = Path(__file__).resolve().parent
MAIN_PREFIXES = ("600", "601", "603", "605", "000", "001", "002", "003")
HISTORY_DAYS = 900
MIN_BARS = 250
CAP_MIN = 30.0
CAP_MAX = 300.0

# 3.3 初始参数：建议先 --limit 100 冒烟测试，再进行全市场扫描
P = {
    "range_min_days": 80,
    "range_max_days": 180,
    "range_max_height_pct": 25.0,
    "range_max_endpoint_change_pct": 30.0,
    "support_tolerance": 0.03,
    "resistance_tolerance": 0.03,
    "support_min_touches": 5,
    "resistance_min_touches": 4,
    "spring_recent_days": 30,
    "early_spring_recent_days": 15,
    "spring_penetration_min_pct": 0.3,
    "spring_penetration_max_pct": 3.5,
    "spring_min_close_pos": 0.55,
    "spring_max_volume_vs_avg": 1.8,
    "test_min_gap": 3,
    "test_max_gap": 20,
    "test_min_rebound_pct": 3.0,
    "test_max_volume_ratio": 0.80,
    "test_support_tolerance_pct": 3.5,
    "main_max_spring_distance_pct": 5.0,
    "main_support_distance_min_pct": -1.0,
    "main_support_distance_max_pct": 4.0,
    "early_max_spring_distance_pct": 5.0,
    "early_support_distance_min_pct": -1.0,
    "early_support_distance_max_pct": 4.0,
    "sos_recent_days": 60,
    "sos_min_breakout_pct": 0.5,
    "sos_min_volume_ratio": 1.30,
    "lps_min_gap": 2,
    "lps_max_gap": 25,
    "lps_max_volume_ratio": 0.80,
    "lps_level_tolerance_pct": 3.0,
    "lps_current_max_distance_pct": 5.0,
    "score_threshold": 80,
}

logging.basicConfig(
    filename=ROOT / "wyckoff_screen.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    encoding="utf-8",
)
LOG = logging.getLogger("wyckoff")


def tencent_symbol(code):
    code = str(code).zfill(6)
    return ("sh" if code.startswith(("6", "9", "5")) else "sz") + code


def get_stock_universe():
    df = ak.stock_info_a_code_name().copy()
    df["code"] = df["code"].astype(str).str.zfill(6)
    df["name"] = df["name"].astype(str)
    df = df[df["code"].str.startswith(MAIN_PREFIXES)]
    df = df[~df["name"].str.contains(r"ST|退", case=False, na=False)]
    return list(zip(df["code"].tolist(), df["name"].tolist()))


def fetch_float_caps(codes, pause=0.15):
    caps = {}
    session = requests.Session()
    for start in range(0, len(codes), 60):
        chunk = codes[start:start + 60]
        url = "https://qt.gtimg.cn/q=" + ",".join(tencent_symbol(c) for c in chunk)
        try:
            resp = session.get(url, timeout=12)
            resp.raise_for_status()
            resp.encoding = "gbk"
            for line in resp.text.split(";"):
                if '="' not in line:
                    continue
                parts = line.split("=", 1)[1].strip().strip('"').split("~")
                if len(parts) <= 44:
                    continue
                code = parts[2].strip()
                try:
                    value = float(parts[44])
                    if code and np.isfinite(value) and value > 0:
                        caps[code] = value
                except (TypeError, ValueError):
                    continue
        except Exception as exc:
            LOG.warning("腾讯市值批次获取失败：%s", exc)
        time.sleep(pause)
    return caps


def normalize_history(df):
    if df is None or df.empty:
        return None
    d = df.copy()
    d.columns = [str(c).strip().lower() for c in d.columns]
    rename = {
        "日期": "date", "开盘": "open", "收盘": "close",
        "最高": "high", "最低": "low", "成交量": "volume",
        "成交额": "amount", "换手率": "turnover",
    }
    d = d.rename(columns={k: v for k, v in rename.items() if k in d.columns})
    if not {"open", "close", "high", "low"}.issubset(d.columns):
        LOG.warning("日线缺少必要字段：%s", list(d.columns))
        return None
    if "volume" not in d.columns:
        if "amount" in d.columns:
            d["volume"] = pd.to_numeric(d["amount"], errors="coerce") / pd.to_numeric(d["close"], errors="coerce")
        else:
            return None
    for col in ("open", "close", "high", "low", "volume"):
        d[col] = pd.to_numeric(d[col], errors="coerce")
    if "date" in d.columns:
        d["date"] = pd.to_datetime(d["date"], errors="coerce")
        d = d.sort_values("date")
    d = d.dropna(subset=["open", "close", "high", "low", "volume"])
    d = d[(d["close"] > 0) & (d["low"] > 0) & (d["volume"] >= 0)]
    d = d.drop_duplicates(subset=["date"] if "date" in d.columns else None)
    return d.reset_index(drop=True) if len(d) >= MIN_BARS else None


def fetch_history(code):
    start = (datetime.now() - timedelta(days=HISTORY_DAYS)).strftime("%Y%m%d")
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
        LOG.warning("%s 行情获取失败：%s", code, exc)
        return None


def local_pivots(values, left=3, right=3, mode="low"):
    arr = np.asarray(values, dtype=float)
    out = []
    for i in range(left, len(arr) - right):
        w = arr[i-left:i+right+1]
        if mode == "low" and arr[i] <= np.min(w):
            out.append(i)
        elif mode == "high" and arr[i] >= np.max(w):
            out.append(i)
    return out


def cluster_price_levels(levels, tolerance=0.03):
    vals = sorted(float(x) for x in levels if np.isfinite(x) and x > 0)
    clusters = []
    for value in vals:
        if not clusters or abs(value / np.median(clusters[-1]) - 1) > tolerance:
            clusters.append([value])
        else:
            clusters[-1].append(value)
    if not clusters:
        return None, 0
    best = max(clusters, key=len)
    return float(np.median(best)), len(best)


def pct(a, b):
    return (a / b - 1) * 100 if b and np.isfinite(b) else np.nan


def detect_structure(df):
    close = df["close"].to_numpy(float)
    high = df["high"].to_numpy(float)
    low = df["low"].to_numpy(float)
    opn = df["open"].to_numpy(float)
    vol = df["volume"].to_numpy(float)
    n = len(close)

    # 找近期有效交易区间。保留原版“最长合格区间”思路，收紧振幅。
    range_info = None
    for length in range(min(P["range_max_days"], n), P["range_min_days"] - 1, -1):
        st = n - length
        ql, qh = float(np.quantile(low[st:], .10)), float(np.quantile(high[st:], .90))
        height = qh / ql - 1 if ql > 0 else 99
        endpoint = abs(close[-1] / close[st] - 1) * 100 if close[st] > 0 else 999
        if height * 100 <= P["range_max_height_pct"] and endpoint <= P["range_max_endpoint_change_pct"]:
            range_info = (st, length, ql, qh, height)
            break

    if range_info:
        range_start, range_days, range_low, range_high, range_height = range_info
        in_range = True
    else:
        range_start = n - min(P["range_max_days"], n)
        range_days = 0
        range_low = float(np.quantile(low[range_start:], .10))
        range_high = float(np.quantile(high[range_start:], .90))
        range_height = range_high / range_low - 1 if range_low > 0 else 99
        in_range = False

    prior_high = float(np.max(high[:range_start])) if range_start >= 30 else float(np.max(high[:max(1, n-180)]))
    drawdown = close[range_start] / prior_high - 1 if prior_high > 0 else 0

    seg_low, seg_high = low[range_start:], high[range_start:]
    lp = local_pivots(seg_low, 3, 3, "low")
    hp = local_pivots(seg_high, 3, 3, "high")

    def spaced(items, gap=5):
        result = []
        for k in items:
            if not result or k - result[-1] >= gap:
                result.append(k)
        return result

    lp, hp = spaced(lp), spaced(hp)
    support, support_touches = cluster_price_levels([seg_low[i] for i in lp], P["support_tolerance"])
    resistance, resistance_touches = cluster_price_levels([seg_high[i] for i in hp], P["resistance_tolerance"])
    support = range_low if support is None else support
    resistance = range_high if resistance is None else resistance

    segment_vol = vol[range_start:]
    third = max(10, len(segment_vol) // 3)
    v_first = float(np.mean(segment_vol[:third]))
    v_last = float(np.mean(segment_vol[-third:]))
    volume_contraction = v_last / v_first if v_first > 0 else 1.0

    # Spring：从最近的有效假跌破中，选择最后一个满足规则的事件。
    spring_idx = None
    if in_range and support_touches >= 3:
        begin = max(range_start + 10, n - P["spring_recent_days"])
        for i in range(begin, n):
            penetration = pct(support, low[i]) * -1  # low低于support时为正
            cpos = (close[i] - low[i]) / max(high[i] - low[i], 1e-9)
            base = float(np.mean(vol[max(range_start, i-20):i])) if i > range_start else 0
            valid = (
                close[i-1] >= support * .985
                and P["spring_penetration_min_pct"] <= penetration <= P["spring_penetration_max_pct"]
                and close[i] >= support * 1.001
                and cpos >= P["spring_min_close_pos"]
                and base > 0 and vol[i] <= base * P["spring_max_volume_vs_avg"]
            )
            if valid:
                spring_idx = i

    # Test：必须先从Spring低点反弹，再缩量回踩；不允许只按日期/低点机械匹配。
    test_idx = None
    rebound_pct = np.nan
    if spring_idx is not None:
        for j in range(spring_idx + P["test_min_gap"], min(n, spring_idx + P["test_max_gap"] + 1)):
            interim_high = float(np.max(high[spring_idx+1:j])) if j > spring_idx + 1 else low[spring_idx]
            rebound_pct_j = pct(interim_high, low[spring_idx])
            near_support = abs(pct(low[j], support)) <= P["test_support_tolerance_pct"]
            holds_spring_low = low[j] >= low[spring_idx] * .995
            lower_volume = vol[j] <= vol[spring_idx] * P["test_max_volume_ratio"]
            cpos = (close[j] - low[j]) / max(high[j] - low[j], 1e-9)
            stable_close = close[j] >= support * .99 and cpos >= .45
            if (rebound_pct_j >= P["test_min_rebound_pct"] and near_support
                    and holds_spring_low and lower_volume and stable_close):
                test_idx = j
                rebound_pct = rebound_pct_j
                break

    # Spring事件之后若价格有效跌破Spring低点，结构失效。
    spring_invalid = False
    if spring_idx is not None:
        later_lows = low[spring_idx+1:]
        later_closes = close[spring_idx+1:]
        if len(later_lows):
            spring_invalid = bool(np.min(later_closes) < low[spring_idx] * .99)

    # 早期Spring：事件较新、尚无Test；价格位置必须仍可观察。
    close_now = float(close[-1])
    spring_distance = pct(close_now, low[spring_idx]) if spring_idx is not None else np.nan
    support_distance = pct(close_now, support)
    days_since_spring = n - 1 - spring_idx if spring_idx is not None else None
    early_spring = bool(
        spring_idx is not None and days_since_spring <= P["early_spring_recent_days"]
        and test_idx is None and not spring_invalid
        and -1 <= spring_distance <= P["early_max_spring_distance_pct"]
        and P["early_support_distance_min_pct"] <= support_distance <= P["early_support_distance_max_pct"]
    )

    # Spring + Test完成：事件顺序成立，当前价仍处于相对低位。
    main_spring_test = bool(
        spring_idx is not None and test_idx is not None and not spring_invalid
        and days_since_spring <= P["spring_recent_days"]
        and 0 <= spring_distance <= P["main_max_spring_distance_pct"]
        and P["main_support_distance_min_pct"] <= support_distance <= P["main_support_distance_max_pct"]
        and close_now >= support * .99
    )

    # SOS/LPS 独立识别。SOS可发生在最近60日；LPS必须在SOS后回踩突破位。
    sos_idx = lps_idx = None
    sos_volume_ratio = lps_volume_ratio = np.nan
    if resistance > 0:
        sos_begin = max(range_start + 20, n - P["sos_recent_days"])
        for i in range(sos_begin, n):
            ref = float(np.mean(vol[max(range_start, i-20):i])) if i > range_start else 0
            cpos = (close[i] - low[i]) / max(high[i] - low[i], 1e-9)
            if close[i] >= resistance * (1 + P["sos_min_breakout_pct"]/100) and ref > 0 and vol[i] >= ref * P["sos_min_volume_ratio"] and cpos >= .65:
                sos_idx = i
                sos_volume_ratio = vol[i] / ref
                break

        if sos_idx is not None:
            for j in range(sos_idx + P["lps_min_gap"], min(n, sos_idx + P["lps_max_gap"] + 1)):
                near_level = resistance * (1 - P["lps_level_tolerance_pct"]/100) <= low[j] <= resistance * (1 + P["lps_level_tolerance_pct"]/100)
                ref = float(np.mean(vol[max(range_start, j-20):j])) if j > range_start else 0
                lower_than_sos = vol[j] <= vol[sos_idx] * P["lps_max_volume_ratio"]
                close_holds = close[j] >= resistance * .99
                if near_level and ref > 0 and lower_than_sos and vol[j] <= ref and close_holds:
                    lps_idx = j
                    lps_volume_ratio = vol[j] / vol[sos_idx]
                    break

    lps_distance = pct(close_now, resistance) if lps_idx is not None else np.nan
    lps_valid = bool(
        lps_idx is not None and close_now >= resistance * .97
        and close_now <= resistance * (1 + P["lps_current_max_distance_pct"]/100)
        and close_now >= low[lps_idx] * .98
    )

    # 背景指标
    ma20 = pd.Series(close).rolling(20).mean()
    ma60 = pd.Series(close).rolling(60).mean()
    ma20_slope = (ma20.iloc[-1] / ma20.iloc[-6] - 1) * 100 if len(ma20) > 6 and ma20.iloc[-6] else 0
    ma60_slope = (ma60.iloc[-1] / ma60.iloc[-6] - 1) * 100 if len(ma60) > 6 and ma60.iloc[-6] else 0
    ema12 = pd.Series(close).ewm(span=12, adjust=False).mean()
    ema26 = pd.Series(close).ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    macd_turn = bool(dif.iloc[-1] > dea.iloc[-1] and dif.iloc[-1] > dif.iloc[-3])
    pivots = local_pivots(low, 3, 3, "low")
    macd_div = False
    if len(pivots) >= 2:
        a, b = pivots[-2], pivots[-1]
        if b-a >= 10:
            macd_div = bool(low[b] < low[a] and dif.iloc[b] > dif.iloc[a])

    dates = pd.to_datetime(df["date"]) if "date" in df.columns else None
    def event_date(idx):
        return dates.iloc[idx].strftime("%Y-%m-%d") if idx is not None and dates is not None else ""

    # 交易区间硬条件：区间长度、振幅、支撑/阻力触点。
    range_quality = bool(
        in_range and range_days >= P["range_min_days"]
        and range_height * 100 <= P["range_max_height_pct"]
        and support_touches >= P["support_min_touches"]
        and resistance_touches >= P["resistance_min_touches"]
        and drawdown <= -.12
    )

    reasons = []
    if not in_range:
        reasons.append("未识别到合格交易区间")
    elif range_height * 100 > P["range_max_height_pct"]:
        reasons.append("交易区间振幅过大")
    if support_touches < P["support_min_touches"]:
        reasons.append("支撑触点不足")
    if resistance_touches < P["resistance_min_touches"]:
        reasons.append("阻力触点不足")
    if spring_idx is not None and spring_invalid:
        reasons.append("Spring后有效跌破Spring低点")
    if spring_idx is not None and test_idx is None:
        reasons.append("尚未形成合格Spring Test")
    if spring_idx is not None and spring_distance > 8:
        reasons.append("现价距离Spring低点超过8%")
    if spring_idx is not None and support_distance > 6:
        reasons.append("现价明显远离支撑")
    if not range_quality and not reasons:
        reasons.append("交易区间结构质量不足")

    return {
        "range_start": int(range_start), "range_days": int(range_days),
        "range_low": float(range_low), "range_high": float(range_high),
        "range_height_pct": float(range_height*100), "in_range": bool(in_range),
        "range_quality": range_quality, "drawdown_pct": float(drawdown*100),
        "support": float(support), "resistance": float(resistance),
        "support_touches": int(support_touches), "resistance_touches": int(resistance_touches),
        "volume_contraction": float(volume_contraction),
        "spring_idx": spring_idx, "test_idx": test_idx,
        "spring_date": event_date(spring_idx), "test_date": event_date(test_idx),
        "spring_low": float(low[spring_idx]) if spring_idx is not None else np.nan,
        "spring_volume": float(vol[spring_idx]) if spring_idx is not None else np.nan,
        "test_volume_ratio": float(vol[test_idx]/vol[spring_idx]) if test_idx is not None and vol[spring_idx] > 0 else np.nan,
        "spring_distance_pct": float(spring_distance) if np.isfinite(spring_distance) else np.nan,
        "distance_to_support_pct": float(support_distance),
        "days_since_spring": days_since_spring,
        "rebound_pct": float(rebound_pct) if np.isfinite(rebound_pct) else np.nan,
        "spring_invalid": spring_invalid, "early_spring": early_spring,
        "main_spring_test": main_spring_test,
        "sos_idx": sos_idx, "lps_idx": lps_idx,
        "sos_date": event_date(sos_idx), "lps_date": event_date(lps_idx),
        "sos_volume_ratio": float(sos_volume_ratio) if np.isfinite(sos_volume_ratio) else np.nan,
        "lps_volume_ratio": float(lps_volume_ratio) if np.isfinite(lps_volume_ratio) else np.nan,
        "lps_valid": lps_valid, "lps_distance_pct": float(lps_distance) if np.isfinite(lps_distance) else np.nan,
        "sc": False, "ar": False, "st": False,
        "spring": spring_idx is not None, "spring_test": test_idx is not None,
        "sos": sos_idx is not None, "lps": lps_valid,
        "macd_divergence": macd_div, "macd_turn_up": macd_turn,
        "ma20_slope_pct": float(ma20_slope), "ma60_slope_pct": float(ma60_slope),
        "close": close_now, "ma20": float(ma20.iloc[-1]), "ma60": float(ma60.iloc[-1]),
        "exclude_reasons": "；".join(dict.fromkeys(reasons)),
    }


def score_lists(x):
    # 分开评分：每个名单只评价自己的结构，不共享一个总分排名。
    common = (15 if x["range_quality"] else 0) + (10 if x["volume_contraction"] <= .85 else 5 if x["volume_contraction"] <= 1 else 0)
    spring_score = (
        common + (25 if x["spring"] else 0)
        + (20 if x["rebound_pct"] >= 3 else 0)
        + (20 if x["spring_test"] and x["test_volume_ratio"] <= .8 else 0)
        + (10 if not x["spring_invalid"] else 0)
    )
    early_score = (
        common + (35 if x["spring"] else 0)
        + (20 if x["days_since_spring"] is not None and x["days_since_spring"] <= 15 else 0)
        + (15 if -1 <= x["spring_distance_pct"] <= 5 else 0)
        + (15 if -1 <= x["distance_to_support_pct"] <= 4 else 0)
    )
    lps_score = (
        (20 if x["range_quality"] else 0)
        + (30 if x["sos"] else 0)
        + (25 if x["lps_valid"] else 0)
        + (15 if x["lps_volume_ratio"] <= .8 else 0)
        + (10 if x["distance_to_support_pct"] >= -1 else 0)
    )
    return min(100, int(spring_score)), min(100, int(early_score)), min(100, int(lps_score))


def analyze_one(code, name):
    hist = fetch_history(code)
    if hist is None:
        return None
    try:
        x = detect_structure(hist)
        s_score, e_score, l_score = score_lists(x)
        row = {
            "code": code, "name": name,
            "Spring_Test_score": s_score, "Spring_early_score": e_score, "LPS_score": l_score,
            "range_quality": x["range_quality"], "range_days": x["range_days"],
            "range_low": x["range_low"], "range_high": x["range_high"],
            "range_height_pct": x["range_height_pct"], "drawdown_pct": x["drawdown_pct"],
            "support": x["support"], "resistance": x["resistance"],
            "support_touches": x["support_touches"], "resistance_touches": x["resistance_touches"],
            "volume_contraction": x["volume_contraction"],
            "Spring": x["spring"], "Spring_date": x["spring_date"], "Spring_low": x["spring_low"],
            "Spring_distance_pct": x["spring_distance_pct"], "days_since_spring": x["days_since_spring"],
            "Spring_rebound_pct": x["rebound_pct"], "Spring_invalid": x["spring_invalid"],
            "Spring_Test": x["spring_test"], "Test_date": x["test_date"],
            "Test_volume_ratio": x["test_volume_ratio"],
            "distance_to_support_pct": x["distance_to_support_pct"],
            "SOS": x["sos"], "SOS_date": x["sos_date"], "SOS_volume_ratio": x["sos_volume_ratio"],
            "LPS": x["lps"], "LPS_date": x["lps_date"], "LPS_volume_ratio": x["lps_volume_ratio"],
            "LPS_distance_pct": x["lps_distance_pct"],
            "MACD_divergence": x["macd_divergence"], "MACD_turn_up": x["macd_turn_up"],
            "MA20_slope_pct": x["ma20_slope_pct"], "MA60_slope_pct": x["ma60_slope_pct"],
            "close": x["close"], "ma20": x["ma20"], "ma60": x["ma60"],
            "exclude_reasons": x["exclude_reasons"],
            "_main_pass": x["main_spring_test"] and s_score >= P["score_threshold"],
            "_early_pass": x["early_spring"] and e_score >= P["score_threshold"],
            "_lps_pass": x["lps_valid"] and l_score >= P["score_threshold"],
        }
        return row
    except Exception as exc:
        LOG.exception("%s 分析失败：%s", code, exc)
        return None


def draw_chart(code, name, out_dir):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return False
    hist = fetch_history(code)
    if hist is None:
        return False
    x = detect_structure(hist)
    d = hist.tail(180).reset_index(drop=True)
    offset = len(hist) - len(d)
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "SimSun", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    fig, (ax, av) = plt.subplots(2, 1, figsize=(14, 9), sharex=True, gridspec_kw={"height_ratios": [3, 1]})
    xx = np.arange(len(d))
    for i, r in d.iterrows():
        color = "red" if r["close"] >= r["open"] else "green"
        ax.vlines(i, r["low"], r["high"], color=color, linewidth=.8)
        ax.bar(i, max(abs(r["close"]-r["open"]), .001), bottom=min(r["open"], r["close"]), width=.6, color=color, edgecolor=color)
    ax.plot(xx, d["close"].rolling(20).mean(), label="MA20", linewidth=1)
    ax.plot(xx, d["close"].rolling(60).mean(), label="MA60", linewidth=1)
    ax.axhline(x["support"], linestyle="--", linewidth=.9, label="Support")
    ax.axhline(x["resistance"], linestyle="--", linewidth=.9, label="Resistance")
    for label, idx in [("Spring", x["spring_idx"]), ("Test", x["test_idx"]), ("SOS", x["sos_idx"]), ("LPS", x["lps_idx"])]:
        if idx is not None and offset <= idx < len(hist):
            xi = idx - offset
            ax.annotate(label, (xi, float(hist.iloc[idx]["low"])), xytext=(0, -16), textcoords="offset points",
                        ha="center", fontsize=8, arrowprops={"arrowstyle": "->", "lw": .6})
    ax.set_title(f"{name} ({code}) - Wyckoff 3.3")
    ax.legend(loc="best", fontsize=8, ncol=3)
    colors = ["red" if r["close"] >= r["open"] else "green" for _, r in d.iterrows()]
    av.bar(xx, d["volume"], color=colors, width=.7)
    av.set_ylabel("Volume")
    out_dir.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(out_dir / f"{code}_{name}.png", dpi=130)
    plt.close(fig)
    return True


def export_excel(result_df, output, stats):
    rename = {
        "code": "代码", "name": "名称",
        "Spring_Test_score": "Spring_Test质量分", "Spring_early_score": "Spring早期质量分", "LPS_score": "LPS质量分",
        "range_quality": "交易区间质量通过", "range_days": "有效区间天数",
        "range_low": "区间下沿", "range_high": "区间上沿", "range_height_pct": "区间振幅%",
        "drawdown_pct": "距前期高点涨跌%", "support": "支撑区域", "resistance": "阻力区域",
        "support_touches": "支撑触点数", "resistance_touches": "阻力触点数",
        "volume_contraction": "后段/前段均量比",
        "Spring": "Spring候选", "Spring_date": "Spring日期", "Spring_low": "Spring最低价",
        "Spring_distance_pct": "现价距Spring低点%", "days_since_spring": "Spring距今天数",
        "Spring_rebound_pct": "Spring后最大反弹%", "Spring_invalid": "Spring结构失效",
        "Spring_Test": "Spring_Test完成", "Test_date": "Test日期", "Test_volume_ratio": "Test量能/Spring量能",
        "distance_to_support_pct": "现价距支撑%",
        "SOS": "SOS候选", "SOS_date": "SOS日期", "SOS_volume_ratio": "SOS量能/前20日均量",
        "LPS": "LPS候选", "LPS_date": "LPS日期", "LPS_volume_ratio": "LPS量能/SOS量能",
        "LPS_distance_pct": "现价距阻力%", "MACD_divergence": "MACD底背离",
        "MACD_turn_up": "MACD拐头", "MA20_slope_pct": "MA20斜率%", "MA60_slope_pct": "MA60斜率%",
        "close": "收盘价", "ma20": "MA20", "ma60": "MA60", "exclude_reasons": "未入选/复核原因",
    }
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        sheets = [
            ("Spring_Test主名单", result_df[result_df["_main_pass"]].sort_values("Spring_Test_score", ascending=False)),
            ("Spring早期观察", result_df[result_df["_early_pass"]].sort_values("Spring_early_score", ascending=False)),
            ("LPS备选", result_df[result_df["_lps_pass"]].sort_values("LPS_score", ascending=False)),
            ("排除名单", result_df[~(result_df["_main_pass"] | result_df["_early_pass"] | result_df["_lps_pass"])]),
            ("全量扫描数据", result_df.sort_values(["_main_pass", "_early_pass", "_lps_pass"], ascending=False)),
        ]
        for name, frame in sheets:
            frame.drop(columns=["_main_pass", "_early_pass", "_lps_pass"], errors="ignore").rename(columns=rename).to_excel(writer, sheet_name=name, index=False)
        pd.DataFrame([["扫描时间", datetime.now().strftime("%Y-%m-%d %H:%M:%S")],
                      ["扫描股票数", stats["universe"]], ["成功分析", stats["success"]], ["失败数量", stats["failed"]],
                      ["Spring+Test名单数", stats["main"]], ["Spring早期名单数", stats["early"]], ["LPS备选名单数", stats["lps"]],
                      ["策略说明", "先硬过滤、后独立评分；名单不凑数；所有事件均为规则候选，需人工看图复核。"] ,
                      ["参数说明", str(P)]], columns=["项目", "内容"]).to_excel(writer, sheet_name="扫描说明", index=False)


def main():
    parser = argparse.ArgumentParser(description="A股 Wyckoff 早期吸筹机会筛选器 3.3")
    parser.add_argument("--threads", type=int, default=5)
    parser.add_argument("--limit", type=int, default=0, help="只扫描前N只，0为全量")
    parser.add_argument("--top", type=int, default=30)
    parser.add_argument("--charts", type=int, default=5, help="生成K线图数量，0关闭")
    parser.add_argument("--no-cap-filter", action="store_true")
    parser.add_argument("--min-cap", type=float, default=CAP_MIN)
    parser.add_argument("--max-cap", type=float, default=CAP_MAX)
    parser.add_argument("--score-threshold", type=int, default=P["score_threshold"])
    args = parser.parse_args()
    P["score_threshold"] = max(0, min(100, args.score_threshold))

    print("=" * 72)
    print("A股 Wyckoff 早期吸筹机会筛选器 3.3")
    print("三个独立名单：Spring+Test / Spring早期 / LPS")
    print("规则型初筛，不代表确定的主力吸筹；请人工复核K线")
    print("=" * 72)

    print("[1/4] 获取沪深主板股票列表...")
    stocks = get_stock_universe()
    print(f"主板非ST：{len(stocks)} 只")
    if args.no_cap_filter:
        universe = stocks
        print("已跳过流通市值过滤")
    else:
        print("[2/4] 获取腾讯行情流通市值...")
        caps = fetch_float_caps([c for c, _ in stocks])
        universe = [(c, n) for c, n in stocks if c in caps and args.min_cap <= caps[c] <= args.max_cap]
        print(f"市值过滤后：{len(universe)} 只")
        if not universe:
            print("没有通过市值过滤的股票，可使用 --no-cap-filter 验证。")
            return
    if args.limit > 0:
        universe = universe[:args.limit]

    print(f"[3/4] 扫描 {len(universe)} 只股票，并发 {args.threads}...")
    results, failed = [], 0
    with ThreadPoolExecutor(max_workers=max(1, args.threads)) as pool:
        futures = {pool.submit(analyze_one, c, n): (c, n) for c, n in universe}
        for idx, future in enumerate(as_completed(futures), 1):
            try:
                r = future.result()
                if r is None:
                    failed += 1
                else:
                    results.append(r)
            except Exception as exc:
                failed += 1
                LOG.exception("任务异常：%s", exc)
            if idx % 50 == 0 or idx == len(futures):
                print(f"进度 {idx}/{len(futures)} | 成功 {len(results)} | 失败 {failed}", flush=True)
    if not results:
        print("没有成功分析的股票，请检查网络、AkShare版本和日志。")
        return

    df = pd.DataFrame(results)
    stats = {
        "universe": len(universe), "success": len(df), "failed": failed,
        "main": int(df["_main_pass"].sum()), "early": int(df["_early_pass"].sum()), "lps": int(df["_lps_pass"].sum()),
    }
    today = datetime.now().strftime("%Y%m%d")
    output = ROOT / f"wyckoff_screen_3_3_{today}.xlsx"
    export_excel(df, output, stats)

    print(f"\n[4/4] Excel已导出：{output}")
    print(f"成功分析 {len(df)} 只，失败 {failed} 只")
    print(f"Spring+Test：{stats['main']} 只 | Spring早期：{stats['early']} 只 | LPS：{stats['lps']} 只")
    for title, flag, score_col in [
        ("Spring+Test主名单", "_main_pass", "Spring_Test_score"),
        ("Spring早期观察", "_early_pass", "Spring_early_score"),
        ("LPS备选", "_lps_pass", "LPS_score"),
    ]:
        part = df[df[flag]].sort_values(score_col, ascending=False).head(args.top)
        print(f"\n--- {title} ---")
        if part.empty:
            print("本次没有符合硬性条件的股票（这是允许的，不会降低标准凑数）。")
        else:
            print(part[["code", "name", score_col, "Spring_date", "Test_date", "SOS_date", "LPS_date"]].to_string(index=False))

    if args.charts > 0:
        chart_candidates = pd.concat([
            df[df["_main_pass"]].sort_values("Spring_Test_score", ascending=False).head(args.charts),
            df[df["_early_pass"]].sort_values("Spring_early_score", ascending=False).head(args.charts),
            df[df["_lps_pass"]].sort_values("LPS_score", ascending=False).head(args.charts),
        ]).drop_duplicates("code").head(args.charts)
        chart_dir = ROOT / "charts_3_3"
        count = 0
        for _, row in chart_candidates.iterrows():
            if draw_chart(row["code"], row["name"], chart_dir):
                count += 1
        print(f"K线图生成：{count} 张，目录：{chart_dir}")


if __name__ == "__main__":
    main()
