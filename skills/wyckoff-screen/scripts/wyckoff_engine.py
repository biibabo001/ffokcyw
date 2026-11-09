"""
Wyckoff 2.0 Volume Profile + Phase 检测
efinance 数据接口版
"""

import numpy as np
import pandas as pd
from scipy.signal import argrelmax, argrelmin


def detect_columns(df: pd.DataFrame) -> dict:
    """自动检测并映射列名"""
    col_map = {}
    for c in df.columns:
        c_lower = str(c).lower()
        if '开' in c or 'open' in c_lower:
            col_map['open'] = c
        elif '收' in c or 'close' in c_lower:
            col_map['close'] = c
        elif '高' in c or 'high' in c_lower:
            col_map['high'] = c
        elif '低' in c or 'low' in c_lower:
            col_map['low'] = c
        elif '成交量' in c or 'volume' in c_lower or 'vol' in c_lower:
            col_map['volume'] = c
        elif '成交额' in c or 'amount' in c_lower:
            col_map['amount'] = c
        elif '日期' in c or 'date' in c_lower:
            col_map['date'] = c
        elif '代码' in c or 'code' in c_lower:
            col_map['code'] = c
        elif '名称' in c or 'name' in c_lower:
            col_map['name'] = c
    return col_map


def calculate_vp(df: pd.DataFrame, lookback: int = 60) -> dict:
    """计算 Volume Profile（VPOC/VAH/VAL/HVN/LVN）"""
    if len(df) < 20:
        return None
    cols = detect_columns(df)
    if not all(k in cols for k in ['close', 'high', 'low', 'volume']):
        print(f"  [warn] 列名检测失败: {df.columns.tolist()}")
        return None
    df = df.tail(lookback).copy()
    col_close = cols['close']
    col_high = cols['high']
    col_low = cols['low']
    col_volume = cols['volume']

    min_price = df[col_low].min()
    max_price = df[col_high].max()
    price_range = max_price - min_price
    if price_range == 0:
        return None

    n_bins = 50
    bin_size = price_range / n_bins
    volume_at_price = np.zeros(n_bins)
    for _, row in df.iterrows():
        low_bin = max(0, int((row[col_low] - min_price) / bin_size))
        high_bin = min(n_bins - 1, int((row[col_high] - min_price) / bin_size))
        vol_per_bin = row[col_volume] / (high_bin - low_bin + 1) if high_bin >= low_bin else row[col_volume]
        for b in range(low_bin, high_bin + 1):
            volume_at_price[b] += vol_per_bin

    price_levels = np.array([min_price + (i + 0.5) * bin_size for i in range(n_bins)])
    vpoc_idx = np.argmax(volume_at_price)
    vpoc_price = price_levels[vpoc_idx]
    vpoc_volume = volume_at_price[vpoc_idx]

    # Value Area（68.2%）
    total_volume = volume_at_price.sum()
    va_threshold = total_volume * 0.682
    cumsum = 0
    va_low_idx = vpoc_idx
    va_high_idx = vpoc_idx
    while cumsum < va_threshold and (va_low_idx > 0 or va_high_idx < n_bins - 1):
        low_vol = volume_at_price[va_low_idx - 1] if va_low_idx > 0 else 0
        high_vol = volume_at_price[va_high_idx + 1] if va_high_idx < n_bins - 1 else 0
        if low_vol > high_vol:
            cumsum += low_vol
            va_low_idx -= 1
        else:
            cumsum += high_vol
            va_high_idx += 1

    val_price = price_levels[va_low_idx]
    vah_price = price_levels[va_high_idx]

    # HVN / LVN
    hvn_indices = argrelmax(volume_at_price, order=3)[0]
    hvn_levels = [{'price': round(price_levels[i], 2), 'volume': round(volume_at_price[i], 0)}
                  for i in hvn_indices if volume_at_price[i] > vpoc_volume * 0.5]
    lvn_indices = argrelmin(volume_at_price, order=3)[0]
    lvn_levels = [{'price': round(price_levels[i], 2), 'volume': round(volume_at_price[i], 0)}
                  for i in lvn_indices if volume_at_price[i] < vpoc_volume * 0.3]

    current_price = df[col_close].iloc[-1]
    if current_price > vah_price:
        position = 'above_vah'
    elif current_price > vpoc_price:
        position = 'above_vpoc'
    elif current_price > val_price:
        position = 'within_va'
    elif current_price > vpoc_price:
        position = 'below_vpoc'
    else:
        position = 'below_val'

    return {
        'vpoc': round(vpoc_price, 2),
        'vpoc_vol': round(vpoc_volume, 0),
        'vah': round(vah_price, 2),
        'val': round(val_price, 2),
        'hvn': hvn_levels,
        'lvn': lvn_levels,
        'cur': round(current_price, 2),
        'position': position,
        'min_p': round(min_price, 2),
        'max_p': round(max_price, 2),
        'total_vol': round(total_volume, 0),
    }


