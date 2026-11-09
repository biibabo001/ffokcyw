"""
Wyckoff 2.0 选股扫描（腾讯数据源版）
每天收盘后运行，扫描全A股市场，找出积累末期/趋势启动候选股

用法:
  python screen.py                # 右侧趋势筛选（默认）
  python screen.py --mode left    # 左侧积累筛选
  python screen.py --mode both    # 两种都跑
  python screen.py --limit 50     # 只扫前50只（冒烟测试用）
  python screen.py --top 20       # 输出前N只
"""

import contextlib
import io
import sys
import time
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from wyckoff_engine import score_stock
from screen_data import fetch_stock_data

import akshare as ak
import pandas as pd

# ========== 配置 ==========
TOP_N = 20
REQUEST_DELAY = 0.3
BATCH_SLEEP = 2
BATCH_SIZE = 100

# 右侧阈值（趋势确认后买）
RIGHT_SCORE_THRESHOLD = 60
# 左侧阈值（低位埋伏）
LEFT_SCORE_THRESHOLD = 55

# B股前缀，排除（A股市场不含B股）
B_SHARE_PREFIXES = ('200', '900')


def get_all_codes() -> list:
    """获取全部A股代码+名称（code, name）"""
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        df = ak.stock_info_a_code_name()
    records = []
    for _, row in df.iterrows():
        code = str(row['code']).strip()
        if code.startswith(B_SHARE_PREFIXES):
            continue
        records.append((code, str(row['name']).strip()))
    print(f'  A股总数: {len(df)} -> 纳入扫描: {len(records)}只')
    return records


def screen(codes: list, modes: list, top_n: int = TOP_N, limit: int = None) -> dict:
    """
    执行选股扫描（单遍取数，同时跑多个模式）
    modes 可为 ['right'] / ['left'] / ['right', 'left']
    返回: {mode: DataFrame}
    """
    if limit:
        codes = codes[:limit]

    results = {m: [] for m in modes}
    failed = 0
    print(f'  开始扫描 {len(codes)} 只股票（模式: {"/".join(modes)}）...')

    for i, (code, name) in enumerate(codes):
        if i % 50 == 0:
            print(f'  进度: {i}/{len(codes)}...')
        try:
            df = fetch_stock_data(code)
        except Exception:
            df = None
        time.sleep(REQUEST_DELAY)

        if df is None or len(df) < 60:
            failed += 1
            continue

        if (i + 1) % BATCH_SIZE == 0:
            time.sleep(BATCH_SLEEP)

        for mode in modes:
            r = score_stock(df, mode=mode)
            if not r['pass']:
                continue
            results[mode].append({
                'code': code,
                'name': name,
                'side': mode,
                'phase': r['phase']['phase'],
                'phase_dir': r['phase']['dir'],
                'score': r['score'],
                'rating': r['rating'],
                'signals': ' | '.join(r['signals']),
                'vpoc': r['profile']['vpoc'],
                'cur': r['profile']['cur'],
                'position': r['profile']['position'],
                'verdict': r['verdict'],
            })
            mode_label = '左侧' if mode == 'left' else '右侧'
            print(f'  ✅ 入选[{mode_label}] #{len(results[mode])} {name}({code}) '
                  f'评分{r["score"]}/{r["rating"]} Phase {r["phase"]["phase"]}/{r["phase"]["dir"]} '
                  f'现价{r["profile"]["cur"]} | {r["verdict"]}')

    out = {}
    print(f'  取数完成: {len(codes) - failed}只成功, {failed}只失败')
    for mode in modes:
        threshold = LEFT_SCORE_THRESHOLD if mode == 'left' else RIGHT_SCORE_THRESHOLD
        mode_label = '左侧积累' if mode == 'left' else '右侧趋势'
        df_mode = pd.DataFrame(results[mode])
        if len(df_mode) == 0:
            print(f'  [{mode_label}] 没有找到符合条件的股票（阈值≥{threshold}）')
            out[mode] = pd.DataFrame()
            continue
        df_mode = df_mode.sort_values('score', ascending=False).head(top_n)
        df_mode = df_mode.reset_index(drop=True)
        df_mode.index = df_mode.index + 1
        df_mode.index.name = 'rank'
        print(f'  [{mode_label}] 扫描完成，找到 {len(df_mode)} 只候选股（阈值≥{threshold}）')
        out[mode] = df_mode

    return out


