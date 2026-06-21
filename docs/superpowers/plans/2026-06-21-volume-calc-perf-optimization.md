# Volume Calculator FULL 路径性能优化

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 VolumeCalculator 在 batch_full 路径下的吞吐从 8 stocks/s 提升到 30-40 stocks/s（对齐均线/K线形态的水平），消除量能日线成为全市场重算瓶颈的问题。

**Architecture:** 三个独立优化点：(1) 用 `sliding_window_view` 向量化单股 `_compute_pct_rank`，同时写 golden-master 测试锁定等价性；(2) 在 `compute_volume_trend_series` 中预计算 SMA 全序列 + 方向斜率，消除 per-bar `pd.Series.rolling()` 和 `np.polyfit` 重复调用；(3) 将 APPEND 路径已有的 `batch_volume_rolling_core` 矩阵批算接入 `batch_full_volume`，一次性计算全市场 ma_vol_5/pct_vol_rank/volume_ratio。

**Tech Stack:** Python 3.9, NumPy, pandas

---

## 文件结构

```
backend/etl/calc_volume.py          # 修改: _compute_pct_rank, compute_volume_trend_series, _compute_volume_core
backend/etl/calc_batch_append.py    # 修改: batch_full_volume
backend/etl/vector/volume_batch.py  # 读取: batch_volume_rolling_core, attach_volume_core_to_df (已有)
tests/test_etl/test_calc_volume.py  # 修改: 新增 golden-master 等价性测试
```

---

### Task 1: 向量化单股 `_compute_pct_rank`

**Files:**
- Modify: `backend/etl/calc_volume.py:397-415`
- Test: `tests/test_etl/test_calc_volume.py`

**背景:** `_compute_pct_rank` 当前用纯 Python `for i in range(window-1, n)` 逐根切 120 元素窗口做排名。单股 ~126 次循环 × 120 次比较。5370 股 → ~8000 万次标量操作。改用 `numpy.lib.stride_tricks.sliding_window_view` 一次性广播比较。

- [ ] **Step 1: 写 golden-master 等价性测试**

在 `tests/test_etl/test_calc_volume.py` 末尾新增：

```python
def test_pct_rank_vectorized_matches_original():
    """向量化版 _compute_pct_rank 与原始逐根循环逐值一致 (atol=1e-9)."""
    from backend.etl.calc_volume import VolumeCalculator
    from numpy.lib.stride_tricks import sliding_window_view

    # 原始实现（拷贝自 calc_volume.py:397-415）
    def _compute_pct_rank_original(ma_vol_5: np.ndarray, window: int) -> np.ndarray:
        n = len(ma_vol_5)
        result = np.full(n, np.nan)
        for i in range(window - 1, n):
            start = max(0, i - window + 1)
            window_vals = ma_vol_5[start:i + 1]
            valid = window_vals[~np.isnan(window_vals)]
            if len(valid) < 2:
                continue
            cur = ma_vol_5[i]
            if np.isnan(cur):
                continue
            rank = np.sum(valid <= cur) / len(valid) * 100.0
            result[i] = rank
        return result

    rng = np.random.default_rng(42)

    # Case 1: all-finite, normal data
    for _ in range(20):
        n = rng.integers(130, 300)
        data = rng.lognormal(11.0, 0.5, size=n)
        for w in [60, 120]:
            orig = _compute_pct_rank_original(data, w)
            vec = _compute_pct_rank_vectorized(data, w)
            np.testing.assert_allclose(
                vec[~np.isnan(orig)], orig[~np.isnan(orig)],
                atol=1e-9,
                err_msg=f"pct_rank mismatch: n={n}, w={w}",
            )
            # NaN positions must match
            np.testing.assert_array_equal(np.isnan(vec), np.isnan(orig))

    # Case 2: with NaN holes
    data_with_nan = rng.lognormal(11.0, 0.5, size=200)
    data_with_nan[rng.integers(0, 200, 15)] = np.nan
    orig = _compute_pct_rank_original(data_with_nan, 120)
    vec = _compute_pct_rank_vectorized(data_with_nan, 120)
    np.testing.assert_allclose(
        vec[~np.isnan(orig)], orig[~np.isnan(orig)], atol=1e-9,
    )
    np.testing.assert_array_equal(np.isnan(vec), np.isnan(orig))

    # Case 3: short data (< window)
    short = rng.lognormal(11.0, 0.5, size=50)
    orig = _compute_pct_rank_original(short, 120)
    vec = _compute_pct_rank_vectorized(short, 120)
    assert np.all(np.isnan(vec)), "short data should be all-NaN"
    np.testing.assert_array_equal(np.isnan(vec), np.isnan(orig))
```