def detect_phase(df: pd.DataFrame, lookback: int = 120) -> dict:
    """识别 Wyckoff Phase（A/B/C/D/E）"""
    if len(df) < 40:
        return {'phase': 'unknown', 'dir': 'unknown', 'conf': 0}
    cols = detect_columns(df)
    if not all(k in cols for k in ['close', 'high', 'low', 'volume']):
        return {'phase': 'unknown', 'dir': 'unknown', 'conf': 0}

    df = df.tail(lookback).copy()
    col_close = cols['close']
    col_high = cols['high']
    col_low = cols['low']
    col_volume = cols['volume']

    df['vol_ma20'] = df[col_volume].rolling(20).mean()
    df['ma20'] = df[col_close].rolling(20).mean()
    df['ma60'] = df[col_close].rolling(60).mean()

    cur = df[col_close].iloc[-1]
    ma20 = df['ma20'].iloc[-1]
    ma60 = df['ma60'].iloc[-1]
    vol_ma = df['vol_ma20'].iloc[-1]

    rolling_high = df[col_high].tail(60).max()
    rolling_low = df[col_low].tail(60).min()
    range_w = rolling_high - rolling_low
    pct = (cur - rolling_low) / range_w if range_w > 0 else 0.5
    vol_r = df[col_volume].iloc[-1] / vol_ma if vol_ma > 0 else 1
    avg_v = df[col_volume].tail(20).mean()

    # Phase E: 趋势
    if ma20 > ma60 and cur > ma20 and pct > 0.7:
        return {'phase': 'E', 'dir': 'uptrend', 'conf': 80 if vol_r > 1.2 else 65}
    if ma20 < ma60 and cur < ma20 and pct < 0.3:
        return {'phase': 'E', 'dir': 'downtrend', 'conf': 80 if vol_r > 1.2 else 65}

    # Phase E 延伸
    if ma20 > ma60 and cur < ma20 and pct > 0.3:
        if vol_r < 0.9:
            return {'phase': 'E', 'dir': 'uptrend_pullback', 'conf': 70}
        else:
            return {'phase': 'C', 'dir': 'spring_test', 'conf': 65}
    if ma20 < ma60 and cur > ma20 and pct < 0.7:
        if vol_r < 0.9:
            return {'phase': 'E', 'dir': 'downtrend_pullback', 'conf': 70}
        else:
            return {'phase': 'C', 'dir': 'upthrust_test', 'conf': 65}

    # Phase A: 停止
    if vol_r > 2.0 and 0.2 < pct < 0.8:
        return {'phase': 'A', 'dir': 'stopping', 'conf': 70}

    # Phase B: 横盘
    if 0.25 <= pct <= 0.7:
        if avg_v < vol_ma * 0.9:
            return {'phase': 'B', 'dir': 'accumulation', 'conf': 75}
        else:
            return {'phase': 'B', 'dir': 'distribution', 'conf': 70}

    # Phase C: 测试
    if 0.10 <= pct <= 0.4:
        if vol_r > 1.2:
            return {'phase': 'C', 'dir': 'spring_test', 'conf': 68}
        else:
            return {'phase': 'B', 'dir': 'accumulation', 'conf': 65}
    if 0.6 <= pct <= 0.9:
        if vol_r > 1.2:
            return {'phase': 'C', 'dir': 'upthrust_test', 'conf': 68}
        else:
            return {'phase': 'B', 'dir': 'distribution', 'conf': 65}

    # Phase D: 突破
    if pct > 0.85 and vol_r > 1.2:
        return {'phase': 'D', 'dir': 'breakout_up', 'conf': 72}
    if pct < 0.10 and vol_r > 1.2:
        return {'phase': 'D', 'dir': 'breakout_down', 'conf': 72}

    # 极低位
    if pct < 0.10:
        return {'phase': 'C', 'dir': 'spring_test', 'conf': 60}

    return {'phase': 'unknown', 'dir': 'unknown', 'conf': 40}