def format_result(df: pd.DataFrame, mode: str = 'right') -> str:
    """格式化输出"""
    if len(df) == 0:
        return '今日无符合条件的股票'

    mode_label = '左侧积累' if mode == 'left' else '右侧趋势'
    threshold = LEFT_SCORE_THRESHOLD if mode == 'left' else RIGHT_SCORE_THRESHOLD
    today = pd.Timestamp.today().strftime('%Y-%m-%d')

    W = 64
    sep = '━' * W

    lines = []
    lines.append(f'{"┌"}{"─"*W}{"┐"}')
    lines.append(f'│{" Wyckoff 2.0 选股结果 ".center(W)}│')
    lines.append(f'│{" "+today+" | "+mode_label+" | 阈值"+str(threshold)+" ".center(W)}│')
    lines.append(f'{"└"}{"─"*W}{"┘"}')

    for rank, row in df.iterrows():
        rating_icon = {'S': '🅖', 'A': '✅', 'B': '🅑', 'C': '⚠️', 'D': '🅕'}.get(row.get('rating', ''), '?')

        lines.append(f'  {sep}')
        lines.append(f'  #{rank}  {row["name"]} ({row["code"]})  {rating_icon} {row.get("rating","?")}')
        lines.append(f'  {sep}')
        lines.append(f'   Phase : {row["phase"]} / {row["phase_dir"]}')
        lines.append(f'   评分  : {row["score"]}/100')
        lines.append(f'   现价  : {row["cur"]}  |  VPOC: {row["vpoc"]}  |  位置: {row["position"]}')
        verdict = row.get('verdict', '')
        if verdict:
            lines.append(f'   结论  : {verdict}')
        signals = row['signals']
        if len(signals) > 80:
            parts = signals.split(' | ')
            line = '   信号  : '
            for p in parts:
                if len(line + p) > W + 8:
                    lines.append(line.rstrip(' ,'))
                    line = '          ' + p + ' | '
                else:
                    line += p + ' | '
            lines.append(line.rstrip(' |'))
        else:
            lines.append(f'   信号  : {signals}')

    lines.append(f'  {"━"*W}')
    lines.append(f'   共 {len(df)} 只候选股  |  规则: 评分≥{threshold} + Wyckoff {mode_label}结构')
    lines.append(f'  {"━"*W}')
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description='Wyckoff 2.0 选股系统（腾讯数据源）')
    parser.add_argument('--mode', default='right',
                        choices=['right', 'left', 'both'],
                        help='right=右侧趋势筛选, left=左侧积累筛选, both=两种都跑')
    parser.add_argument('--top', type=int, default=TOP_N, help=f'输出前N只（默认{TOP_N}）')
    parser.add_argument('--limit', type=int, default=None, help='只扫描前N只（冒烟测试用）')
    args = parser.parse_args()

    print('=' * 60)
    print('  Wyckoff 2.0 选股系统（腾讯数据源）')
    print(f'  模式: {args.mode}' + (f' | 限量: {args.limit}只' if args.limit else ''))
    print('=' * 60)

    codes = get_all_codes()

    modes = ['right', 'left'] if args.mode == 'both' else [args.mode]

    all_results = screen(codes, modes=modes, top_n=args.top, limit=args.limit)

    print()
    for mode, result in all_results.items():
        print(format_result(result, mode=mode))
        print()

    # 保存
    today = pd.Timestamp.today().strftime('%Y-%m-%d')
    out_path = Path(__file__).parent.parent / 'data' / f'screen_{today}.txt'
    with open(out_path, 'w', encoding='utf-8') as f:
        for mode, result in all_results.items():
            f.write(format_result(result, mode=mode) + '\n\n')
    print(f'结果已保存: {out_path}')


if __name__ == '__main__':
    main()