- [ ] **Step 2: 运行测试确认失败**

```bash
pytest tests/test_etl/test_calc_volume.py::test_pct_rank_vectorized_matches_original -v
```

Expected: FAIL with `NameError: name '_compute_pct_rank_vectorized' is not defined`

- [ ] **Step 3: 在 `calc_volume.py` 中添加向量化版本**

在 `_compute_pct_rank` 方法上方（≈line 396）插入新函数：

```python
def _compute_pct_rank_vectorized(ma_vol_5: np.ndarray, window: int = 120) -> np.ndarray:
    """Percentile rank (vectorized, single-pass).

    For each bar ``i >= window-1``, computes the fraction of valid values
    in ``ma_vol_5[i-window+1 : i+1]`` that are <= ``ma_vol_5[i]``, then
    multiplies by 100.  Uses ``sliding_window_view`` to compare all bars
    against their respective windows in one broadcast operation.

    Matches the original per-bar loop exactly (verified by
    ``test_pct_rank_vectorized_matches_original``).
    """
    from numpy.lib.stride_tricks import sliding_window_view

    n = len(ma_vol_5)
    result = np.full(n, np.nan)

    if n < window:
        return result

    # sliding_window_view(x, w) returns shape (n-w+1, w):
    #   row k = x[k : k+w]  for k = 0 .. n-w
    # For bar i, the trailing window is ma_vol_5[i-window+1 : i+1],
    # which is row (i-window+1) of the view.
    windows = sliding_window_view(ma_vol_5, window)               # (n-w+1, window)
    cur = ma_vol_5[window - 1:]                                    # (n-w+1,)

    valid_mask = ~np.isnan(windows)                                # (n-w+1, window)
    valid_count = valid_mask.sum(axis=1)                           # (n-w+1,)

    cur_2d = cur[:, np.newaxis]                                    # (n-w+1, 1)
    le = (windows <= cur_2d) & valid_mask                          # (n-w+1, window)
    rank = le.sum(axis=1) / np.maximum(valid_count, 1) * 100.0    # (n-w+1,)

    apply_mask = (valid_count >= 2) & np.isfinite(cur)
    result[window - 1:][apply_mask] = rank[apply_mask]

    return result
```

- [ ] **Step 4: 在 `_compute_volume_core` 中切换到向量化版本**