def detect_recent_spring(df: pd.DataFrame, days: int = 5, pierce_pct: float = 0.005) -> bool:
    """最近N个交易日内是否出现Spring：跌破近期支撑后又收回支撑上方"""
    cols = detect_columns(df)
    if not all(k in cols for k in ['close', 'low']):
        return False
    if len(df) < days + 5:
        return False

    col_close = cols['close']
    col_low = cols['low']

    recent = df.tail(days)
    prior = df.iloc[:-days]
    support = prior[col_low].tail(20).min()  # 最近N日之前的20日低点作为支撑
    spring_low = recent[col_low].min()
    cur_close = df[col_close].iloc[-1]

    pierced = spring_low < support * (1 - pierce_pct)   # 跌破支撑至少0.5%
    recovered = cur_close > support                      # 已收回支撑上方
    return bool(pierced and recovered)


def detect_wyckoff_events(df: pd.DataFrame, lookback: int = 250) -> dict | None:
    """
    识别威科夫积累结构事件（摆动点法）：
    PS首次支撑 / SC恐慌抛售 / AR自动反弹 / ST二次测试 / Creek阻力 / Spring洗盘 / LPS最后支撑(缩量) / SOS突破
    返回: {事件名: {'idx','price',...} | None, 'support': 支撑价, 'creek': 阻力价}
    """
    cols = detect_columns(df)
    if not all(k in cols for k in ['high', 'low', 'close', 'volume']):
        return None

    d = df.tail(lookback).reset_index(drop=True)
    high = d[cols['high']].astype(float).values
    low = d[cols['low']].astype(float).values
    close = d[cols['close']].astype(float).values
    vol = d[cols['volume']].astype(float).values
    n = len(d)
    if n < 80:
        return None

    vol_ma = pd.Series(vol).rolling(20).mean().values
    avg_vol = float(np.nanmean(vol_ma[-20:])) if np.isfinite(vol_ma[-1]) else float(np.nanmean(vol))
    if avg_vol <= 0:
        return None

    hi = argrelmax(high, order=3)[0]
    lo = argrelmin(low, order=3)[0]
    hi = hi[(hi > 0) & (hi < n - 1)]
    lo = lo[(lo > 0) & (lo < n - 1)]
    if len(hi) < 2 or len(lo) < 3:
        return None

    events = {
        'PS': None, 'SC': None, 'AR': None, 'ST': None,
        'Creek': None, 'Spring': None, 'LPS': None, 'SOS': None,
    }
    sc_i = ar_i = st_i = spring_i = None

    # --- SC 恐慌抛售：窗口前60%内，放量(>=1.5倍均量)且价格最低的摆动低点 ---
    for i in lo:
        if i <= n * 0.6 and vol[i] >= 1.5 * avg_vol:
            if sc_i is None or low[i] < low[sc_i]:
                sc_i = i
    if sc_i is None:
        return None
    events['SC'] = {'idx': int(sc_i), 'price': round(float(low[sc_i]), 2),
                    'vol_ratio': round(float(vol[sc_i]) / avg_vol, 1)}

    # --- PS 首次支撑：SC 之前最近的一个摆动低点 ---
    ps_cands = [i for i in lo if i < sc_i]
    if ps_cands:
        ps_i = max(ps_cands)
        events['PS'] = {'idx': int(ps_i), 'price': round(float(low[ps_i]), 2)}
    else:
        return None

    # --- AR 自动反弹：SC 之后第一个摆动高点 ---
    ar_cands = [i for i in hi if i > sc_i]
    if not ar_cands:
        return None
    ar_i = int(min(ar_cands))
    events['AR'] = {'idx': ar_i, 'price': round(float(high[ar_i]), 2)}

    # --- ST 二次测试：AR 之后贴近 SC 低点(±6%)且缩量(<0.8倍均量)的摆动低点 ---
    for i in lo:
        if i <= ar_i:
            continue
        if abs(low[i] - low[sc_i]) / low[sc_i] <= 0.06 and vol[i] < 0.8 * avg_vol:
            st_i = int(i)
            events['ST'] = {'idx': st_i, 'price': round(float(low[i]), 2),
                            'vol_ratio': round(float(vol[i]) / avg_vol, 1)}
            break
    if events['ST'] is None:
        fallback = [i for i in lo if i > ar_i]
        if fallback:
            st_i = int(fallback[0])
            events['ST'] = {'idx': st_i, 'price': round(float(low[st_i]), 2),
                            'vol_ratio': round(float(vol[st_i]) / avg_vol, 1)}
        else:
            return None

    # --- Creek 阻力区域：AR高点及之后形成的摆动高点密集区上沿 ---
    creek_cands = [high[i] for i in hi if i >= ar_i and i < n - 15]
    if not creek_cands:
        creek_cands = [high[ar_i]]
    creek = float(max(creek_cands))
    events['Creek'] = {'price': round(creek, 2)}

    # --- 支撑：SC 与 ST 低点中较低者 ---
    support = float(min(low[sc_i], low[st_i]))

    # --- Spring 洗盘：ST 之后跌破支撑(>=0.5%)且其后5根内收盘收回支撑上方，取最近一次 ---
    for i in sorted(lo, reverse=True):
        if i <= st_i or i >= n - 2:
            continue
        if low[i] < support * 0.995:
            fut = close[i + 1:i + 6]
            if len(fut) and float(np.max(fut)) > support:
                spring_i = int(i)
                events['Spring'] = {
                    'idx': spring_i,
                    'price': round(float(low[i]), 2),
                    'pierce_pct': round((support - low[i]) / support * 100, 2),
                }
                break

    # --- LPS 最后支撑：Spring 之后高于 Spring 低点且缩量(<1倍均量)的摆动低点 ---
    if spring_i is not None:
        for i in lo:
            if i > spring_i and low[i] > low[spring_i] * 1.005 and vol[i] < avg_vol:
                events['LPS'] = {'idx': int(i), 'price': round(float(low[i]), 2),
                                 'vol_ratio': round(float(vol[i]) / avg_vol, 1)}
                break

    # --- SOS 突破：最近15根内收盘站上 Creek 且放量(>=1.5倍均量) ---
    for i in range(max(n - 15, 0), n):
        if close[i] > creek and vol[i] >= 1.5 * avg_vol:
            events['SOS'] = {'idx': int(i), 'price': round(float(close[i]), 2),
                             'vol_ratio': round(float(vol[i]) / avg_vol, 1)}
            break

    events['support'] = round(support, 2)
    events['creek'] = round(creek, 2)
    events['cur'] = round(float(close[-1]), 2)
    events['n'] = n
    return events


