# 数据获取效率 + 计算性能 完整优化方案

**日期：** 2026-06-06
**作者视角：** 数据架构师
**状态：** Phase A（A1–A5）+ B1 + B2 全部已落地（TDD + golden-master，全量 pytest 绿）

> **B2 落地记录（2026-06-06，分支 `perf/b2-polyfit-vectorization`）**：逐 bar `np.polyfit` 全部向量化。
> - 核心：`base.weighted_window_slopes(y, window, decay)` 定窗加权回归斜率闭式（**关键：polyfit 权重
>   按 `w²` 进入 WLS**），`base.sliding_window_mean_abs` 滑动 |·| 均值用于归一化。
> - MACD（decay=0.15）、DDE（0.20）、MA slope（无权 decay=0）：定窗，直接闭式替换。
> - Volume（decay=0.20，ln(vol)）：变长压缩窗口 → **混合**：全正满窗走闭式快路径，含 NaN/非正值的
>   少数窗口回退原逐 bar 逻辑，逐位等价。
> - 护栏：每个函数都有 golden-master 测试（oracle=冻结的旧 polyfit 循环，随机数据含 NaN/零负/不足窗，
>   斜率 `atol=1e-9` + 分类/NaN 位置逐一相等）。
> - 微基准：MACD trend+strength ~0.16ms/序列 vs 旧仅 trend ~20ms/序列（~250×）。
> - 背离检测里的 `linear_regression_slope`（单次 gap 斜率，非逐 bar 热循环）不在本次范围，保留。

> **落地记录（2026-06-06）**：按审批顺序 `A4 → A2 → A1 → A3 → A5 → B1` 完成。
> - A1 共享限流：`client.py` 新增 `_InterfaceRateLimiter`（进程级、按接口滑动窗口、`threading.Lock`），
>   `TushareClient._limiter` 类级共享，`PER_API_LIMIT=480`。
> - A2 stock-batched 批量 INSERT：`fetch_stocks_incremental` 三接口改 `register`+`INSERT SELECT`。
> - A3 本地日历：`_local_trading_days(con,start,end)`，覆盖时查 `dim_date`，否则回退 `trade_cal`；
>   `_get_trading_days` 与 `fetch_stocks_incremental` 都已改走。
> - A4 指纹批量：`base.load_latest_fingerprints` 一次取回 `{ts_code:fp}`，6 个计算器预取后传入
>   `check_dwd_unchanged(..., latest_fps=...)`。
> - A5 latest 视图：12 个 `v_dws_*_latest` 改 `QUALIFY ROW_NUMBER() OVER (...)=1`。
>   ⚠️ **实库需重跑 schema 初始化**（`CREATE OR REPLACE VIEW`）才会替换旧视图。
> - B1 计算器批量取数：`base.load_quote_groups`（5 个简单计算器共用）+ DDE 的
>   `_load_daily_batch`/`_load_weekly_batch`；指标计算逻辑零改动，等价性由"批量帧==逐股查询"测试锁定。
> - B2（polyfit 向量化）**未做**，按计划独立分支 + golden-master 推进。

> 说明：审计基于当前代码逐项核实（含行号）。其中 P0 快照清理已于 2026-06-05 落地
> （`prune_dws_snapshots` + CLI `prune`，见 `2026-06-05-dws-snapshot-retention.md`），
> 本方案只补其遗留的 latest 视图改写。其余 5 项均未动。

---

## 0. 现状核对（审计结论）

