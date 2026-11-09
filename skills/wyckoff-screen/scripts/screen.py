"""
Wyckoff 2.0 选股扫描（腾讯数据源版）
每天收盘后运行，扫描全A股市场，按威科夫积累结构筛选候选股：
PS首次支撑 / SC恐慌抛售 / AR自动反弹 / ST二次测试 / Creek阻力 / Spring洗盘 / LPS最后支撑(缩量) / SOS突破

用法:
  python screen.py                 # 默认全量扫描
  python screen.py --limit 100     # 只扫前100只（冒烟测试用）
  python screen.py --top 20        # 输出前N只
  python screen.py --threads 4     # 并发取数（建议3~6）
"""

import contextlib
import io
import os
import sys
import time
import argparse
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# 禁用 akshare 内部 tqdm 进度条（stderr 噪音，会与实时进度显示互相覆盖）
os.environ.setdefault('TQDM_DISABLE', '1')

sys.path.insert(0, str(Path(__file__).parent))
from wyckoff_engine import (detect_recent_spring, detect_wyckoff_events,
                            score_wyckoff_structure)
from screen_data import fetch_stock_data

import akshare as ak
import pandas as pd

# ========== 配置 ==========
TOP_N = 20
REQUEST_DELAY = 0.1
BATCH_SLEEP = 1
BATCH_SIZE = 200

# 威科夫结构评分阈值（>=75：核心事件齐全 + Creek + 近期Spring）
STRUCTURE_SCORE_THRESHOLD = 75

# 仅扫描主板：沪市主板(60xxxx) + 深市主板(00xxxx)
MAIN_BOARD_PREFIXES = ('60', '00')
# 总市值上限：500亿（元）
MARKET_CAP_LIMIT = 500 * 10**8


def get_all_codes() -> list:
    """获取主板、非ST、总市值<500亿的A股代码+名称（code, name）"""
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        df = ak.stock_zh_a_spot_em()
    records = []
    for _, row in df.iterrows():
        code = str(row['代码']).strip()
        if not code.startswith(MAIN_BOARD_PREFIXES):
            continue
        name = str(row['名称']).strip()
        if 'ST' in name.upper() or '退' in name:
            continue
        cap = row.get('总市值', None)
        if cap is None or pd.isna(cap) or cap >= MARKET_CAP_LIMIT:
            continue
        records.append((code, name))
    print(f'  A股总数: {len(df)} -> 纳入扫描: {len(records)}只（主板·非ST·市值<500亿）')
    return records


def _fetch_one(code: str, name: str, suppress: bool = True):
    """取数封装：异常返回 None"""
    try:
        return code, name, fetch_stock_data(code, suppress=suppress)
    except Exception:
        return code, name, None


def _progress(done: int, total: int, hits: int):
    """实时进度条：终端下原地刷新；输出重定向（非终端）时每50只打一行"""
    pct = done / total * 100 if total else 0
    bar_len = 30
    filled = int(bar_len * done / total) if total else 0
    bar = '█' * filled + '░' * (bar_len - filled)
    line = f'  已扫描 {done}/{total} [{bar}] {pct:5.1f}%  命中 {hits} 只'
    if sys.stdout.isatty():
        print(f'\r{line}', end='', flush=True)
    elif done % 50 == 0 or done == total:
        print(line, flush=True)


def screen(codes: list, top_n: int = TOP_N, limit: int = None,
           threads: int = 1) -> pd.DataFrame:
    """
    执行威科夫结构选股扫描
    过滤链: 数据充足 -> 近一周Spring -> PS/SC/AR/ST核心事件 -> 结构评分>=75
    返回: 按结构评分降序的 DataFrame
    """
    if limit:
        codes = codes[:limit]

    rows = []
    total = len(codes)
    print(f'  开始扫描 {total} 只股票（并发: {threads}线程）...')

    # ---------- 取数阶段（并发或串行） ----------
    fetched = []
    if threads > 1:
        with ThreadPoolExecutor(max_workers=threads) as ex:
            futures = [ex.submit(_fetch_one, c, n, False) for c, n in codes]
            for fut in as_completed(futures):
                fetched.append(fut.result())
                _progress(len(fetched), total, 0)
    else:
        for i, (code, name) in enumerate(codes):
            try:
                df = fetch_stock_data(code)
            except Exception:
                df = None
            time.sleep(REQUEST_DELAY)
            if (i + 1) % BATCH_SIZE == 0:
                time.sleep(BATCH_SLEEP)
            fetched.append((code, name, df))
            _progress(i + 1, total, 0)

    print()  # 换行结束进度条

    # ---------- 结构识别与评分阶段 ----------
    failed = 0
    hits = 0
    for code, name, df in fetched:
        if df is None or len(df) < 60:
            failed += 1
            continue

        # 最近一周（5个交易日）内刚经历 Spring 洗盘
        if not detect_recent_spring(df):
            continue

        # 威科夫积累结构：PS/SC/AR/ST 核心事件必须齐全
        events = detect_wyckoff_events(df)
        if not events or not all(events[k] for k in ('PS', 'SC', 'AR', 'ST')):
            continue

        r = score_wyckoff_structure(events)
        if not r['pass']:
            continue

        hits += 1
        ev_chain = '/'.join(k for k in ('PS', 'SC', 'AR', 'ST', 'Creek', 'Spring', 'LPS', 'SOS')
                            if events[k])
        rows.append({
            'code': code,
            'name': name,
            'score': r['score'],
            'rating': r['rating'],
            'events': ev_chain,
            'signals': ' | '.join(r['signals']),
            'support': events['support'],
            'creek': events['creek'],
            'cur': events['cur'],
            'verdict': r['verdict'],
        })
        print(f'\n  ✅ 入选 #{hits} {name}({code}) '
              f'结构评分{r["score"]}/{r["rating"]} 现价{events["cur"]} '
              f'支撑{events["support"]}/阻力{events["creek"]} | {ev_chain}', flush=True)

    print()
    print(f'  取数完成: {total - failed}只成功, {failed}只失败')

    df_out = pd.DataFrame(rows)
    if len(df_out) == 0:
        print(f'  没有找到符合条件的股票（结构评分≥{STRUCTURE_SCORE_THRESHOLD}）')
        return pd.DataFrame()
    df_out = df_out.sort_values('score', ascending=False).head(top_n)
    df_out = df_out.reset_index(drop=True)
    df_out.index = df_out.index + 1
    df_out.index.name = 'rank'
    print(f'  扫描完成，找到 {len(df_out)} 只候选股（结构评分≥{STRUCTURE_SCORE_THRESHOLD}）')
    return df_out


