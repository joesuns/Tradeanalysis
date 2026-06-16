# DDE 警惕 2-bar + 趋势强度 5-bar 调参实施计划

> **日期：** 2026-06-16  
> **状态：** 已实施（2026-06-16）  
> **Goal：** 将 `dde_alert`/`w_dde_alert` 改为 DDX2 相邻 **2-bar** 斜率拐点；`dde_trend_strength`/`w_dde_trend_strength` 改为 **5-bar** 加权回归（decay=0.20 不变）；**不改**周线 `dde_trend`（仍为 DDX3 **4-bar**）。  
> **非目标：** DWD 全库 rebuild；除权/adj 路径变更；新增 DWS 列。

---

## 0. 决策摘要（数据架构 + 交易）

| 变更 | 现网 | 目标 | 交易语义 | 架构结论 |
|------|------|------|----------|----------|
| `dde_alert` 日/周 | 5-bar 斜率拐点 | **2-bar** | 极短 DDX2 动量翻转，信号更密、假阳性升 | **脱离 123 B4 对齐** → 移入 soft 层 |
| `dde_trend_strength` 日/周 | 8-bar | **5-bar** | 与 MACD 趋势强度同窗，资金动能更敏感 | **TA 自研列**（本就不在 B4 hard gate） |
| `dde_trend` 周线 | 4-bar DDX3 | **不变** | 保持周趋势稳定 + `w_dde_trend` B4 对齐 | 无改动 |

**B4 硬门禁：** 12 列 → **10 列**（`dde_alert` / `w_dde_alert` 与 `ma_alignment` 同属 soft；extract/export 仍保留 14 列）。

**重算路径：** 仅算法/spec 变 → `DDECalculator.SPEC_VERSION` bump + 窄窗 FULL（`dde` 日+周），**禁止** `rebuild_all_dwd`。

---

## 1. 现网硬编码证据（变更前）

| 位置 | 现值 | 作用 |
|------|------|------|
| `calc_dde.py:599` | `trend_window = 8` | `trend_strength` 生产窗 |
| `calc_dde.py:824` | `window=5` | `_compute_alerts` → `compute_ddx2_slope_alerts` |
| `calc_dde.py:66` | `SPEC_VERSION = "v2"` | 指纹/spec 门禁 |
| `calc_dde.py:21-22` | daily=5, weekly=**4** | **`dde_trend` only**（本计划不动 weekly=4） |
| `b4_alerts.py:64` | default `window=5` | 函数默认参（由 calc 传入） |
| `b4_gate/columns.py` | `dde_alert` ∈ hard | 12 列 hard gate |
| `tests/test_calc_dde.py` | oracle `window=8` | trend_strength golden-master |
| `tests/test_b4_alerts.py` | `window=5` | 123 对齐用例 |

---

## 2. 文件变更清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `backend/etl/calc_dde.py` | **Modify** | 新增常量；`trend_window` 8→5；alert window 5→2；`SPEC_VERSION` v2→v3；更新类 docstring |
| `backend/b4_gate/columns.py` | **Modify** | `dde_alert` 移入 `B4_SOFT_DAILY_FIELDS`；hard 列表自动派生为 5+5=10 |
| `backend/b4_gate/diff.py` | **Modify** | `DDE_FIELDS` 仅保留 `dde_trend`（alert 不再 hard diff） |
| `tests/test_b4_gate_columns.py` | **Modify** | hard 计数 6→5、12→10；assert `dde_alert` ∉ hard |
| `tests/test_calc_dde.py` | **Modify** | `_oracle_dde_trend_strength` 及关联测试 window 8→5；重命名 `test_dde_trend_8bar_window` 注释（该测 legacy `_compute_ddx2_trend`，非生产 trend_strength，可保留 window=8） |
| `tests/test_b4_alerts.py` | **Modify** | 删除 123 window=5 golden；新增 window=2 拐点用例 |
| `tests/test_etl/test_append_calc.py` | **Verify** | DDE APPEND/FULL 等价性仍须 `atol=1e-9` |
| `tests/test_etl/test_batch_append_calc.py` | **Verify** | batch DDE 浮点列 diff |
| `tests/fixtures/b4_gate/golden_*.csv` | **Regenerate** | recalc 后 `verify_b4_gate --export-golden`（仅 10 列 hard） |
| `CLAUDE.md` | **Modify** | DDE 警惕/强度参数；B4 硬门禁 12→10 列说明 |
| `docs/superpowers/specs/2026-05-31-stock-analysis-data-model.md` §6.4 | **Modify** | alert/strength 口径（与 B4 123 脱钩说明） |
| `docs/superpowers/specs/2026-06-09-daily-screening-product-alignment.md` | **Modify** | 硬门禁 12→10；`dde_alert` 标注 soft/TA-native |
| `scripts/diff_vs_123.py` docstring | **Modify** | 12→10 列说明 |

**不改动：** `schema.py` DDL、`export_wide.py` 列名、`orchestrator` 注册、`SIGNATURE_COLS`、`RECALC_SPEC`、`dde_trend` 周线 4-bar 路径。

---

## 3. 代码设计（最小 diff）

### 3.1 常量（`calc_dde.py` 模块顶）

```python
DDE_ALERT_WINDOW = 2          # daily + weekly; TA-native short DDX2 inflection
DDE_TREND_STRENGTH_WINDOW = 5 # DDX2 weighted slope / mean(|DDX2|); aligns MACD strength
# DDE_MONEYFLOW_REGRESSION_* unchanged (weekly trend stays 4)
```

