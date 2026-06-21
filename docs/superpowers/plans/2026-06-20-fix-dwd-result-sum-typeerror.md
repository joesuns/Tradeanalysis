# Fix DWD Result Dict `sum()` TypeError

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix `TypeError` crash when `sum(dwd_result.values())` encounters the new `changed_codes` list field added by commit `7103f81`.

**Architecture:** Commit `7103f81` added a `changed_codes: list[str]` key to the DWD rebuild result dict returned by `rebuild_dwd_incremental()` and `rebuild_all_dwd()`. Two call sites use `sum(result.values())` to compute total row count — this now includes the list and raises `TypeError: unsupported operand type(s) for +: 'int' and 'list'`. One call site iterates `result.items()` and passes each value as `row_count` to `log_etl_end()` — this passes a list where an int is expected. Fix: add a helper `_dwd_rebuild_row_count()` that sums only numeric values, and filter `changed_codes` from the orchestrator iteration.

**Tech Stack:** Python ≥3.9, DuckDB

---

## 影响范围总结

| # | 文件 | 行号 | 问题 | 风险 |
|---|------|------|------|------|
| 1 | `backend/cli.py` | 437 | `sum(dwd_result.values())` 含 list | **CRASH** (d14a5a35 实锤) |
| 2 | `backend/etl/refresh_pipeline.py` | 258 | 同上 | **CRASH** (未触发) |
| 3 | `backend/etl/orchestrator.py` | 172-173 | `result.items()` 遍历含 `changed_codes`，list 当 row_count 传入 log_etl_end | **静默错误** (DuckDB INSERT 可能报错或写入异常值) |

以下调用点 **安全**（result dict 仅作为整体传递，不迭代 values）：
- `orchestrator.py:580,1612,1645` — `log_timed_step` 直接透传返回值
- `cli.py:448-506` — 用 `dwd_result` 做 truthy 检查，`maybe_refresh_state_after_dwd_rebuild` 通过 `.get('changed_codes')` 安全访问

---

## 文件结构

- **Modify** `backend/etl/build_dwd.py` — 新增 `_dwd_rebuild_row_count()` 辅助函数
- **Modify** `backend/cli.py:437` — 使用辅助函数
- **Modify** `backend/etl/refresh_pipeline.py:258` — 使用辅助函数
- **Modify** `backend/etl/orchestrator.py:172` — 过滤 `changed_codes` 键
- **Modify** `tests/test_etl/test_build_dwd.py` — 新增回归测试

---

### Task 1: 新增 `_dwd_rebuild_row_count()` 辅助函数

**Files:**
- Create: (无)
- Modify: `backend/etl/build_dwd.py` — 在 `rebuild_dwd_incremental` 和 `rebuild_all_dwd` 函数之后新增
- Test: `tests/test_etl/test_build_dwd.py` — 新增测试

- [ ] **Step 1: 写入回归测试**

在 `tests/test_etl/test_build_dwd.py` 末尾新增：

```python
def test_dwd_rebuild_row_count_ignores_changed_codes():
    """_dwd_rebuild_row_count sums only numeric fields, skipping changed_codes."""
    from backend.etl.build_dwd import _dwd_rebuild_row_count

    # Normal case
    result = {
        "daily_quote": 10,
        "weekly_quote": 5,
        "moneyflow": 92,
        "changed_codes": ["000001.SZ", "000002.SZ"],
    }
    assert _dwd_rebuild_row_count(result) == 107

    # Empty changed_codes
    result2 = {
        "daily_quote": 0,
        "weekly_quote": 0,
        "moneyflow": 0,
        "changed_codes": [],
    }
    assert _dwd_rebuild_row_count(result2) == 0

    # All zero (daily_basic gate deferred)
    result3 = {
        "daily_quote": 0,
        "weekly_quote": 0,
        "moneyflow": 92,
        "changed_codes": [],
    }
    assert _dwd_rebuild_row_count(result3) == 92

    # Only changed_codes populated (insert + qfq)
    result4 = {
        "daily_quote": 150,
        "weekly_quote": 10,
        "moneyflow": 150,
        "changed_codes": ["A.SZ", "B.SZ"],
    }
    assert _dwd_rebuild_row_count(result4) == 310
```

- [ ] **Step 2: 运行测试确认失败**

```bash
pytest tests/test_etl/test_build_dwd.py::test_dwd_rebuild_row_count_ignores_changed_codes -v
```

Expected: FAIL with `ImportError: cannot import name '_dwd_rebuild_row_count'`

- [ ] **Step 3: 实现辅助函数**

在 `backend/etl/build_dwd.py` 中 `rebuild_all_dwd` 函数定义之后添加：

```python
def _dwd_rebuild_row_count(result: dict) -> int:
    """Sum only numeric DWD rebuild counts, skipping ``changed_codes``.

    ``rebuild_dwd_incremental`` and ``rebuild_all_dwd`` both return a dict
    with integer row-count fields *and* a ``changed_codes`` list.  Callers
    that need a total row count should use this helper instead of
    ``sum(result.values())``, which raises ``TypeError``.
    """
    return sum(
        v for v in result.values() if isinstance(v, (int, float))
    )
```

- [ ] **Step 4: 运行测试确认通过**

```bash
pytest tests/test_etl/test_build_dwd.py::test_dwd_rebuild_row_count_ignores_changed_codes -v
```

Expected: PASS

- [ ] **Step 5: 确保现有测试不受影响**

