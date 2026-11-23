# Wyckoff A股分析工具

基于威科夫（Wyckoff）方法论的个人 A 股分析工具集，包含**全市场早期吸筹选股**和**单股诊股**两大功能。规则型初筛工具，结果需人工看图复核，不构成投资建议。

## 项目结构

```
wyckoff/
├── main.py               # 统一入口：screen（选股）+ diagnose（诊股）
├── wyckoff_screen.py     # 威科夫 4.1 选股（主工具，三个名单）
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
