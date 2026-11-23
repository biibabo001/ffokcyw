#!/usr/bin/env python3
"""Wyckoff A股工具统一入口（选股 + 诊股）

用法:
  python main.py screen [参数]          # 全市场威科夫选股（转发到 wyckoff_screen.py）
  python main.py base [参数]            # 大涨、深跌后横盘的股票
  python main.py diagnose <代码或名称>   # 单只股票诊股

示例:
  python main.py screen --threads 5 --charts 10
  python main.py screen --help          # 查看选股参数
  python main.py diagnose 601985
  python main.py diagnose 中国核电
"""

import os
import re
import sys
import warnings
from pathlib import Path

# 禁用 akshare 内部 tqdm 进度条（stderr 噪音，会干扰实时进度显示）
os.environ.setdefault('TQDM_DISABLE', '1')
# 屏蔽 requests/urllib3 版本告警噪音
warnings.filterwarnings('ignore', message='urllib3')

# 国内行情源（腾讯/交易所）走直连：系统代理(Clash)会把国内行情流量导向坏节点，导致连接被重置
_no_proxy = os.environ.get('NO_PROXY', '')
if 'eastmoney.com' not in _no_proxy:
    os.environ['NO_PROXY'] = ','.join(filter(None, [_no_proxy, 'eastmoney.com', 'gtimg.cn', 'qq.com']))

# GBK 控制台无法编码 emoji，输出时替换为 ? 而非抛 UnicodeEncodeError
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors='replace')
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).parent

USAGE = """用法:
  python main.py screen [参数]          # 全市场威科夫选股（转发到 wyckoff_screen.py）
  python main.py base [参数]            # 大涨、深跌后横盘的股票
  python main.py diagnose <代码或名称>   # 单只股票诊股
"""


def run_screen(argv):
    """转发到根目录的 wyckoff_screen.py（最新选股逻辑）"""
    import wyckoff_screen
    sys.argv = ['wyckoff_screen.py'] + argv
    wyckoff_screen.main()


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
        code, name_esc, kind = parts[1], parts[2], parts[4]
        if not re.fullmatch(r'\d{6}', code) or kind != 'GP-A':
            continue
        try:
            name = json.loads(f'"{name_esc}"')
        except Exception:
            name = name_esc
        stocks.append((code, name))

    if not stocks:
        raise SystemExit(f'未找到与「{q}」匹配的A股，请改用6位股票代码')

    code, name = stocks[0]
    if len(stocks) > 1:
        others = '、'.join(f'{n}({c})' for c, n in stocks[1:])
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
    if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help', 'help'):
        print(__doc__)
        return

    cmd, rest = sys.argv[1], sys.argv[2:]

    if cmd == 'screen':
        run_screen(rest)
    elif cmd == 'base':
        from bottom_base import main as scan_bottom_base
        scan_bottom_base(rest)
    elif cmd == 'diagnose':
        if not rest:
            raise SystemExit('用法: python main.py diagnose <股票代码或名称>')
        run_diagnose(rest[0])
    else:
        raise SystemExit(f'未知命令: {cmd}\n{USAGE}')


if __name__ == '__main__':
    main()
