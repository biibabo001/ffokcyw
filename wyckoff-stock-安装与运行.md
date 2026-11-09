# Wyckoff Stock（A股威科夫诊股/选股工具）安装与运行指南

> 来源：dxarch1980 的 Wyckoff 系列 Skill（ClawHub / SkillHub 分发）
> 文档地址：https://docs.clawhub.ai/dxarch1980/skills/wyckoff-stock
> 整理日期：2026-11-09
> 仅作技术参考，不构成投资建议。

---

## 一、技能系列总览

| 技能 | 用途 | 安装命令 | 版本 |
|------|------|----------|------|
| **wyckoff-stock** | A股诊股（单只/批量诊断） | `openclaw skills install @dxarch1980/wyckoff-stock` | 1.0.0 |
| wyckoff-screen | A股全市场选股 | `npx secure-skills-cli add dxarch1980/wyckoff-screen` | 1.2.0 |
| wyckoff-diagnose | A股诊股 | `npx secure-skills-cli add dxarch1980/wyckoff-diagnose` | 1.2.0 |

> 注意：`wyckoff-stock` 走 **OpenClaw** 生态（`openclaw skills install`）；
> `wyckoff-screen` / `wyckoff-diagnose` 走 `npx secure-skills-cli`。两者安装方式不同，勿混用。

---

## 二、环境依赖（Python3）

```bash
pip install akshare --upgrade -i https://mirrors.aliyun.com/pypi/simple/
pip install efinance
```

| 依赖 | 用途 |
|------|------|
| **akshare** | 获取全部 A 股代码（约 5500 只） |
| **efinance** | 拉取日线行情 |
| **Python3** | 运行环境 |

---

## 三、核心脚本与引擎

| 技能 | 主脚本 | 引擎 |
|------|--------|------|
| wyckoff-stock | `scripts/diagnose.py` | — |
| wyckoff-screen | `scripts/screen.py` | `scripts/wyckoff_engine.py` |
| wyckoff-diagnose | `scripts/diagnose.py` | `scripts/wyckoff_engine.py` |

---

## 四、运行方式

### 直接运行

```bash
python scripts/diagnose.py    # wyckoff-stock 诊股脚本
python scripts/screen.py      # wyckoff-screen 选股脚本
```

### 选股脚本的 `--mode` 参数（screen.py）

| 参数 | 含义 | 输出阈值 |
|------|------|----------|
| `--mode right`（默认） | 右侧趋势筛选 | 评分 ≥60 输出 |
| `--mode left` | 左侧吸筹筛选 | 评分 ≥55 输出 |
| `--mode both` | 两种模式都跑 | — |

### 作为模块调用（screen 示例）

```python
import sys
sys.path.insert(0, 'scripts')
from screen import screen, init_db, update_daily_data, format_result

conn = init_db()
update_daily_data(conn)
result = screen(conn)
print(format_result(result))
```

---

## 五、工作流程（README 核心）

1. **拉取全市场股票列表**：akshare `stock_info_a_code_name()` 获取所有 A 股代码。
2. **更新日线数据**：efinance `get_quote_history(code, klt=101, fqt=1)` 存入 SQLite（`stock_daily` 表）。
   - `klt=101` = 日线；`fqt=1` = 前复权。
3. **逐只评分**：`wyckoff_engine.score_stock()` 计算 Phase、VPOC、综合评分。
   - 筛选条件：评分达标 且 Phase/VPOC 方向为「积累/突破」。
4. **输出结果**：按评分降序输出 TOP20 候选股（代码、名称、Phase、VPOC、现价、关键信号、评分）。

---

## 六、评级档位

| 评级 | 分数 | 含义 |
|------|------|------|
| **S** | ≥75 | 强烈推荐 |
| **A** | 60–74 | 满足买入条件 |
| **B** | 40–59 | 观察 |
| **C/D** | <40 | 不建议 / 回避 |

---

## 七、威科夫关键价位术语（诊股输出会涉及）

| 术语 | 含义 |
|------|------|
| VPOC | 成交量最大的价格（重心） |
| VAH | 价值区上沿（68%） |
| VAL | 价值区下沿（68%） |
| HVN | 高成交量节点 |
| LVN | 低成交量节点 |

---

## 八、相关数据接口速查

### akshare

```python
import akshare as ak

# 全部 A 股代码（约 5500 只）
ak.stock_info_a_code_name()

# 单只历史日线（前复权）
ak.stock_zh_a_hist(symbol="000001", period="daily",
                   start_date="20240101", end_date="20260101", adjust="qfq")
```

### efinance

```python
import efinance as ef

# 日线、前复权
ef.stock.get_quote_history(code, klt=101, fqt=1)
```

---

## 九、注意事项

1. 安装命令、参数可能随版本更新，**以官方 `docs.clawhub.ai` 页面与 README 为准**。
2. 数据源为公开免费接口（akshare / efinance），拉取全市场日线时可能较慢，注意网络与限流。
3. 本工具定位为「研究 / 教育 / 信息」用途，输出结果需自行判断，**不构成投资建议**。