| # | 问题 | 提报级别 | 当前代码状态 | 证据 |
|---|---|:---:|---|---|
| 1 | DWS 快照无限增长 | P0 | **部分完成**：清理已做；latest 视图仍是关联子查询 | `schema.py:483-491` 仍 correlated subquery |
| 2 | 限流器跨线程不协调 | P1 | **未做** | 每线程各建 `TushareClient`（`ods_daily.py:448`），计数器每实例独立（`client.py:22-37`），3×600=1800/min |
| 3 | stock-batched 逐行 INSERT | P1 | **未做** | `fetch_stocks_incremental` 逐行 `con.execute`（`ods_daily.py:328-380`），date-batched 已用 bulk（`ods_daily.py:444`） |
| 4 | 交易日历重复走 API | P2 | **未做** | `_get_trading_days` 调 `trade_cal`（`ods_daily.py:154`）；`fetch_stocks_incremental` 也调（`:301`）；`dim_date` 本地已有 |
| 5 | 计算器 N+1 + 逐 bar polyfit | P1 | **未做** | 每股一次 SELECT（`calc_macd.py:26-39`）；polyfit 逐 bar 循环（`calc_macd.py:91,124`） |
| 6 | 指纹检测 N+1 | P1 | **未做** | `check_dwd_unchanged` 每股一次 SELECT（`base.py:165-169`） |

**架构师再排序**：考虑到 P0 清理已落地、`prune --keep 1` 可把每键快照压到 1 行，
原 P0 的"latest 视图慢"已大幅缓解，故 latest 改写降级为 P2（仍建议做，纯收益）。
当前真正的性能瓶颈是 **#5 计算器 N+1 + 逐 bar polyfit**（~20min 计算的主因）。

---

## 1. 分阶段策略

- **Phase A — 低风险快赢**（#2 #3 #4 #6 #1）：互不耦合，逐项 TDD，单独可上线。
- **Phase B — 高收益高风险**（#5）：大重构，必须 golden-master 测试护栏（与现实现逐行比对），独立分支推进。

---

## Phase A

### A1（#2）进程级共享限流（令牌桶 + 锁）

**根因**：`_rate_limit` 用实例级 `self._calls/_window_start`；并行 fetch 每线程一个 client →
每线程各自按 600/min 放行。**tushare 限流按接口独立计**（6200 积分=5000+ 档，每接口 500/min，
每日总量无上限），3 线程并发**同一接口**（如 `daily`）合计峰值 1800/min ≫ 单接口 500 限额
→ 该接口被节流甚至封 IP。

**方案**：限流状态提升为**类级共享、按接口名（func_name）分桶**，`threading.Lock` 保护；
每接口独立 ≤ `PER_API_LIMIT`（取 480，官方 500 留余量）。这样跨线程对每个接口都不超限，
且不同接口互不挤占预算 → 吞吐最大化（优于"全局单一计数器"会让 4 接口共享一份预算）。
```python
class TushareClient:
    PER_API_LIMIT = 480          # 单接口/分钟（官方500，留余量；实测645不节流）
    _lock = threading.Lock()
    _calls: dict = {}            # {func_name: count}
    _win: dict = {}              # {func_name: window_start}

    def _rate_limit(self, func_name: str):
        with TushareClient._lock:
            now = time.time()
            ws = TushareClient._win.get(func_name, now)
            if now - ws >= 60:
                TushareClient._calls[func_name] = 0
                TushareClient._win[func_name] = now
                ws = now
            TushareClient._calls[func_name] = TushareClient._calls.get(func_name, 0) + 1
            if TushareClient._calls[func_name] >= self.PER_API_LIMIT and now - ws < 60:
                time.sleep(60 - (now - ws) + 1)     # 持锁等待：该接口强制串行节流
                TushareClient._calls[func_name] = 0
                TushareClient._win[func_name] = time.time()
```
`call(func_name, ...)` 把 `func_name` 传入 `_rate_limit`。
**TDD**：多线程并发调用 mock，注入假时钟，断言每个接口 60s 窗口放行数 ≤ PER_API_LIMIT，
不同接口预算独立。
**风险**：低-中。持锁 sleep 让同接口在限流点串行（正是目的）。
**适用边界**：日常增量每天仅 1 交易日 × 4 接口 ≈ 几次调用，根本不触限流；该修复主要保护
**历史回补**（数千交易日 × 4 接口）场景，避免封 IP。
**收益**：合规且吞吐最大（每接口跑满 500）。

### A2（#3）stock-batched 改批量 INSERT