def format_wyckoff_events(events: dict) -> str:
    """把事件检测结果压缩成一行信号文本"""
    if not events:
        return '威科夫结构: 无'
    found = [k for k in ('PS', 'SC', 'AR', 'ST', 'Creek', 'Spring', 'LPS', 'SOS') if events.get(k)]
    names = {'PS': '首次支撑', 'SC': '恐慌抛售', 'AR': '自动反弹', 'ST': '二次测试',
             'Creek': '阻力区', 'Spring': '洗盘', 'LPS': '最后支撑', 'SOS': '强势突破'}
    detail = ' '.join(f'{k}({names[k]})' for k in found)
    return f'威科夫结构: {detail} 支撑{events.get("support")}/阻力{events.get("creek")}'


def score_wyckoff_structure(events: dict, recent_days: int = 25) -> dict:
    """
    威科夫积累结构完整度评分（0~100）
    基础: PS5 + SC20 + AR10 + ST15；Creek10；Spring25（需近期）；LPS10（需近期）；SOS5（需近期）
    阈值: >=75 具备完整积累结构
    """
    if not events:
        return {'score': 0, 'rating': 'N', 'pass': False, 'signals': [], 'verdict': '无威科夫结构'}

    n = events.get('n', 0)
    sc = 0
    signals = []

    def recent(idx):
        return idx is not None and n > 0 and (n - idx) <= recent_days

    if events.get('PS'):
        sc += 5
        signals.append(f"PS首次支撑@{events['PS']['price']}")
    if events.get('SC'):
        sc += 20
        signals.append(f"SC恐慌抛售@{events['SC']['price']}({events['SC'].get('vol_ratio')}x量)")
    if events.get('AR'):
        sc += 10
        signals.append(f"AR自动反弹@{events['AR']['price']}")
    if events.get('ST'):
        sc += 15
        signals.append(f"ST二次测试@{events['ST']['price']}({events['ST'].get('vol_ratio')}x量)")
    if events.get('Creek'):
        sc += 10
        signals.append(f"Creek阻力区@{events['creek']}")
    if events.get('Spring') and recent(events['Spring'].get('idx')):
        sc += 25
        signals.append(f"Spring洗盘@{events['Spring']['price']}(跌破{events['Spring'].get('pierce_pct')}%)")
    if events.get('LPS') and recent(events['LPS'].get('idx')):
        sc += 10
        signals.append(f"LPS最后支撑@{events['LPS']['price']}(缩量{events['LPS'].get('vol_ratio')}x)")
    if events.get('SOS') and recent(events['SOS'].get('idx')):
        sc += 5
        signals.append(f"SOS强势突破@{events['SOS']['price']}({events['SOS'].get('vol_ratio')}x量)")

    if sc >= 95:
        rating, verdict = 'S', '结构完整(LPS确认)，即将启动或已启动，重点关注'
    elif sc >= 85:
        rating, verdict = 'A', '积累结构完成(Spring洗盘后)，等待LPS/SOS确认'
    elif sc >= 75:
        rating, verdict = 'B', '具备积累结构，观察Creek突破'
    else:
        rating, verdict = 'C', '结构不完整，暂不关注'

    return {'score': sc, 'rating': rating, 'pass': sc >= 75,
            'signals': signals, 'verdict': verdict}