### 3.2 `_compute_dde_derived`

```python
df["trend_strength"] = self._compute_trend_strength(
    df["ddx2"].values.astype(float),
    window=DDE_TREND_STRENGTH_WINDOW,
)
```

### 3.3 `_compute_alerts`

```python
return compute_ddx2_slope_alerts(
    df["ddx2"].values,
    window=DDE_ALERT_WINDOW,
    eps=0.0,
)
```

### 3.4 `SPEC_VERSION`

```python
SPEC_VERSION = "v3"  # v3: alert 2-bar + trend_strength 5-bar (TA-native; not 123 alert parity)
```

### 3.5 B4 soft 层

```python
B4_SOFT_DAILY_FIELDS: List[str] = ["ma_alignment", "dde_alert"]
```

`B4_HARD_DAILY_FIELDS` 保持列表写法，移除 `"dde_alert"` 即可。

---

## 4. 数据流与重算

```
DWD (unchanged) → DDECalculator.calculate/append
  ├─ ddx, ddx2, trend, divergence  （trend 算法不变）
  ├─ alert        ← window 5→2
  └─ trend_strength ← window 8→5
→ dws_dde_{daily|weekly} INSERT (spec_version=v3)
→ v_dws_dde_*_latest → export / API / screening
```

### 4.1 运维命令（实库）

```bash
# 1. 部署代码 + pytest 通过后
python -m backend.cli calc --refresh-spec dde --date <analysis_date>
# 若 refresh-spec 子集不完整，兜底：
# CALC_FORCE_HARD=1 python -m backend.cli calc --date <analysis_date> --ts-code ...  # 或全市场 calc --force

# 2. 重导 golden（sample_500）
python -m scripts.verify_b4_gate --export-golden --date <analysis_date>

# 3. 验收
pytest tests/test_b4_gate*.py tests/test_etl/test_calc_dde.py tests/test_etl/test_b4_alerts.py tests/test_etl/test_append_calc.py -v
python -m scripts.health_check   # Section K 仅 daily dde_trend，不受影响

# 4. 123 diff（预期：dde_alert 若仍 extract 比对会 mismatch — hard diff 已跳过 alert）
python3 -m scripts.diff_vs_123 --date <analysis_date> --breakdown --summary
```

**为何不需 DWD rebuild：** 输入列 `SIGNATURE_COLS` 未变；仅派生列算法变 → spec_version 触发窄窗 FULL 即可。

---

## 5. 测试计划

### Task 1: 常量 + SPEC_VERSION + 生产路径

- [ ] 改 `calc_dde.py` 三处窗长 + `SPEC_VERSION=v3`
- [ ] `pytest tests/test_etl/test_calc_dde.py::test_dde_trend_strength_* -v` — 先红后绿

### Task 2: B4 soft 层

- [ ] 改 `columns.py` / `diff.py`
- [ ] 改 `test_b4_gate_columns.py`（5/10 列）
- [ ] 新增 `test_diff_b4_skips_dde_alert`（仿 `test_diff_b4_skips_ma_alignment`）

### Task 3: alert 2-bar 单测

- [ ] 重写 `test_b4_alerts.py`：构造 2-bar 斜率翻转序列
- [ ] 保留 `test_dde_alert_uses_ddx2` / inflection tests（window=2）

### Task 4: 等价性回归

- [ ] `pytest tests/test_etl/test_append_calc.py -k dde -v`
- [ ] `pytest tests/test_etl/test_batch_append_calc.py -k dde -v`
- [ ] `pytest tests/test_etl/test_incremental_calc.py -k dde -v`（如有）

### Task 5: 文档

- [ ] CLAUDE.md + data-model §6.4 + screening spec 硬门禁列数

### Task 6: 实库验收（可选，有 DuckDB 时）

- [ ] recalc + export golden
- [ ] `verify_b4_gate` 绿
- [ ] 记录 alert 非空率变化（SQL COUNT before/after，运维观测）

---

## 6. 风险与缓解

| 风险 | 级别 | 缓解 |
|------|------|------|
| alert 2-bar 信号过密 | 中 | Excel/人工先观察 1–2 周；combo 仍不用 alert |
| B4 12→10 列文档漂移 | 低 | 本 plan Task 5 同步三份 spec |
| golden CSV 全量失效 | 低 | recalc 后 `--export-golden`；hard 列不含 alert |
| `diff_vs_123` alert mismatch | **预期** | alert 已 soft；hard breakdown 应不含 alert |
| trend_strength 阈值回测失效 | 低 | 当前无 combo 依赖；若未来用需重标定 |
| APPEND 等价性破坏 | 高 | Task 4 必跑；失败则禁止合入 |

---

## 7. 验收标准（Definition of Done）

1. `pytest tests/ -v` 全绿（或至少 DDE + B4 相关子集全绿）
2. `verify_b4_gate` 对重导 golden **10 列 hard** 零 mismatch
3. `diff_vs_123 --breakdown` hard 10 列中 **`dde_trend`/`w_dde_trend` 不回归**（周线仍 4-bar）
4. 实库 `dws_dde_*` 新快照 `spec_version='v3'`
5. CLAUDE.md 与 spec 已更新

---

## 8. 审批后执行顺序

1. 代码 + 单测（Task 1–4）  
2. 文档（Task 5）  
3. 用户确认实库 analysis_date → calc refresh-spec dde  
4. golden 重导 + verify（Task 6）  
5. 收尾核对（engineering-protocol §⑤）

---

**等待审批：** 回复「同意」或「可以」后开始 Task 1 编码。