def format_result(df: pd.DataFrame) -> str:
    """格式化输出"""
    if len(df) == 0:
        return '今日无符合条件的股票'

    today = pd.Timestamp.today().strftime('%Y-%m-%d')
    W = 64
    sep = '━' * W

    lines = []
    lines.append(f'{"┌"}{"─"*W}{"┐"}')
    lines.append(f'│{" Wyckoff 2.0 选股结果 ".center(W)}│')
    lines.append(f'│{" "+today+" | 威科夫积累结构 | 评分≥"+str(STRUCTURE_SCORE_THRESHOLD)+" ".center(W)}│')
    lines.append(f'{"└"}{"─"*W}{"┘"}')

    for rank, row in df.iterrows():
        rating_icon = {'S': '🅖', 'A': '✅', 'B': '🅑', 'C': '⚠️', 'D': '🅕'}.get(row.get('rating', ''), '?')

        lines.append(f'  {sep}')
        lines.append(f'  #{rank}  {row["name"]} ({row["code"]})  {rating_icon} {row.get("rating","?")}')
        lines.append(f'  {sep}')
        lines.append(f'   结构评分: {row["score"]}/100')
        lines.append(f'   事件链  : {row["events"]}')
        lines.append(f'   现价    : {row["cur"]}  |  支撑: {row["support"]}  |  阻力(Creek): {row["creek"]}')
        verdict = row.get('verdict', '')
        if verdict:
            lines.append(f'   结论    : {verdict}')
        signals = row['signals']
        if len(signals) > 80:
            parts = signals.split(' | ')
            line = '   信号    : '
            for p in parts:
                if len(line + p) > W + 8:
                    lines.append(line.rstrip(' ,'))
                    line = '            ' + p + ' | '
                else:
                    line += p + ' | '
            lines.append(line.rstrip(' |'))
        else:
            lines.append(f'   信号    : {signals}')

    lines.append(f'  {"━"*W}')
    lines.append(f'   共 {len(df)} 只候选股  |  规则: 主板·非ST·市值<500亿 + 近一周Spring + 威科夫结构评分≥{STRUCTURE_SCORE_THRESHOLD}')
    lines.append(f'  {"━"*W}')
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description='Wyckoff 2.0 选股系统（威科夫积累结构）')
    parser.add_argument('--top', type=int, default=TOP_N, help=f'输出前N只（默认{TOP_N}）')
    parser.add_argument('--limit', type=int, default=None, help='只扫描前N只（冒烟测试用）')
    parser.add_argument('--threads', type=int, default=1, help='并发取数线程数（默认1，建议3~6）')
    args = parser.parse_args()

    print('=' * 60)
    print('  Wyckoff 2.0 选股系统（威科夫积累结构筛选）')
    print('  条件: 主板·非ST·市值<500亿 + 近一周Spring + PS/SC/AR/ST结构'
          + (f' | 限量: {args.limit}只' if args.limit else '')
          + (f' | 并发: {args.threads}线程' if args.threads > 1 else ''))
    print('=' * 60)

    print('  正在获取股票列表与市值数据（约1分钟）...')
    codes = get_all_codes()

    result = screen(codes, top_n=args.top, limit=args.limit, threads=args.threads)

    print()
    print(format_result(result))

    # 保存
    today = pd.Timestamp.today().strftime('%Y-%m-%d')
    out_path = Path(__file__).parent.parent / 'data' / f'screen_{today}.txt'
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(format_result(result) + '\n')
    print(f'结果已保存: {out_path}')


if __name__ == '__main__':
    main()