# ============================================================
#  右侧评分（趋势确认，跟随买入）
# ============================================================
def score_stock_right(df: pd.DataFrame) -> dict:
    """右侧趋势跟随评分——Phase E/D 突破确认后买入"""
    prof = calculate_vp(df)
    ph = detect_phase(df)
    if not prof or ph['phase'] == 'unknown':
        return {
            'pass': False, 'score': 0, 'red_flags': [], 'green_flags': [],
            'signals': [], 'rating': 'N', 'verdict': '数据不足，无法判断',
            'phase': ph, 'profile': prof, 'side': 'right',
        }

    green, red, sc = [], [], 0
    cols = detect_columns(df)
    vol_col = cols.get('volume', None)
    avg5 = df[vol_col].tail(5).mean() if vol_col else 0
    avg60 = df[vol_col].tail(60).mean() if vol_col else 0

    # --- 危险信号 ---
    if ph['phase'] == 'E' and ph['dir'] == 'downtrend':
        red.append(f"⚠️ Phase E 下跌趋势，坚决回避")
        sc -= 30
    if ph['phase'] in ['B', 'C'] and ph['dir'] in ['distribution', 'upthrust_test']:
        red.append(f"🔴 Phase {ph['phase']}({ph['dir']}) = 派发/诱多，不宜买入")
        sc -= 20
    if prof['cur'] < prof['val']:
        red.append(f"🔴 价格在VAL({prof['val']})下方，偏弱")
        sc -= 15
    if prof['cur'] < prof['vpoc']:
        red.append(f"🔴 价格在VPOC({prof['vpoc']})下方，重心偏下")
        sc -= 10

    # --- 利好信号 ---
    if ph['phase'] == 'E' and ph['dir'] == 'uptrend':
        green.append(f"✅ Phase E 上涨趋势（强度{ph['conf']}%）")
        sc += 20
    if ph['phase'] == 'E' and ph['dir'] == 'uptrend_pullback':
        green.append(f"✅ Phase E 上涨趋势回踩（强度{ph['conf']}%）")
        sc += 15
    if ph['phase'] == 'B' and ph['dir'] == 'accumulation':
        green.append(f"✅ Phase B 积累区间（机构收集）")
        sc += 25
    if ph['phase'] == 'C' and ph['dir'] == 'spring_test':
        green.append(f"⚠️ Phase C Spring测试（未确认，需等待阳线收回）")
        sc += 10
    if ph['phase'] == 'D' and ph['dir'] == 'breakout_up':
        green.append(f"✅ Phase D 向上突破（强度{ph['conf']}%）")
        sc += 30
    if prof['cur'] > prof['vpoc']:
        green.append(f"✅ 价格在VPOC({prof['vpoc']})上方，重心偏多")
        sc += 20
    if prof['cur'] > prof['vah']:
        green.append(f"✅ 价格在VAH({prof['vah']})上方，强势")
        sc += 10
    if avg5 > avg60 * 1.5:
        green.append(f"✅ 成交量放大（近5日均量是60日均量的{avg5/avg60:.0%}，机构入场）")
        sc += 15
    elif avg5 > avg60 * 1.2:
        green.append(f"🟡 成交量温和放大（{avg5/avg60:.0%}）")
        sc += 5
    if prof['lvn']:
        nearest_gap = min([abs(prof['cur'] - l['price']) / prof['cur'] for l in prof['lvn']])
        if nearest_gap < 0.05:
            nearest_lvn = min(prof['lvn'], key=lambda x: abs(prof['cur'] - x['price']))
            green.append(f"✅ 下方LVN@{nearest_lvn['price']}形成支撑")
            sc += 10

    sc = max(0, min(100, sc))

    if red and any('🔴' in r for r in red):
        rating = 'D' if sc < 50 else ('C' if sc < 70 else 'B')
        verdict = '风险过高，建议回避' if rating == 'D' else ('风险信号明确，不建议买入' if rating == 'C' else '有风险但有机会，谨慎参与')
    elif sc >= 75:
        rating, verdict = 'S', '强势候选，重点关注'
    elif sc >= 60:
        rating, verdict = 'A', '满足买入条件，可以关注'
    elif sc >= 40:
        rating, verdict = 'B', '信号不明确，继续观察'
    else:
        rating, verdict = 'C', '信号偏弱，不宜买入'

    return {
        'pass': rating in ['S', 'A'],
        'score': sc, 'rating': rating,
        'red_flags': red, 'green_flags': green,
        'signals': green + red, 'verdict': verdict,
        'phase': ph, 'profile': prof, 'side': 'right',
    }