**根因**：`fetch_stocks_incremental` 对 daily/daily_basic/moneyflow 各逐行 `con.execute`，
项目自注释 register+SELECT 快 ~666×（0.04s vs 28s/5500 行）。

**方案**：与 date-batched 一致——把每段 recs 收集成 list[dict] → `con.register` → 单条
`INSERT OR REPLACE ... SELECT`。三个接口各一次批量写。
**TDD**：构造多条 mock recs，跑增量路径，断言写入行数/内容正确（用临时库真实 INSERT）。
**风险**：低。
**收益**：补拉大量股票时该路径从分钟级降到秒级；消除"同代码两套标准"。

### A3（#4）交易日历优先查本地 dim_date

**根因**：增量场景每次调 `trade_cal` API，`dim_date` 已有全量日历。

**方案**：新增 `_load_trading_days(con, start, end)` 优先查 `dim_date WHERE is_trade_day=1`；
仅当本地为空或区间未覆盖（max(dim_date) < end）时回退 API。`_get_trading_days` 与
`fetch_stocks_incremental` 都改走它。
**TDD**：dim_date 有数据时不调 API（mock client.call 断言未被调用）；区间超出时回退。
**风险**：低（需保证 dim_date 新鲜——它由 `build_dim_date` 从 `ods_trade_cal` 构建）。
**收益**：增量跑批省若干 API 往返 + 网络延迟。

### A4（#6）指纹检测批量化

**根因**：`check_dwd_unchanged` 每股每指标一次 `SELECT ... LIMIT 1`，全市场 ~6.6 万次往返。

**方案**：在 Calculator 处理一组股票前，一次性取回该组最新指纹：
```sql
SELECT ts_code, input_fingerprint
FROM (SELECT ts_code, input_fingerprint,
             ROW_NUMBER() OVER (PARTITION BY ts_code ORDER BY calc_date DESC) rn
      FROM {dws_table} WHERE ts_code IN (...) AND input_fingerprint IS NOT NULL)
WHERE rn=1
```
得到 `{ts_code: fp}` 字典，逐股内存比对。改 `check_dwd_unchanged` 接受预取字典，或新增
`load_latest_fingerprints(con, table, ts_codes)`。
**TDD**：预取字典命中→跳过；不命中/无记录→不跳过。与现行为逐股等价。
**风险**：低。
**收益**：~6.6 万次 SQL → ~（组数×12）次。与 A5/B 的批量取数天然配套。

### A5（#1 遗留）latest 视图改写为 QUALIFY 窗口函数

**根因**：`v_dws_*_latest` 用关联子查询，每行一次子查询；快照多时慢。

**方案**：12 个视图统一改：
```sql
CREATE OR REPLACE VIEW {view} AS
SELECT * EXCLUDE (rn) FROM (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY ts_code, trade_date
                               ORDER BY calc_date DESC) rn
  FROM {table}
) WHERE rn = 1
```
或用 DuckDB `QUALIFY`。
**TDD**：构造同 (ts_code,trade_date) 多 calc_date，断言新视图与旧视图返回**逐行一致**
（含指纹交错场景：某键最新值在旧 calc_date）。
**风险**：低（语义等价，需逐行比对验证）。
**收益**：视图查询/导出更快且随快照数稳定；与 `prune` 互补。

---

## Phase B（#5）计算器 N+1 + 逐 bar polyfit 向量化

> ⚠️ 最高收益（~20min → 目标 ~5min）也是最高风险。**必须**先建 golden-master 测试。

**根因**：
1. 每个 Calculator `for ts_code in ts_codes: SELECT ... WHERE ts_code=?` → 5500×6×2 ≈ 6.6 万次
   SQL + 6.6 万次 DataFrame 构建。
2. `_compute_trend/_compute_trend_strength/_compute_divergence` 等对每根 bar 跑
   `np.polyfit`（`calc_macd.py:91,124`，`calc_dde.py` 同理）。

