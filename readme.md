# Wyckoff A股分析工具

基于威科夫（Wyckoff）方法论的个人 A 股分析工具集，支持**全市场早期吸筹选股**、**深跌后横盘筛选**和**单股诊股**。规则型初筛工具，结果需人工看图复核，不构成投资建议。

## 项目结构

```
wyckoff/
├── main.py               # 统一入口：screen、base、diagnose
├── wyckoff_screen.py     # 威科夫 4.1 选股（主工具，三个名单）
├── bottom_base.py        # 深跌后横盘筛选
├── diagnose.py           # 单股诊股（可独立运行）
├── backtest.py           # 历史快照和后续收益统计
├── pyproject.toml        # 项目配置（uv 管理）
├── uv.lock
└── readme.md
```

## 环境安装

- Python >= 3.12
- 依赖：`akshare` `pandas` `numpy` `requests` `openpyxl`（K 线图可选 `matplotlib`）

```bash
uv sync
# 需要生成 K 线图时
uv sync --extra charts
```

## 快速开始

### 选股（全市场威科夫筛选）

```bash
# 冒烟测试（前 100 只）
python wyckoff_screen.py --limit 100 --threads 5

# 全量扫描，输出前 30 只 + 生成 5 张 K 线图
python wyckoff_screen.py --threads 5 --charts 5

# 通过 main.py 统一入口（等价于上面）
python main.py screen --threads 5 --charts 5
```

### 诊股（单只股票）

```bash
# 支持代码或名称
python main.py diagnose 601985
python main.py diagnose 中国核电

# 或直接运行
python diagnose.py 601985
```

### 深跌后横盘（吉视传媒模板）

在项目目录运行。首次使用先执行上面的环境安装步骤。

```powershell
# 离线扫描 cache/ 里的全部日线缓存
uv run python -X utf8 main.py base --cache-only

# 联网扫描沪深主板非 ST 股票
uv run python -X utf8 main.py base --threads 5

# 离线复查单只股票，例如吉视传媒或电投产融
uv run python -X utf8 main.py base --cache-only --code 601929
uv run python -X utf8 main.py base --cache-only --code 000958
```

如果 Windows 上已创建项目的 `.venv`，也可以直接运行：

```powershell
.\.venv\Scripts\python.exe -X utf8 main.py base --cache-only
```

该入口筛选大涨、长期深跌后逐步趋稳的股票，要求至少 60 根日线的底部整理，且收盘价在区间两端至少完成两次往返。候选名单直接显示在终端，包括估计的横盘起点、主要收盘区间、往返次数和行情日期。离线模式的行情新旧取决于本地缓存。`base` 不要求 Spring、Test 或 SOS，也不应用 `screen` 的流通市值过滤。需要原来的三个名单和 Excel 时，使用上面的 `screen` 命令。

## 选股参数

| 参数 | 默认 | 说明 |
|------|------|------|
| `--threads` | 5 | 并发取数线程数 |
| `--limit` | 0 | 只扫前 N 只，0 为全量 |
| `--top` | 30 | 终端显示前 N 只 |
| `--charts` | 5 | 生成 K 线图数量，0 关闭 |
| `--no-cap-filter` | - | 跳过流通市值过滤 |
| `--min-cap` | 30 | 最小流通市值（亿元） |
| `--max-cap` | 300 | 最大流通市值（亿元） |
| `--score-threshold` | 80 | 名单最低评分 |

## 选股逻辑（4.1）

- 范围：沪深主板（600/601/603/605、000/001/002/003），排除 ST/退市，流通市值 30~300 亿
- 结构硬过滤先于评分；结构失效或价格偏离不靠高分进名单
- Spring 和 SOS 各自用触发日之前的日线确定支撑和阻力。后续日线用于验证结构是否延续。
- 输出**三个独立名单**：
  1. **Spring+Test 主名单**：Spring 假跌破 → 明确反弹 → 缩量回踩（Test）
  2. **Spring 早期观察**：Spring 事件较新、尚无 Test
  3. **LPS 备选**：SOS 放量突破并站稳，首次缩量回踩后再走强才入选。全量结果同时保留观察中、已确认和失效状态。

### 历史快照回测

```bash
# 用现有行情缓存离线验证，跳过市值过滤
uv run python backtest.py --cache-only --limit 60 --snapshots 4 --horizon 10
```

快照和当日选股共用入选规则。收益从信号后一根日线开盘算到第 N 根日线收盘，未计交易成本或成交限制。联网运行默认使用运行时的股票池和流通市值，离线运行使用缓存中的股票。统计结果会受到样本范围影响。

## 输出产物

- `wyckoff_screen_4_1_YYYYMMDD.xlsx`：三个选股名单、排除名单、全量扫描和扫描说明
- `charts_YYYYMMDDHH_MM/`：入选股票的 K 线图（带支撑/阻力线 + Spring/Test/SOS/LPS 事件标注）
- `wyckoff_screen.log`：运行日志

## 数据源

- 股票列表：交易所官网（`ak.stock_info_a_code_name`），稳定不限流
- 日线行情：腾讯（`ak.stock_zh_a_hist_tx`，前复权）
- 流通市值：腾讯实时行情（`qt.gtimg.cn`）
- **不使用东方财富接口**（push2 接口易限流）

## 免责声明

本工具为规则型技术初筛，不构成任何投资建议；所有信号均为规则候选，请结合基本面与人工看图复核后自行决策。