# ============================================================
#  左侧评分（积累末期，低位埋伏）
# ============================================================
def score_stock_left(df: pd.DataFrame) -> dict:
    """左侧积累筛选——主力悄悄收集，价格低位，尚未启动"""
    prof = calculate_vp(df)
    ph = detect_phase(df)
    if not prof or ph['phase'] == 'unknown':
        return {
            'pass': False, 'score': 0, 'red_flags': [], 'green_flags': [],
            'signals': [], 'rating': 'N', 'verdict': '数据不足，无法判断',
            'phase': ph, 'profile': prof, 'side': 'left',
        }

    green, red, sc = [], [], 0
    cols = detect_columns(df)
    vol_col = cols.get('volume', None)
    avg5 = df[vol_col].tail(5).mean() if vol_col else 0
    avg20 = df[vol_col].tail(20).mean() if vol_col else 0
    avg60 = df[vol_col].tail(60).mean() if vol_col else 0

    cur = prof['cur']
    vpoc = prof['vpoc']
    val = prof['val']
    range_w = prof['max_p'] - prof['min_p']
    pct_range = (cur - prof['min_p']) / range_w if range_w > 0 else 0.5

    # ========== 左侧核心利好 ==========

    # ① Phase B 积累（最重要！）
    if ph['phase'] == 'B' and ph['dir'] == 'accumulation':
        sc += 30
        green.append(f"✅ Phase B 积累区间（机构暗中收集）")
        if pct_range < 0.4:
            sc += 10
            green.append(f"✅ Phase B 初期，价格在区间低位（{pct_range:.0%}），安全边际高")
        elif pct_range > 0.6:
            sc += 5
            green.append(f"🟡 Phase B 末期，接近突破窗口")

    # ② Phase C Spring 测试
    if ph['phase'] == 'C' and ph['dir'] == 'spring_test':
        sc += 20
        green.append(f"⚠️ Phase C Spring测试（支撑位测试）")
        if cur > vpoc:
            sc += 15
            green.append(f"✅ Spring后价格收回VPOC({vpoc})，支撑确认")
        elif cur > val:
            sc += 8
            green.append(f"🟡 价格在VAL({val})上方，勉强守住")
        else:
            sc -= 5
            red.append(f"🔴 Spring后价格未能收回VAL({val})，支撑可能失效")

    # ③ 价格紧贴 LVN 支撑
    if prof['lvn']:
        nearest_lvn = min(prof['lvn'], key=lambda x: abs(cur - x['price']))
        gap_pct = abs(cur - nearest_lvn['price']) / cur
        if gap_pct < 0.03:
            sc += 15
            green.append(f"✅ 价格紧贴LVN@{nearest_lvn['price']}，强支撑位")
        elif gap_pct < 0.08:
            sc += 8
            green.append(f"🟡 价格接近LVN@{nearest_lvn['price']}，支撑区间")

    # ④ 价格在 VAL/VPOC 下方（低位吸筹区）
    if cur < val:
        sc += 10
        green.append(f"✅ 价格在VAL({val})下方，偏离价值区，低位吸筹区")
    if cur < vpoc:
        sc += 5
        green.append(f"🟡 价格在VPOC({vpoc})下方，尚未确认启动")

    # ⑤ 缩量止跌
    if avg5 < avg20 * 0.8 and avg20 < avg60 * 0.9:
        sc += 15
        green.append(f"✅ 缩量止跌（5日均量{avg5/avg60:.0%}×60日均量），主力在吸筹")
    elif avg5 < avg20:
        sc += 5
        green.append(f"🟡 成交量萎缩，卖压减弱")

    # ⑥ 区间极低位
    if pct_range < 0.25:
        sc += 12
        green.append(f"✅ 价格在60日区间的{pct_range:.0%}低位，安全边际高")

    # ========== 左侧红线 ==========
    if ph['phase'] in ['B', 'C'] and ph['dir'] in ['distribution', 'upthrust_test']:
        red.append(f"🔴 Phase {ph['phase']}({ph['dir']}) = 派发，不宜左侧买入")
        sc -= 25
    if ph['phase'] == 'E' and ph['dir'] == 'downtrend':
        red.append(f"🔴 Phase E 下跌趋势，不做左侧抄底")
        sc -= 20
    if ph['phase'] == 'B' and ph['dir'] == 'distribution':
        red.append(f"🔴 Phase B 派发区间，主力在出货")
    if avg5 > avg60 * 1.8 and cur < val:
        red.append(f"🔴 放量下跌（5日均量是60日均量的{avg5/avg60:.0%}×），主力在派发")
        sc -= 20
    if pct_range < 0.15 and avg5 > avg60:
        red.append(f"🔴 价格极低位但未缩量，下跌可能未结束")
        sc -= 10

    # ========== 综合评级 ==========
    sc = max(0, min(100, sc))
    if sc >= 75:
        rating, verdict = 'S', '左侧积累末期，重点关注，止损明确'
    elif sc >= 55:
        rating, verdict = 'A', '左侧候选，耐心等待启动信号'
    elif sc >= 35:
        rating, verdict = 'B', '左侧观察，暂不介入'
    else:
        rating, verdict = 'C', '左侧信号弱，不建议'

    return {
        'pass': rating in ['S', 'A'],
        'score': sc, 'rating': rating,
        'red_flags': red, 'green_flags': green,
        'signals': green + red, 'verdict': verdict,
        'phase': ph, 'profile': prof, 'side': 'left',
    }


# ============================================================
#  主入口：mode 参数分发
# ============================================================
def score_stock(df: pd.DataFrame, mode: str = 'right') -> dict:
    """
    综合评分（0~100）
    mode='right' → 右侧趋势跟随（Phase E/D 突破确认后买）
    mode='left'  → 左侧积累筛选（Phase B/C 低位埋伏）
    """
    if mode == 'left':
        return score_stock_left(df)
    return score_stock_right(df)