```bash
pytest tests/test_etl/test_build_dwd.py -v
```

Expected: ALL PASS

- [ ] **Step 6: Commit**

```bash
git add backend/etl/build_dwd.py tests/test_etl/test_build_dwd.py
git commit -m "fix(build_dwd): add _dwd_rebuild_row_count helper to skip changed_codes in sum

The result dict of rebuild_dwd_incremental and rebuild_all_dwd now contains
'changed_codes' (list) alongside numeric row-count fields.  sum() over all
values crashes with TypeError when changed_codes is present.  The new helper
sums only int/float values.

Related: #7103f81

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: 修复 `cli.py` 的 `sum()` 调用

**Files:**
- Modify: `backend/cli.py:437`

- [ ] **Step 1: 修改 `_cmd_run_single_day`**

将 `backend/cli.py:437`：

```python
            rebuild_rows = sum(dwd_result.values()) if dwd_result else 0
```

改为：

```python
            from backend.etl.build_dwd import _dwd_rebuild_row_count

            rebuild_rows = _dwd_rebuild_row_count(dwd_result) if dwd_result else 0
```

- [ ] **Step 2: 运行相关 CLI 测试**

```bash
pytest tests/test_cli.py -v -k "cmd_run"
```

Expected: PASS（现有测试 mock 了 `rebuild_dwd_incremental` 和 `log_etl_end`，不应受影响）

- [ ] **Step 3: Commit**

```bash
git add backend/cli.py
git commit -m "fix(cli): use _dwd_rebuild_row_count to avoid sum() TypeError

The sum(dwd_result.values()) call at _cmd_run_single_day:437 crashed when
commit 7103f81 added 'changed_codes' (list) to the result dict.  Switch to
_dwd_rebuild_row_count which sums only int/float fields.

Fixes: d14a5a35 crash

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: 修复 `refresh_pipeline.py` 的 `sum()` 调用

**Files:**
- Modify: `backend/etl/refresh_pipeline.py:258`

- [ ] **Step 1: 修改 `run_refresh_pipeline` 中的 `sum()` 调用**

将 `backend/etl/refresh_pipeline.py:258`：

```python
        rebuild_rows = sum(dwd_result.values()) if dwd_result else 0
```

改为：

```python
        from backend.etl.build_dwd import _dwd_rebuild_row_count

        rebuild_rows = _dwd_rebuild_row_count(dwd_result) if dwd_result else 0
```

- [ ] **Step 2: 运行 refresh pipeline 测试**

```bash
pytest tests/ -v -k "refresh"
```

Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add backend/etl/refresh_pipeline.py
git commit -m "fix(refresh_pipeline): use _dwd_rebuild_row_count to avoid sum() TypeError

Same pattern as cli.py — commit 7103f81 added 'changed_codes' list to the
DWD result dict, which causes sum(dwd_result.values()) to raise TypeError.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 4: 修复 `orchestrator.py` 的 `result.items()` 遍历

**Files:**
- Modify: `backend/etl/orchestrator.py:171-173`

- [ ] **Step 1: 读取现有代码确认上下文**

`orchestrator.py:170-173` 当前代码：

```python
                result = rebuild_all_dwd(con, codes)
                for name, n in result.items():
                    log_etl_end(con, lid, f"build_dwd_{name}", t0, "success", row_count=n)
```

问题：`result.items()` 会遍历到 `("changed_codes", [...])`，导致 `row_count=[...]` 传入 `log_etl_end`，DuckDB 参数绑定可能失败或写入异常。

- [ ] **Step 2: 修改遍历逻辑**

```python
                result = rebuild_all_dwd(con, codes)
                for name, n in result.items():
                    if name == "changed_codes":
                        continue
                    log_etl_end(con, lid, f"build_dwd_{name}", t0, "success", row_count=n)
```

- [ ] **Step 3: 运行 orchestrator 测试**

```bash
pytest tests/test_etl/test_orchestrator.py -v
```

Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add backend/etl/orchestrator.py
git commit -m "fix(orchestrator): skip changed_codes when iterating DWD result items

rebuild_all_dwd now returns 'changed_codes' (list) alongside numeric
row-count fields.  Iterating result.items() for log_etl_end would pass
a list as row_count to DuckDB.  Skip the key explicitly.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 5: 更新 CLAUDE.md

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: 在 "已知问题和注意事项" 添加一条**

在 `CLAUDE.md` 的 "已知问题和注意事项" 段落末尾添加：

```markdown
- **DWD rebuild 结果字典：** `rebuild_dwd_incremental` / `rebuild_all_dwd` 返回的 dict
  含 `changed_codes`（list）键。需要用 `_dwd_rebuild_row_count()` 而非裸
  `sum(result.values())` 计算总行数（后者会 TypeError）。该 helper 定义于
  `backend/etl/build_dwd.py`。
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(claude): document _dwd_rebuild_row_count usage for DWD result dict

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 6: 全量回归测试

**Files:**
- (无变更，仅验证)

- [ ] **Step 1: 运行全量测试**

```bash
pytest tests/ -v
```

Expected: ALL PASS

- [ ] **Step 2: 确认无遗漏的 `sum(*.values())` 模式**

```bash
grep -rn "sum(.*result.*values()\|sum(.*dwd.*values()" backend/
```

Expected: 无匹配（所有旧 `sum(result.values())` 已替换为 `_dwd_rebuild_row_count`）
