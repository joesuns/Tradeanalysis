# Calc 新日追算（Append-Only）架构设计

- 日期：2026-06-07
- 状态：设计稿，待实施
- 关联：`docs/superpowers/plans/2026-06-07-calc-incremental-optimization.md`（增量优化）、`docs/superpowers/plans/2026-06-07-calc-thread-pool-lock-fix.md`（线程池锁修复）
- 数据模型 spec：`docs/superpowers/specs/2026-05-31-stock-analysis-data-model.md` §12.7

## 1. 背景与问题

当前增量 calc（`CALC_INCREMENTAL=1`）每次运行对每只股**窄写 255-bar 窗口**，并逐股走 `calc_stock_pipeline`。实库验收结论：

- **新交易日 calc：~49min**（5388 股 @ ~1.6 stk/s）+ 约 320s 全市场 freshness-fetch。
- **指纹跳过仅加速「同日重跑」**：`compute_input_fingerprint` 含 `last_td`，新交易日 `last_td` 变化 → 指纹必不命中 → 全量重算。
- 实测瓶颈是**逐股迭代开销**（每股多次小 SQL + df 构造 + Python 循环，≈625ms/股），而非窗口宽度或 numpy 数值本身。

目标：常规交易日的 calc 计算从 ~49min 降到**秒级~低分钟级**，真正支撑快日更。

### 1.1 已知缺陷（本设计一并修复）

- **弱指纹（M2）**：`compute_fingerprint` 仅取每列 `min/max/mean/count`（6 位小数），存在碰撞面 → 可能漏检数据变更 → 误跳过。
- **历史失真（H2/M1）**：现指纹域 `[recalc_start, last_td]` 不含 recalc_start 之前的 seed/lookback bar。前复权 `close_qfq = close × adj / latest_adj` 在除权日重标定**全历史**，而增量只重写窗口内行 → recalc_start 之前的历史 MA/MACD 量级静默失真。
- **指纹查找非确定（线程池修复期发现）**：`load_latest_fingerprints` 仅 `ORDER BY calc_date DESC`，同一 calc_date 混有多代指纹时任取一行 → 跳过时灵时不灵。

## 2. 设计目标与非目标

**目标**
- 常规日 calc 计算秒级~低分钟级（5300+ 股）。
- 数值与现全量/窄窗路径**逐值等价**（`atol=1e-9`）。
- 统一处理除权 / 停牌填充 / 数据修正引起的历史变动，杜绝静默失真。

**非目标（点明，单独立项）**
- **`~320s 全市场 freshness-fetch` 开销不在本设计内**。即使 calc 降到秒级，日更端到端仍被该 fetch 拖住——这是兑现「秒级日更」的**下一个**必须项。
- 不改 DWS 快照模型、不改 `v_*_latest` 视图语义。

## 3. 核心架构：双路径模型

每只股每 freq 落入三态之一：

```
                    ┌─ 无新 bar & 签名同 ──────────→ SKIP（不做任何事）
每股(freq) ─判定─┤
                    ├─ 有新 bar & 历史签名同 ──────→ APPEND（向量化只算新 bar）★主路径
                    └─ 历史签名变（除权/填充/修正）→ FULL（现有窄窗 255 重算，逐股）
```

- 常规交易日：几乎全部走 APPEND（除权股每日个位数）。
- APPEND 跨股向量化处理；FULL 复用现有 `calc_stock_pipeline`（少数股，逐股可接受），同时作为 APPEND 的等价性 oracle。
- 三层保险开关：`CALC_APPEND=1`（默认）→ `=0` 回退到现 `CALC_INCREMENTAL` 窄窗 → `CALC_INCREMENTAL=0` 全量。

## 4. 状态表 `dws_calc_state`

```sql
CREATE TABLE dws_calc_state (
    ts_code          VARCHAR NOT NULL,
    freq             VARCHAR NOT NULL,       -- 'daily' | 'weekly'
    last_trade_date  VARCHAR NOT NULL,       -- 该股已写入 DWS 的最新 bar
    history_fp       VARCHAR NOT NULL,       -- [load_start, last_trade_date] 强签名
    quote_latest_adj DOUBLE,                 -- 上次计算的 latest_adj（除权快速预筛，可选优化）
    spec_version     VARCHAR DEFAULT 'v1',
    updated_calc_date VARCHAR NOT NULL,
    PRIMARY KEY (ts_code, freq)
);
```

- 一行 per `(ts_code, freq)`。日线/周线各一份。
- 缺失（首次部署 / 新股）→ 视为「无基线」→ 走 FULL 建立状态，之后转 APPEND。

### 4.1 强签名 `history_fp`

修复弱指纹（M2）与历史失真（H2/M1）：

- **签名域**：`[load_start, last_trade_date]`，**包含** recalc_start 之前的 seed/lookback bar。
  - 除权重标定全历史 close_qfq → 签名必变 → 触发 FULL → 历史不再静默失真。