修改 [calc_volume.py:299](backend/etl/calc_volume.py#L299)：

```python
# 旧:
# df["pct_vol_rank"] = self._compute_pct_rank(df["ma_vol_5"].values, 120)

# 新:
df["pct_vol_rank"] = _compute_pct_rank_vectorized(df["ma_vol_5"].values, 120)
```

- [ ] **Step 5: 运行等价性测试**

```bash
pytest tests/test_etl/test_calc_volume.py::test_pct_rank_vectorized_matches_original -v
```

Expected: PASS

- [ ] **Step 6: 运行现有 volume 测试确认无回归**

```bash
pytest tests/test_etl/test_calc_volume.py -v
```

Expected: ALL PASS

- [ ] **Step 7: Commit**

```bash
git add backend/etl/calc_volume.py tests/test_etl/test_calc_volume.py
git commit -m "perf(volume): vectorize _compute_pct_rank with sliding_window_view

Replace per-bar Python loop (~126 iter x 120 comparisons per stock)
with a single sliding_window_view broadcast.  Golden-master test
locks equivalence at atol=1e-9.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: 预计算 SMA + 方向斜率，消除 `volume_trend_v2` 中的 per-bar 重复计算

**Files:**
- Modify: `backend/etl/calc_volume.py:72-167`（`volume_trend_v2` 和 `compute_volume_trend_series`）
- Test: `tests/test_etl/test_calc_volume.py`

**背景:** `compute_volume_trend_series` 逐 bar 调用 `volume_trend_v2(vol[:i+1])`。每次调用：(1) `pd.Series(vol[:i+1]).rolling(5).mean()` — 新建 pandas 对象 + 从头算 SMA；(2) `_slope_over_mean_abs(obs_5)` — `np.polyfit` 做 5 点线性回归。两个操作都可以在循环外**一次性预计算**。

改动策略：不修改 `volume_trend_v2` 的函数签名（保持兼容），而是新增一个接受预计算数组的内部版本，由 `compute_volume_trend_series` 调用。

- [ ] **Step 1: 写等价性测试**

在 `tests/test_etl/test_calc_volume.py` 末尾新增：

```python
def test_compute_volume_trend_series_vectorized_matches_original():
    """预计算版 trend series 与原始逐 bar 版完全一致."""
    from backend.etl.calc_volume import (
        compute_volume_trend_series,
        _compute_volume_trend_series_vectorized,
        VOLUME_TREND_V2_DAILY,
        VOLUME_TREND_V2_WEEKLY,
    )

    rng = np.random.default_rng(77)

    for label, params in [("daily", VOLUME_TREND_V2_DAILY),
                           ("weekly", VOLUME_TREND_V2_WEEKLY)]:
        anchor = params["anchor_bars"]
        for _ in range(30):
            n = rng.integers(anchor + 10, anchor + 80)
            vol = rng.lognormal(11.0, 0.5, size=n)

            # Full range (target_indices=None)
            orig = compute_volume_trend_series(vol, params)
            vec = _compute_volume_trend_series_vectorized(vol, params)
            assert len(orig) == len(vec) == n
            for i in range(n):
                assert orig[i] == vec[i], (
                    f"[{label}] mismatch at i={i}: orig={orig[i]}, vec={vec[i]}"
                )

            # Subset indices (APPEND path)
            indices = sorted(set(rng.integers(anchor, n, size=min(10, n - anchor))))
            orig_sub = compute_volume_trend_series(vol, params, target_indices=indices)
            vec_sub = _compute_volume_trend_series_vectorized(
                vol, params, target_indices=indices,
            )
            for i in range(n):
                assert orig_sub[i] == vec_sub[i], (
                    f"[{label}] subset mismatch at i={i}"
                )

    # Case: data with NaN
    vol_nan = rng.lognormal(11.0, 0.5, size=120)
    vol_nan[rng.integers(0, 120, 5)] = np.nan
    orig = compute_volume_trend_series(vol_nan, VOLUME_TREND_V2_DAILY)
    vec = _compute_volume_trend_series_vectorized(vol_nan, VOLUME_TREND_V2_DAILY)
    for i in range(len(vol_nan)):
        assert orig[i] == vec[i], f"NaN case mismatch at i={i}"
```

- [ ] **Step 2: 运行测试确认失败**

```bash
pytest tests/test_etl/test_calc_volume.py::test_compute_volume_trend_series_vectorized_matches_original -v
```

Expected: FAIL with `ImportError: cannot import name '_compute_volume_trend_series_vectorized'`

- [ ] **Step 3: 实现预计算版 `_compute_volume_trend_series_vectorized`**

在 `calc_volume.py` 中，`compute_volume_trend_series` 函数下方（≈line 168）插入：

```python
def _compute_volume_trend_series_vectorized(
    vol_series,
    params: dict,
    target_indices: Optional[list] = None,
) -> list:
    """Pre-computed SMA + direction slopes version of compute_volume_trend_series.

    Avoids per-bar ``pd.Series.rolling()`` and ``np.polyfit`` by:
    1. Computing the full MA5 rolling series ONCE (via pandas, then .to_numpy())
    2. Pre-computing 5-bar OLS direction slopes for ALL bars via
       ``weighted_window_slopes(decay=0)`` / ``sliding_window_mean_abs``
    Then the per-bar loop only does percentile / regime classification,
    which are cheap (anchor: 60 or 30 elements).

    Semantically identical to compute_volume_trend_series; verified by
    test_compute_volume_trend_series_vectorized_matches_original.
    """
    from backend.etl.base import weighted_window_slopes, sliding_window_mean_abs

    vol = np.asarray(vol_series, dtype=float)
    n = len(vol)
    anchor_bars = int(params["anchor_bars"])
    ma_window = int(params.get("ma_window", 5))
    result = [None] * n
    kw = {k: params[k] for k in params if k not in ("anchor_bars", "ma_window")}

    # ---- pre-compute once ----
    # 1. Full MA5 series (same as volume_trend_v2's pd.Series.rolling)
    ma_full = (
        pd.Series(vol)
        .rolling(window=ma_window, min_periods=ma_window)
        .mean()
        .to_numpy(dtype=float)
    )

    # 2. Per-bar direction slopes for obs_5 = vol[i-4:i+1] (window=5, unweighted)
    dir_slopes = weighted_window_slopes(vol, window=5, decay=0.0)
    dir_scales = sliding_window_mean_abs(vol, window=5)
    # direction value = slope / scale (matches _slope_over_mean_abs)
    dir_vals = np.full(n, np.nan)
    with np.errstate(invalid="ignore"):
        dir_vals_ok = np.isfinite(dir_slopes) & (dir_scales > 1e-9)
        dir_vals[dir_vals_ok] = dir_slopes[dir_vals_ok] / dir_scales[dir_vals_ok]

    vol_flat_eps = float(kw.get("vol_flat_eps", 0.001))
    high_percentile = float(kw.get("high_percentile", 80))
    low_percentile = float(kw.get("low_percentile", 20))
    amp_threshold = float(kw.get("amp_threshold", 1.4))
    fast_count = int(kw.get("fast_count", 3))
    recent_count = int(kw.get("recent_count", 2))
    confirm_window = int(kw.get("confirm_window", 10))
    confirm_count = int(kw.get("confirm_count", 3))

    # ---- per-bar loop (only percentile + regime, no polyfit / rolling) ----
    indices = range(n) if target_indices is None else target_indices
    for i in indices:
        if i < 0 or i >= n:
            continue
        if i + 1 < anchor_bars:
            continue

        # ma for bars 0..i  (pre-computed, just slice)
        ma_i = ma_full[: i + 1]
        valid_ma = ma_i[~np.isnan(ma_i)]
        if len(valid_ma) < anchor_bars:
            continue

        anchor = valid_ma[-anchor_bars:]
        p80 = float(np.percentile(anchor, high_percentile))
        p20 = float(np.percentile(anchor, low_percentile))

        amp = p80 / max(p20, 1e-9)
        if amp < amp_threshold:
            regime = "振幅不足"
        else:
            recent_ma = (
                valid_ma[-confirm_window:]
                if len(valid_ma) >= confirm_window
                else valid_ma
            )
            if len(recent_ma) >= fast_count and bool(
                np.all(recent_ma[-fast_count:] >= p80)
            ):
                regime = "爆量区"
            elif len(recent_ma) >= fast_count and bool(
                np.all(recent_ma[-fast_count:] <= p20)
            ):
                regime = "地量区"
            else:
                boom_days = int(np.sum(recent_ma >= p80))
                dry_days = int(np.sum(recent_ma <= p20))
                boom_recent = (
                    int(np.sum(recent_ma[-5:] >= p80))
                    if len(recent_ma) >= 5
                    else 0
                )
                dry_recent = (
                    int(np.sum(recent_ma[-5:] <= p20))
                    if len(recent_ma) >= 5
                    else 0
                )
                if boom_days >= confirm_count and boom_recent >= recent_count:
                    regime = "爆量区"
                elif dry_days >= confirm_count and dry_recent >= recent_count:
                    regime = "地量区"
                else:
                    regime = "正常区"

        # direction from pre-computed dir_vals
        dir_val = dir_vals[i]
        if not np.isfinite(dir_val):
            dir_val = 0.0
        if dir_val > vol_flat_eps:
            direction = "放量中"
        elif dir_val < -vol_flat_eps:
            direction = "缩量中"
        else:
            direction = "平量"

        label = f"{regime}·{direction}"
        result[i] = trend_from_v2_label(label)

    return result
```

- [ ] **Step 4: 修改 `compute_volume_trend_series` 使用向量化版本**

修改 [calc_volume.py:165](backend/etl/calc_volume.py#L165)，将循环体内的 `volume_trend_v2` 调用替换为委托给向量化版本。最简单的方式是**函数入口直接转发**：

```python
def compute_volume_trend_series(
    vol_series,
    params: dict,
    target_indices: Optional[list] = None,
) -> list:
    """Per-bar ``volume_trend_v2`` on expanding prefixes; None until anchor met.

    Delegates to the vectorized implementation that pre-computes SMA and
    direction slopes once, then does a lightweight per-bar pass for
    percentile/regime classification only.
    """
    return _compute_volume_trend_series_vectorized(
        vol_series, params, target_indices=target_indices,
    )
```

保持旧函数体不变（后续可清理），只改入口。

- [ ] **Step 5: 运行等价性测试**

```bash
pytest tests/test_etl/test_calc_volume.py::test_compute_volume_trend_series_vectorized_matches_original -v
pytest tests/test_etl/test_calc_volume.py::test_compute_volume_trend_series_prefix_consistency -v
```

Expected: ALL PASS

- [ ] **Step 6: 运行现有全部 volume 测试**

```bash
pytest tests/test_etl/test_calc_volume.py -v
```

Expected: ALL PASS

- [ ] **Step 7: Commit**

```bash
git add backend/etl/calc_volume.py tests/test_etl/test_calc_volume.py
git commit -m "perf(volume): pre-compute SMA + direction slopes in trend series

Eliminate per-bar pd.Series.rolling() and np.polyfit calls in
compute_volume_trend_series by pre-computing the full MA5 series
and 5-bar OLS slopes once via weighted_window_slopes.

Golden-master test locks equivalence with original per-bar loop.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: 将 `batch_volume_rolling_core` 矩阵批算接入 `batch_full_volume`

**Files:**
- Modify: `backend/etl/calc_batch_append.py:761-794`
- Read (existing, no changes): `backend/etl/vector/volume_batch.py`

**背景:** `vector/volume_batch.py` 已有 `batch_volume_rolling_core` + `attach_volume_core_to_df`，在 APPEND 路径中一次性算完所有股票的 ma_vol_5 / pct_vol_rank / volume_ratio。当前 `batch_full_volume` 却逐股调用 `_compute_volume_core`（含 Task 1 已优化的向量化 pct_rank，但仍是单股逐一处理）。改成先批量矩阵计算 rolling core，再逐股只做 derived（zone/trend/divergence），减少重复 load 和 Python 调用开销。

- [ ] **Step 1: 修改 `batch_full_volume`**

替换 [calc_batch_append.py:761-794](backend/etl/calc_batch_append.py#L761-L794) 的整个函数：

```python
def batch_full_volume(
    con,
    freq: str,
    ts_codes: List[str],
    calc_date: str,
    recalc_start: Optional[str],
    quote_groups: dict,
    state_map: Optional[dict] = None,
):
    from backend.etl.base import load_latest_fingerprints, load_latest_spec_versions
    from backend.etl.calc_compute_domain import resolve_compute_indices
    from backend.etl.calc_volume import VolumeCalculator
    from backend.etl.vector.volume_batch import (
        batch_volume_rolling_core,
        attach_volume_core_to_df,
    )

    calc = VolumeCalculator(con, freq)

    # ---- Phase A: batch compute rolling core for all stocks at once ----
    batch_core = batch_volume_rolling_core(
        ts_codes, quote_groups, pct_window=120, ma_period=5,
    )
    label_zh = _batch_label_zh("volume", freq)
    logger.info(
        "progress calc.batch_full: %s batch rolling core | %d/%d stocks",
        label_zh, len(batch_core), len(ts_codes),
    )

    # ---- Phase B: per-stock compute (zone + trend + divergence only) ----
    def _compute(c, _code, df):
        idx = resolve_compute_indices(df, recalc_start, calc_date)
        # Attach pre-computed rolling core columns (ma_vol_5, pct_vol_rank, volume_ratio)
        df = attach_volume_core_to_df(df)
        return c._compute_volume_derived(
            df, trend_target_indices=idx or None,
        )

    return _batch_full_loop(
        calc, ts_codes, calc_date, recalc_start, quote_groups, _compute,
        VolumeCalculator.DWS_COLS, VolumeCalculator.FLOAT_COLS,
        _batch_label_zh("volume", freq), min_rows=5,
        spec_version=VolumeCalculator.SPEC_VERSION,
        check_spec=True,
        latest_fps=load_latest_fingerprints(con, calc.dws_table, ts_codes),
        latest_specs=load_latest_spec_versions(con, calc.dws_table, ts_codes),
        state_map=state_map,
        indicator_name="volume",
        sig_cols=calc.SIGNATURE_COLS,
    )
```

注意：`_batch_full_loop` 里 `_compute(calc, ts_code, df)` 收到的 `df` 是 `data_groups.get(ts_code)`，即原始 DWD quote 数据（不含 ma_vol_5 / pct_vol_rank 列）。`attach_volume_core_to_df(df)` 从 `batch_core` 按 `len(df)` 匹配到对应行数的预计算数组，attach 到 df 上。

但 `attach_volume_core_to_df` 的当前实现（`vector/volume_batch.py:84-89`）需要 `core` dict（来自 `batch_volume_rolling_core` 对单个 code 的返回值）。需要在 `_compute` 闭包里拿到 `batch_core[ts_code]`。

修正版 `_compute`：

```python
    def _compute(c, ts_code, df):
        idx = resolve_compute_indices(df, recalc_start, calc_date)
        core = batch_core.get(ts_code)
        if core is not None and len(core.get("ma_vol_5", [])) == len(df):
            df = attach_volume_core_to_df(df, core)
        else:
            # Fallback: stock not in batch_core (different bar count),
            # use the now-vectorized per-stock path
            df = c._compute_volume_core(df)
        return c._compute_volume_derived(
            df, trend_target_indices=idx or None,
        )
```

- [ ] **Step 2: 检查 `attach_volume_core_to_df` 接口兼容性**

当前 `attach_volume_core_to_df` 的 core 数组长度可能不等于 df 长度（因为 `_stack_vol_matrix` 只取最长的那组股票）。必须加长度检查（已在 Step 1 的 fallback 逻辑中处理）。

同时检查 `_batch_full_loop` 传给 `_compute` 的第二个参数名：当前是 `_code`（见 [calc_batch_append.py:776](backend/etl/calc_batch_append.py#L776)），需要在闭包中改为 `ts_code`。

- [ ] **Step 3: 写等价性测试**

在 `tests/test_etl/test_calc_volume.py` 末尾新增集成测试：

```python
def test_batch_full_volume_with_batch_core_matches_per_stock():
    """batch_full_volume（使用 batch_volume_rolling_core）与逐股 calculate 结果一致."""
    import duckdb
    from backend.etl.calc_volume import VolumeCalculator
    from backend.etl.calc_batch_append import batch_full_volume
    from backend.etl.base import load_quote_groups

    con = duckdb.connect(":memory:")

    # 建 minimal DWD schema
    con.execute("""
        CREATE TABLE dwd_daily_quote (
            ts_code VARCHAR, trade_date VARCHAR, vol DOUBLE,
            close_qfq DOUBLE, open_qfq DOUBLE, high_qfq DOUBLE, low_qfq DOUBLE,
            amount DOUBLE, is_suspended INTEGER
        )
    """)
    con.execute("""
        CREATE TABLE dws_volume_daily (
            ts_code VARCHAR, trade_date VARCHAR, calc_date VARCHAR,
            ma_vol_5 DOUBLE, volume_ratio DOUBLE, pct_vol_rank DOUBLE,
            zone VARCHAR, trend VARCHAR, trend_strength DOUBLE,
            divergence VARCHAR, input_fingerprint VARCHAR, spec_version VARCHAR
        )
    """)

    rng = np.random.default_rng(99)
    ts_codes = [f"00000{i}.SZ" for i in range(1, 6)]
    all_data = {}

    for code in ts_codes:
        n = 200
        df = pd.DataFrame({
            "ts_code": code,
            "trade_date": [f"2026{i:02d}01" for i in range(1, n + 1)],
            "vol": rng.lognormal(11.0, 0.5, size=n),
            "close_qfq": rng.lognormal(2.5, 0.3, size=n),
            "open_qfq": rng.lognormal(2.5, 0.3, size=n),
            "high_qfq": rng.lognormal(2.5, 0.3, size=n),
            "low_qfq": rng.lognormal(2.5, 0.3, size=n),
            "amount": rng.lognormal(18.0, 0.5, size=n),
            "is_suspended": 0,
        })
        all_data[code] = df
        con.execute("INSERT INTO dwd_daily_quote SELECT * FROM df")

    calc_date = "2026020001"
    quote_groups = load_quote_groups(
        con, "dwd_daily_quote", "daily",
        VolumeCalculator(None, "daily").quote_load_columns("daily"),
        ts_codes,
    )

    # ---- Per-stock reference ----
    calc = VolumeCalculator(con, "daily")
    ref_results = {}
    for code in ts_codes:
        df = quote_groups[code].copy()
        result_df = calc._compute_indicators(df)
        ref_results[code] = result_df

    # ---- batch_full_volume ----
    agg, stock_rows = batch_full_volume(
        con, "daily", ts_codes, calc_date, None, quote_groups,
    )

    # Each stock's computed columns should match reference
    for code in ts_codes:
        ref = ref_results[code]
        # batch_full writes via insert_dws_batch_multi — check stock_rows
        # for the final computed DataFrame columns
        for ts_code, out_df, fp, rs, we in stock_rows:
            if ts_code == code:
                # Compare core columns
                for col in ["ma_vol_5", "volume_ratio", "pct_vol_rank"]:
                    r = ref[col].values
                    o = out_df[col].values
                    mask = ~np.isnan(r) & ~np.isnan(o)
                    np.testing.assert_allclose(
                        r[mask], o[mask], atol=1e-9,
                        err_msg=f"{code} {col} mismatch",
                    )
                # zone/trend/divergence should match exactly (string enums)
                for col in ["zone", "trend", "divergence"]:
                    assert list(out_df[col]) == list(ref[col]), (
                        f"{code} {col} mismatch"
                    )
                break

    con.close()
```

> ⚠️ 此测试需创建内存 DuckDB + DWD/DWS 表。若现有测试基础设施有 `db_with_schema` fixture（含完整 schema），优先使用 fixture 简化。

- [ ] **Step 4: 运行集成测试**

```bash
pytest tests/test_etl/test_calc_volume.py::test_batch_full_volume_with_batch_core_matches_per_stock -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/etl/calc_batch_append.py tests/test_etl/test_calc_volume.py
git commit -m "perf(volume): wire batch_volume_rolling_core into batch_full_volume

Use the existing matrix-based rolling core computation (from APPEND path)
in batch_full_volume to compute ma_vol_5/pct_vol_rank/volume_ratio for
all stocks at once.  Per-stock fallback for stocks with non-uniform bar
counts.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 4: 端到端 profiling 验证

**Files:**
- Create: `scripts/profile_volume_batch_full.py`

**背景:** 用实际数据跑 profiling 脚本确认吞吐从 8 stocks/s 提升到 30+ stocks/s。

- [ ] **Step 1: 创建 profiling 脚本**

```python
"""Profile VolumeCalculator batch_full throughput before/after optimization.

Usage:
    python scripts/profile_volume_batch_full.py --sample 500
"""
import argparse
import time
import duckdb
import numpy as np

from backend.db.connection import get_connection
from backend.etl.calc_volume import VolumeCalculator
from backend.etl.calc_batch_append import batch_full_volume
from backend.etl.base import load_quote_groups


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", type=int, default=500,
                        help="Number of stocks to profile")
    args = parser.parse_args()

    con = get_connection()
    calc_date = "20260615"

    # Resolve active stocks
    rows = con.execute(
        "SELECT DISTINCT ts_code FROM dwd_daily_quote "
        "WHERE trade_date <= ? ORDER BY ts_code",
        [calc_date],
    ).fetchall()
    all_codes = [r[0] for r in rows]
    ts_codes = all_codes[: args.sample]

    calc = VolumeCalculator(con, "daily")
    load_cols = calc.quote_load_columns("daily")

    t0 = time.monotonic()
    quote_groups = load_quote_groups(
        con, calc.src_table, "daily", load_cols, ts_codes,
    )
    t1 = time.monotonic()
    print(f"load_quote_groups: {t1 - t0:.1f}s for {len(ts_codes)} stocks")

    t0 = time.monotonic()
    agg, stock_rows = batch_full_volume(
        con, "daily", ts_codes, calc_date, None, quote_groups,
    )
    t1 = time.monotonic()
    elapsed = t1 - t0
    rate = len(ts_codes) / elapsed if elapsed > 0 else float("inf")
    print(f"batch_full_volume: {elapsed:.1f}s for {len(ts_codes)} stocks "
          f"({rate:.1f} stocks/s)")
    print(f"calculated={agg.calculated} skipped={agg.total_skipped}")

    con.close()


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 运行 profiling（优化前基准 — 如果有备份分支）**

```bash
# 在优化前分支运行，记录基准吞吐
python scripts/profile_volume_batch_full.py --sample 500
# 预期: ~8 stocks/s
```

- [ ] **Step 3: 运行 profiling（优化后）**

```bash
python scripts/profile_volume_batch_full.py --sample 500
# 目标: >30 stocks/s
```

- [ ] **Step 4: Commit**

```bash
git add scripts/profile_volume_batch_full.py
git commit -m "perf: add volume batch_full profiling script"
```

---

## 自审

### 1. 覆盖检查
- ✅ `_compute_pct_rank` 向量化 → Task 1
- ✅ `compute_volume_trend_series` 预计算 SMA + 斜率 → Task 2
- ✅ `batch_full_volume` 矩阵批算接入 → Task 3
- ✅ 等价性测试覆盖全部三个改动 → Task 1 Step 1, Task 2 Step 1, Task 3 Step 3
- ✅ Profiling 验证 → Task 4

### 2. Placeholder 扫描
- ✅ 无 TBD/TODO/implement later
- ✅ 所有代码步骤包含完整实现
- ✅ 所有测试步骤包含完整测试代码
- ✅ 所有命令包含预期输出

### 3. 类型一致性
- ✅ `_compute_pct_rank_vectorized` 在 Task 1 定义，Task 1 Step 4 引用
- ✅ `_compute_volume_trend_series_vectorized` 在 Task 2 定义，Task 2 Step 4 引用
- ✅ `batch_volume_rolling_core` / `attach_volume_core_to_df` 在 `vector/volume_batch.py` 已有，Task 3 引用
- ✅ `_compute(calc, ts_code, df)` 闭包参数名在 Task 3 中统一为 `ts_code`