**方案（分两步，可分别上线）**：
- **B1 批量取数**：一次 `SELECT ... WHERE ts_code IN (chunk) ORDER BY ts_code, trade_date`
  取回整组，`groupby('ts_code')` 后在内存分股计算。SQL 往返从"每股一次"降到"每组一次"。
  指标计算逻辑**完全不动**（仍逐股调现有 `_compute_*`），只改取数与分发。风险低、收益中。
- **B2 polyfit 向量化**：固定窗口加权线性回归的斜率有解析解（无需 polyfit）：
  对窗口 `x=0..n-1`、权重 `w`，加权最小二乘斜率
  `slope = Σw·(x-x̄_w)(y-ȳ_w) / Σw·(x-x̄_w)²`，分母对固定窗口为常数，分子可用滑动点积/
  卷积一次算完整列。把逐 bar 循环替换为向量化列运算。风险中-高（数值等价性），收益高。

**测试护栏（golden-master）**：
- 先用当前实现对一组固定股票跑出 DWS 全字段快照，存为基准。
- 重构后对同输入跑，断言**所有字段逐行数值相等**（浮点容差 1e-9）。
- 覆盖：正常、停牌（NaN carry）、不足窗口、跨年周、divergence/turning_point 边界。

**实施顺序**：B1 先行（低风险即得 SQL 收益）→ 验证 → B2 逐指标向量化（MACD→DDE→其余），
每个指标单独 golden-master 通过才合并。

**风险**：B2 改动核心数值逻辑，任何偏差都会污染全市场指标。故要求：独立分支、逐指标推进、
golden-master + 全量 pytest 双绿、保留回滚点。

---

## 2. 汇总：优先级 / 工作量 / 收益

| 项 | 优先级 | 工作量 | 风险 | 预期收益 |
|---|:---:|:---:|:---:|---|
| A1 共享限流 | P1 | 0.5d | 低-中 | 消除封 IP；合规 |
| A2 stock 批量 INSERT | P1 | 0.5d | 低 | 增量大批量 分钟→秒 |
| A3 本地日历 | P2 | 0.25d | 低 | 省 API 往返 |
| A4 指纹批量 | P1 | 0.5d | 低 | 6.6万 SQL → 数百 |
| A5 latest QUALIFY | P2 | 0.5d | 低 | 视图/导出稳定提速 |
| B1 计算器批量取数 | P1 | 1d | 中 | 6.6万 SQL → 数百 |
| B2 polyfit 向量化 | P1 | 1.5-2d | 中-高 | 计算 ~20min → ~5min |

**建议落地顺序**：A4 → A2 → A1 → A3 → A5（Phase A 快赢）→ B1 → B2（Phase B 攻坚）。

---

## 3. 待决策点 + 架构师建议

> 限流相关依据：tushare 按**接口独立**限流；6200 积分=5000+ 档 → **每接口 500/min、每日总量
> 无上限**（官方文档 doc_id=290 + GitHub issue #1001）。

1. **范围** → 建议 **Phase A 全做 + B1**（都是低/中风险，fetch 与计算 SQL 收益立得）；
   **B2 单独排期、独立分支**（高风险数值重构，不与其它项混在一批）。
2. **限流预算** → 建议 **按接口共享、每接口 480/min**（≈官方 500 留余量），**而非全局单预算**。
   理由：限流本就 per-interface，分桶后每接口都能跑满且互不挤占；全局单预算会浪费吞吐。
3. **B2 正确性判据** → 建议 **golden-master 即唯一基准**（与现实现逐行比对，容差 1e-9）。
   B2 是"行为等价的性能重构"，验收只需证明输出不变；与外部源比对属"指标算法正确性"另一议题，
   不应混入，否则范围发散。
4. **实施方式** → 建议 **每项独立提交 + 独立 TDD**；Phase A 在主线，B（尤其 B2）走独立分支，
   逐指标 golden-master + 全量 pytest 双绿才合并，保留回滚点。

---

## 4. 不在本方案
- 不重复 P0 快照清理（已落地）。
- 不改指标业务口径/参数（纯性能重构，行为等价）。
- delist_date 未填充（健康体检发现，属元数据完整性，另议）。