- **签名内容**：对关键输入列的**实际值序列（四舍五入到固定精度）**做 SHA256，而非汇总统计。
  - quote 类：`trade_date + close_qfq + open_qfq/high_qfq/low_qfq + vol + amount`。
  - DDE 额外含 moneyflow 输入（`buy_lg/sell_lg/...` 净额）。daily/weekly 各自的输入。
- 精度：value 四舍五入位数需与 DWD 存储精度对齐，确保「真实变更必反映、浮点噪声不误判」。

## 5. APPEND 主路径（跨股向量化）

按 freq 一次性处理所有 APPEND 资格股：

1. **批量取尾窗**：一条 `WHERE ts_code IN (...)` + 内存 groupby 取回各股 lookback 尾窗（max lookback = 250 daily / 周线对应窗口），沿用 `load_quote_groups`。窗口须含「新 bar 在内的尾部」，与 FULL 在该 bar 的窗口完全一致。
2. **批量取 EMA 种子**：用 `ROW_NUMBER` 批量查询取回各股 DWS 最新 bar 的 EMA 列（MACD `ema_12/ema_26/dea`、DDE `ddx2`），一次取整组。
3. **向量化算新 bar 指标**（仅新 bar 位置，新 bar 通常 1 根、偶尔几根）：
   - **EMA 类**：`ema_new = α·x + (1-α)·seed`，跨股一次数组运算；k 根新 bar 则 k 步短递推（k = 距上次 calc 的交易日数，通常 1）。NaN（停牌）携带 prev、不更新 prev。
   - **滚动类**：PP 250 min/max、divergence 60、volume 120 分位——在尾窗内对新 bar 位置批算极值/分位。
   - **K线形态**：纯当根/近几根，向量化天然适配。
4. **只 INSERT 新 bar 行**（新 calc_date 快照），更新 `dws_calc_state`。

DWS 仍是 INSERT-only 快照，新 bar 落新 calc_date；新 bar 的 trade_date 仅一份快照，`v_*_latest` 语义不变。

### 5.1 周线

周线新 bar 仅在真周末（`is_week_end=1`）出现；多数交易日无新周线 bar → 自然 SKIP。逻辑与日线一致。

## 6. 判定流程（每股每 freq）

```
new_bars   = DWD 中 trade_date > state.last_trade_date 的 bar（freq 对应过滤）
cur_fp     = 强签名([load_start, DWD_max_trade_date])

if state 缺失:                         → FULL（建立基线）
elif cur_fp != state.history_fp:        → FULL（除权/填充/修正）
elif not new_bars:                      → SKIP
else:                                   → APPEND(new_bars)
```

- FULL 与 APPEND 完成后均刷新 `dws_calc_state`（`last_trade_date`、`history_fp`）。

## 7. 数值等价性（硬约束）

- **Golden-master**：同批股、同新 bar，分别跑 FULL（oracle）与 APPEND，断言 6 大指标全列 `atol=1e-9` 相等。
- **EMA 种子等价**：复用 `resolve_ema_seeds`；新增「种子递推 == 全窗口重算」边界测试（含停牌 NaN 携带）。
- **滚动窗口边界**：新 bar 的 250/120/60 窗口须与 FULL 在该 bar 的窗口完全一致；覆盖「上市不足窗口」「窗口跨停牌」。

## 8. 测试策略

**签名机制**
- 签名变更触发 FULL：构造除权（latest_adj 变）、停牌填充、单 bar 修正三类，断言各落 FULL。
- 签名不变走 APPEND：数据未变断言走 APPEND 且不重写历史行。
- 强签名不漏检（针对 M2）：构造「min/max/mean/count 不变但有值变」样本，断言签名仍变。

**路径与回退**
- `CALC_APPEND=0` 回退现窄窗增量；`CALC_INCREMENTAL=0` 回退全量；结果均与 oracle 一致。
- 首次/无 state 股走 FULL 建立基线，第二次转 APPEND。

## 9. 验收基准

- 常规日（无除权）5300 股 calc 计算墙钟：目标 ~49min → 秒级~低分钟级，记 `ods_etl_log`。
- 除权日：少数股 FULL，其余 APPEND，总耗时仍远低于全量。
- `health_check` 全绿；抽样股 APPEND 结果 == 全量重算结果。

## 10. 风险与缓解

| 风险 | 缓解 |
|---|---|
| APPEND 与 FULL 数值不等价 | golden-master `atol=1e-9` 锁定；不等价则该指标禁用 APPEND |
| 强签名精度选错（漏判/误判） | 精度与 DWD 存储对齐，加边界测试 |
| 跨股向量化 EMA 递推实现错 | 短递推（k 步）逐步向量化 + oracle 对比 |
| 部署期 state 缺失致全 FULL | 首轮 FULL 建基线（一次性，等同现状），之后 APPEND |
| DuckDB 并发写（沿用线程池） | 复用已验证的 ThreadPoolExecutor 模式；APPEND 写量小、冲突面更低 |

## 11. 范围外后续项

1. **freshness-fetch 提速**（端到端秒级日更的下一个必须项）。
2. 指纹查找非确定性修复（`load_latest_fingerprints` 加 `trade_date DESC`）——在 FULL 路径仍有意义，可并入本次或单独小修。
