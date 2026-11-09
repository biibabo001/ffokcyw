"""
选股系统统一数据获取
主数据源: 腾讯(akshare stock_zh_a_hist_tx)，备选: efinance(东财)
"""

import contextlib
import io
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))


def _to_tencent_symbol(code: str) -> str:
    """将6位A股代码转换为腾讯证券代码(sh/sz/bj前缀)"""
    code = str(code).strip()
    if code.startswith(("6", "9", "5")):
        return f"sh{code}"
    if code.startswith(("4", "8")):
        return f"bj{code}"
    return f"sz{code}"


def fetch_stock_data(code: str, suppress: bool = True) -> pd.DataFrame | None:
    """
    获取单只股票日线数据（腾讯数据源，前复权）
    suppress=False 时不重定向 stdout/stderr（并发取数场景，避免全局重定向线程不安全）
    返回: DataFrame（列名 date/open/close/high/low/volume/amount，wyckoff_engine 可自动识别）
    """
    code = str(code).strip()

    # 主数据源: 腾讯（取最近约600个自然日，覆盖120日回看 + MA60 足够）
    try:
        import akshare as ak
        symbol = _to_tencent_symbol(code)
        start = (datetime.now() - timedelta(days=600)).strftime("%Y%m%d")
        end = datetime.now().strftime("%Y%m%d")
        if suppress:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                df = ak.stock_zh_a_hist_tx(symbol=symbol, start_date=start, end_date=end, adjust="qfq")
        else:
            df = ak.stock_zh_a_hist_tx(symbol=symbol, start_date=start, end_date=end, adjust="qfq")
        if df is not None and len(df) >= 60:
            return df
    except Exception:
        pass

    # 备选: efinance（东财）
    try:
        import efinance as ef
        df = ef.stock.get_quote_history(code, beg='20230101', end='20500101', klt=101, fqt=1)
        if df is not None and len(df) >= 60:
            return df
    except Exception:
        pass

    return None
