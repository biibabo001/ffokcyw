#!/usr/bin/env python3
"""Wyckoff 2.0 A股工具统一入口（选股 + 诊股）

用法:
  python main.py screen [--threads N] [--limit N] [--top N]
  python main.py diagnose <股票代码或名称>

示例:
  python main.py screen --threads 4
  python main.py diagnose 601985
  python main.py diagnose 中国核电
"""

import argparse
import os
import re
import sys
import warnings
from pathlib import Path

# 禁用 akshare 内部 tqdm 进度条（stderr 噪音，会干扰实时进度显示）
os.environ.setdefault('TQDM_DISABLE', '1')
# 屏蔽 requests/urllib3 版本告警噪音
warnings.filterwarnings('ignore', message='urllib3')

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / 'skills' / 'wyckoff-screen' / 'scripts'))
sys.path.insert(0, str(ROOT / 'skills' / 'wyckoff-stock' / 'scripts'))


def run_screen(threads: int = 1, limit: int = None, top: int = None):
    """执行全市场选股（主板·非ST·市值<500亿 + 近一周Spring + 威科夫积累结构）"""
    import pandas as pd
    from screen import get_all_codes, screen, format_result, TOP_N

    top_n = top if top else TOP_N

    print('=' * 60)
    print('  Wyckoff 2.0 选股系统（威科夫积累结构筛选）')
    info = '  条件: 主板·非ST·市值<500亿 + 近一周Spring + PS/SC/AR/ST结构'
    if limit:
        info += f' | 限量: {limit}只'
    if threads > 1:
        info += f' | 并发: {threads}线程'
    print(info)
    print('=' * 60)

    print('  正在获取股票列表与市值数据（约1分钟）...')
    codes = get_all_codes()

    result = screen(codes, top_n=top_n, limit=limit, threads=threads)

    print()
    print(format_result(result))

    today = pd.Timestamp.today().strftime('%Y-%m-%d')
    out_path = ROOT / 'skills' / 'wyckoff-screen' / 'data' / f'screen_{today}.txt'
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(format_result(result) + '\n')
    print(f'结果已保存: {out_path}')
    return result


def resolve_stock(query: str) -> str:
    """6位数字视为股票代码；否则按名称搜索解析为代码（腾讯智能选股搜索）"""
    import json
    import requests

    q = str(query).strip()
    if re.fullmatch(r'\d{6}', q):
        return q

    try:
        resp = requests.get('https://smartbox.gtimg.cn/s3/',
                            params={'v': '2', 'q': q, 't': 'all'}, timeout=10)
        resp.raise_for_status()
        text = resp.text
    except Exception as e:
        raise SystemExit(f'名称搜索失败: {e}')

    m = re.search(r'v_hint="([^"]*)"', text)
    if not m or not m.group(1):
        raise SystemExit(f'未找到与「{q}」匹配的A股，请改用6位股票代码')

    stocks = []
    for entry in m.group(1).split('^'):
        # 格式: 市场~代码~名称~拼音~类型，如 sh~601985~中国核电~zghd~GP-A
        parts = entry.split('~')
        if len(parts) < 5:
            continue
        market, code, name_esc, kind = parts[0], parts[1], parts[2], parts[4]
        if not re.fullmatch(r'\d{6}', code) or kind != 'GP-A':
            continue
        try:
            name = json.loads(f'"{name_esc}"')
        except Exception:
            name = name_esc
        stocks.append((code, name, market))

    if not stocks:
        raise SystemExit(f'未找到与「{q}」匹配的A股，请改用6位股票代码')

    code, name, market = stocks[0]
    if len(stocks) > 1:
        others = '、'.join(f'{n}({c})' for c, n, _ in stocks[1:])
        print(f'  多个匹配: {name}({code})、{others}，默认取第一个', file=sys.stderr)
    print(f'  已解析: {name}({code})', file=sys.stderr)
    return code


def run_diagnose(query: str):
    """执行诊股；query 可为6位股票代码或股票名称"""
    from diagnose import generate_diagnosis, print_report

    code = resolve_stock(query)
    result = generate_diagnosis(code)
    print_report(result)
    return result


def main():
    parser = argparse.ArgumentParser(
        prog='main.py',
        description='Wyckoff 2.0 A股工具统一入口（选股 + 诊股）',
    )
    sub = parser.add_subparsers(dest='cmd', required=True)

    p_screen = sub.add_parser(
        'screen',
        help='全市场选股（威科夫积累结构：PS/SC/AR/ST/Creek/Spring/LPS/SOS）')
    p_screen.add_argument('--threads', type=int, default=1,
                          help='并发取数线程数（默认1，建议3~6）')
    p_screen.add_argument('--limit', type=int, default=None, help='只扫描前N只（冒烟测试用）')
    p_screen.add_argument('--top', type=int, default=None, help='输出前N只（默认20）')

    p_diag = sub.add_parser('diagnose', help='单只股票诊股（Wyckoff 2.0）')
    p_diag.add_argument('query', help='股票代码或名称，如 601985、中国核电')

    args = parser.parse_args()
    if args.cmd == 'screen':
        run_screen(threads=args.threads, limit=args.limit, top=args.top)
    else:
        run_diagnose(args.query)


if __name__ == '__main__':
    main()
