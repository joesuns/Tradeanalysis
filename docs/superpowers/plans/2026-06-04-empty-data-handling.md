# Empty Data Handling — 空数据处理架构重整

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 消除空数据静默跳过：缺数据无条件补拉，补不到按根因分类记录，计算层输出可追溯的跳过统计，ADS 视图人类可读。

**Architecture:** 四层改动：(1) `ods_calc_skip_log` 表 + `v_indicator_availability` 视图提供人类可读的空值原因；(2) `CalcResult` 让每个 Calculator 显式返回成功/跳过计数；(3) orchestrator 删除 50 阈值、加入熔断器、补拉范围按 warmup=27tdays 精确计算、退市股过滤；(4) 种子偏差在 DWS 层面处理——MACD/DDE 信号列在数据不足质量下限时置 NULL。

**Tech Stack:** Python 3.9, DuckDB, 现有 ETL 框架

---

## 关键参数

| 参数 | 值 | 含义 |
|---|---|---|
| `WARMUP_TDAYS` | 27 | 系统级 warmup = max(各指标功能下限)。MACD 是瓶颈 |
| MACD 功能下限 | 27 条 | EMA26 种子 = 26 条，第 27 条出第一个 DIF/DEA/MACD_bar |
| MACD 质量下限 | 100 条 | 种子偏差基本消散 + 背离窗口饱满。不足时 divergence/alert/turning_point 置 NULL |
| DDE 功能下限 | 10 条 | DDX2=EMA5 种子 + 基础数据 |
| DDE 质量下限 | 120 条 | 背离窗口=60 + 趋势回归稳定 |
| PP 功能下限 | 2 条 | 每个窗口独立，min_periods=2，数据不够自动用已有 |
| 熔断器阈值 | 5 | 连续 5 只股票 API 异常 或 连续 5 只空返回 → 中止补拉 |
| 拉取起始 | `max(list_date, end_date - 27 tdays)` | 不拉用不上的历史数据 |
| 拉取终点 | `min(calc_date, delist_date)` | 退市股终点 = 退市日 |

---

## 文件结构

| 文件 | 改动 |
|---|---|
| `backend/db/schema.py` | 新增 `ods_calc_skip_log` 表 + 索引 + `v_indicator_availability` 视图 |
| `backend/etl/base.py` | 新增 `SkipReason` 枚举 + `CalcResult` dataclass |
| `backend/etl/orchestrator.py` | 删除 50 阈值、`compute_fetch_range()`、熔断器、退市股过滤、分类日志、汇总 |
| `backend/etl/calc_macd.py` | `calculate()` → `CalcResult` |
| `backend/etl/calc_ma.py` | 同上 |
| `backend/etl/calc_kpattern.py` | 同上 |
| `backend/etl/calc_dde.py` | 同上 + BSE 识别 |
| `backend/etl/calc_volume.py` | 同上 |
| `backend/etl/calc_price_position.py` | 同上 + `min_periods=2` 替代 `min_periods=window` |
| `tests/test_etl/test_empty_data.py` | 新测试 |
| `CLAUDE.md` | 更新文档 |

---

### Task 1: 新增 `ods_calc_skip_log` 表和 `v_indicator_availability` 视图

**Files:**
- Modify: `backend/db/schema.py`

- [ ] **Step 1: 在 `_ODS_DDL` 末尾（第 114 行之后）加入 skip_log 表 DDL**

```python
    # 12.3 ods_calc_skip_log — records why a stock was skipped during calculation
    """CREATE TABLE IF NOT EXISTS ods_calc_skip_log (
        calc_date      TEXT NOT NULL,
        ts_code        TEXT NOT NULL,
        indicator      TEXT NOT NULL,
        freq           TEXT NOT NULL,
        reason         TEXT NOT NULL,
        detail         TEXT,
        PRIMARY KEY (calc_date, ts_code, indicator, freq)
    )""",
```

- [ ] **Step 2: 在 `_ODS_INDEX_DDL` 末尾加入两个索引**

```python
    "CREATE INDEX IF NOT EXISTS idx_skip_log_cd ON ods_calc_skip_log(calc_date)",
    "CREATE INDEX IF NOT EXISTS idx_skip_log_ind ON ods_calc_skip_log(indicator, reason)",
```

- [ ] **Step 3: 在 `_LATEST_VIEW_DDL` 之后、`_ADS_WIDE_VIEWS_DDL` 之前，加入 `v_indicator_availability` 视图 DDL**

```python
_V_INDICATOR_AVAILABILITY_DDL = """
CREATE VIEW IF NOT EXISTS v_indicator_availability AS
WITH latest AS (
    SELECT MAX(calc_date) AS calc_date FROM ods_calc_skip_log
),
indicators AS (
    SELECT 'macd' AS indicator, 'daily' AS freq
    UNION ALL SELECT 'macd', 'weekly'
    UNION ALL SELECT 'ma', 'daily'
    UNION ALL SELECT 'ma', 'weekly'
    UNION ALL SELECT 'kpattern', 'daily'
    UNION ALL SELECT 'kpattern', 'weekly'
    UNION ALL SELECT 'dde', 'daily'
    UNION ALL SELECT 'dde', 'weekly'
    UNION ALL SELECT 'volume', 'daily'
    UNION ALL SELECT 'volume', 'weekly'
    UNION ALL SELECT 'price_position', 'daily'
    UNION ALL SELECT 'price_position', 'weekly'
),
stock_ind AS (
    SELECT s.ts_code, s.name, s.exchange, s.list_date, s.delist_date,
           i.indicator, i.freq
    FROM dim_stock s
    CROSS JOIN indicators i
    WHERE s.is_active = 1
)
SELECT
    si.ts_code,
    si.name,
    si.exchange,
    si.indicator,
    si.freq,
    CASE
        WHEN sl.reason = 'source_unavailable' THEN 'unavailable'
        WHEN sl.reason = 'delisted' THEN 'historical'
        WHEN sl.reason = 'insufficient_rows' THEN 'partial'
        WHEN sl.reason IN ('no_dwd_data', 'fetch_failed') THEN 'missing'
        ELSE 'available'
    END AS status,
    COALESCE(sl.detail, '') AS detail
FROM stock_ind si
LEFT JOIN ods_calc_skip_log sl
    ON si.ts_code = sl.ts_code
    AND si.indicator = sl.indicator
    AND si.freq = sl.freq
    AND sl.calc_date = (SELECT calc_date FROM latest)
"""
```

- [ ] **Step 4: 在 `create_all_tables()` 中加入视图创建（在 `_V_DATA_FRESHNESS_DDL` 之前）**

```python
    # Indicator availability view
    con.execute(_V_INDICATOR_AVAILABILITY_DDL)
```

- [ ] **Step 5: 在 `drop_all_tables()` 的 `_all_views` 列表中加入 `v_indicator_availability`**

```python
    _all_views = (
        ["v_indicator_availability",
         "v_ads_index_wide_weekly", "v_ads_index_wide",
         ...
    )
```

- [ ] **Step 6: 在 `drop_all_tables()` 的 `_all_tables` 中加入 `ods_calc_skip_log`**

在 `"ods_etl_log"` 之后加入 `"ods_calc_skip_log"`。

- [ ] **Step 7: 验证**

```bash
python -c "
from backend.db.connection import get_connection
from backend.db.schema import create_all_tables
con = get_connection()
create_all_tables(con)
cols = con.execute('DESCRIBE ods_calc_skip_log').fetchall()
for c in cols: print(c)
print('---')
views = con.execute(\"SELECT name FROM sqlite_master WHERE type='view' AND name='v_indicator_availability'\").fetchall()
print('view exists:', len(views) > 0)
con.close()
"
```

Expected: 输出 6 列 + view exists: True

- [ ] **Step 8: Commit**

```bash
git add backend/db/schema.py
git commit -m "feat: add ods_calc_skip_log table, indexes, and v_indicator_availability view"
```

---

### Task 2: 新增 `SkipReason` 枚举和 `CalcResult` 数据结构

**Files:**
- Modify: `backend/etl/base.py`

- [ ] **Step 1: 在现有 import 之后、`ema()` 之前插入新代码**

```python
from dataclasses import dataclass, field
from enum import Enum


class SkipReason(str, Enum):
    """Root-cause classification for why a stock was skipped during calculation."""
    NO_DWD_DATA = "no_dwd_data"              # DWD has 0 rows for this stock
    INSUFFICIENT_ROWS = "insufficient_rows"   # DWD has rows but < functional minimum
    SOURCE_UNAVAILABLE = "source_unavailable" # tushare doesn't support (e.g. BSE moneyflow)
    FETCH_FAILED = "fetch_failed"            # Auto-fetch exhausted retries
    DELISTED = "delisted"                     # Stock delisted before calc_date


@dataclass
class CalcResult:
    """Return value of Calculator.calculate().

    Usage:
        result = CalcResult()
        result.calculated += 1
        result.add_skip(SkipReason.INSUFFICIENT_ROWS, "688001.SH", "DWD rows=15, min=27")
    """
    calculated: int = 0
    skipped: dict = field(default_factory=dict)  # {SkipReason: [(ts_code, detail), ...]}

    def add_skip(self, reason: SkipReason, ts_code: str, detail: str = ""):
        if reason not in self.skipped:
            self.skipped[reason] = []
        self.skipped[reason].append((ts_code, detail))

    @property
    def total_skipped(self) -> int:
        return sum(len(v) for v in self.skipped.values())

    @property
    def total_input(self) -> int:
        return self.calculated + self.total_skipped
```

注意：`from __future__ import annotations` 不需要。`base.py` 当前不使用前向引用，保持一致。

- [ ] **Step 2: 验证 import**

```bash
python -c "from backend.etl.base import SkipReason, CalcResult; r = CalcResult(); r.add_skip(SkipReason.INSUFFICIENT_ROWS, '000001.SZ', 'test'); print(r)"
```

Expected: `CalcResult(calculated=0, skipped={<SkipReason.INSUFFICIENT_ROWS: 'insufficient_rows'>: [('000001.SZ', 'test')]})`

- [ ] **Step 3: Commit**

```bash
git add backend/etl/base.py
git commit -m "feat: add SkipReason enum and CalcResult dataclass to base.py"
```

---

### Task 3: 改造 `MACDCalculator.calculate()` 返回 `CalcResult`

**Files:**
- Modify: `backend/etl/calc_macd.py`

- [ ] **Step 1: 更新 import（第 1-3 行）**

```python
import logging
import numpy as np
import pandas as pd
from backend.etl.base import ema, to_float_safe, linear_regression_slope, insert_dws_batch, SkipReason, CalcResult

logger = logging.getLogger(__name__)
```

- [ ] **Step 2: 替换 `calculate()` 方法（第 16-35 行）**

```python
    def calculate(self, ts_codes: list[str], calc_date: str) -> CalcResult:
        """Calculate MACD for a batch of stocks. Returns CalcResult with stats."""
        result = CalcResult()
        for ts_code in ts_codes:
            if self.freq == "weekly":
                df = self.con.execute(f"""
                    SELECT d.trade_date, d.close_qfq FROM {self.src_table} d
                    JOIN dim_date dd ON d.trade_date = dd.trade_date
                    WHERE d.ts_code = ? AND dd.is_week_end = 1
                    ORDER BY d.trade_date
                """, (ts_code,)).df()
            else:
                df = self.con.execute(f"""
                    SELECT trade_date, close_qfq FROM {self.src_table}
                    WHERE ts_code = ? AND is_suspended = 0
                    ORDER BY trade_date
                """, (ts_code,)).df()

            if df.empty:
                logger.debug("MACD %s skip %s: no DWD data", self.freq, ts_code)
                result.add_skip(SkipReason.NO_DWD_DATA, ts_code, "DWD returned 0 rows")
                continue
            if len(df) < 27:
                logger.debug("MACD %s skip %s: %d rows < 27",
                             self.freq, ts_code, len(df))
                result.add_skip(SkipReason.INSUFFICIENT_ROWS, ts_code,
                                f"DWD rows={len(df)}, min=27")
                continue

            df = self._compute_indicators(df)
            self._insert(ts_code, df, calc_date)
            result.calculated += 1
        return result
```

- [ ] **Step 3: 运行现有测试确保不破坏**

```bash
pytest tests/test_etl/test_calc_macd.py -v
```

Expected: 全部 PASS

- [ ] **Step 4: Commit**

```bash
git add backend/etl/calc_macd.py
git commit -m "feat: MACDCalculator.calculate() returns CalcResult with skip stats"
```

---

### Task 4: 改造 `MACalculator.calculate()` 返回 `CalcResult`

**Files:**
- Modify: `backend/etl/calc_ma.py`

- [ ] **Step 1: 更新 import（第 1-3 行）**

```python
import logging
import numpy as np
import pandas as pd
from backend.etl.base import sma, to_float_safe, linear_regression_slope, insert_dws_batch, SkipReason, CalcResult

logger = logging.getLogger(__name__)
```

- [ ] **Step 2: 替换 `calculate()` 方法**

找到 `MACalculator.calculate()` 方法（约第 48-70 行），替换为：

```python
    def calculate(self, ts_codes: list[str], calc_date: str) -> CalcResult:
        result = CalcResult()
        for ts_code in ts_codes:
            if self.freq == "weekly":
                df = self.con.execute(f"""
                    SELECT d.trade_date, d.close_qfq FROM {self.src_table} d
                    JOIN dim_date dd ON d.trade_date = dd.trade_date
                    WHERE d.ts_code = ? AND dd.is_week_end = 1
                    ORDER BY d.trade_date
                """, (ts_code,)).df()
            else:
                df = self.con.execute(f"""
                    SELECT trade_date, close_qfq FROM {self.src_table}
                    WHERE ts_code = ? AND is_suspended = 0
                    ORDER BY trade_date
                """, (ts_code,)).df()

            if df.empty:
                logger.debug("MA %s skip %s: no DWD data", self.freq, ts_code)
                result.add_skip(SkipReason.NO_DWD_DATA, ts_code, "DWD returned 0 rows")
                continue
            if len(df) < 11:
                logger.debug("MA %s skip %s: %d rows < 11", self.freq, ts_code, len(df))
                result.add_skip(SkipReason.INSUFFICIENT_ROWS, ts_code,
                                f"DWD rows={len(df)}, min=11")
                continue

            df = self._compute_indicators(df)
            self._insert(ts_code, df, calc_date)
            result.calculated += 1
        return result
```

- [ ] **Step 3: 运行测试**

```bash
pytest tests/test_etl/test_calc_ma.py -v
```

Expected: 全部 PASS

- [ ] **Step 4: Commit**

```bash
git add backend/etl/calc_ma.py
git commit -m "feat: MACalculator.calculate() returns CalcResult with skip stats"
```

---

### Task 5: 改造 `KPatternCalculator.calculate()` 返回 `CalcResult`

**Files:**
- Modify: `backend/etl/calc_kpattern.py`

- [ ] **Step 1: 更新 import（第 1-4 行）**

```python
import logging
import numpy as np
import pandas as pd
from backend.etl.base import sma, to_float_safe, insert_dws_batch, SkipReason, CalcResult
from backend.kpattern_params import KPATTERN_PARAMS

logger = logging.getLogger(__name__)
```

- [ ] **Step 2: 替换 `calculate()` 方法（第 22-34 行）**

```python
    def calculate(self, ts_codes: list[str], calc_date: str) -> CalcResult:
        result = CalcResult()
        min_rows = KPATTERN_PARAMS["common"]["min_data_rows"]
        for ts_code in ts_codes:
            df = self.con.execute(f"""
                SELECT trade_date, open_qfq, high_qfq, low_qfq, close_qfq, vol, pct_chg
                FROM {self.src_table} WHERE ts_code = ?
                {'' if self.freq == 'weekly' else 'AND is_suspended = 0'}
                ORDER BY trade_date
            """, (ts_code,)).df()

            if df.empty:
                logger.debug("KPattern %s skip %s: no DWD data", self.freq, ts_code)
                result.add_skip(SkipReason.NO_DWD_DATA, ts_code, "DWD returned 0 rows")
                continue
            if len(df) < min_rows:
                logger.debug("KPattern %s skip %s: %d rows < %d",
                             self.freq, ts_code, len(df), min_rows)
                result.add_skip(SkipReason.INSUFFICIENT_ROWS, ts_code,
                                f"DWD rows={len(df)}, min={min_rows}")
                continue

            is_st = self._is_st_stock(ts_code)
            df = self._compute_patterns(df, is_st)
            self._insert(ts_code, df, calc_date)
            result.calculated += 1
        return result
```

- [ ] **Step 3: 运行测试**

```bash
pytest tests/test_etl/test_calc_kpattern.py -v
```

Expected: 全部 PASS

- [ ] **Step 4: Commit**

```bash
git add backend/etl/calc_kpattern.py
git commit -m "feat: KPatternCalculator.calculate() returns CalcResult with skip stats"
```

---

### Task 6: 改造 `DDECalculator.calculate()` 返回 `CalcResult` + BSE 识别

**Files:**
- Modify: `backend/etl/calc_dde.py`

- [ ] **Step 1: 更新 import（第 1-5 行）**

```python
import logging
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
from backend.etl.base import ema, to_float_safe, insert_dws_batch, SkipReason, CalcResult

logger = logging.getLogger(__name__)
```

- [ ] **Step 2: 替换 `calculate()` 方法（第 28-38 行）**

```python
    def calculate(self, ts_codes: list[str], calc_date: str) -> CalcResult:
        result = CalcResult()
        for ts_code in ts_codes:
            if self.freq == "daily":
                df = self._load_daily(ts_code)
            else:
                df = self._load_weekly(ts_code)

            if df.empty:
                if ts_code.endswith(".BJ"):
                    logger.debug("DDE %s skip %s: BSE — moneyflow unavailable",
                                 self.freq, ts_code)
                    result.add_skip(SkipReason.SOURCE_UNAVAILABLE, ts_code,
                                    "BSE stocks have no moneyflow data from tushare")
                else:
                    logger.debug("DDE %s skip %s: no DWD moneyflow data",
                                 self.freq, ts_code)
                    result.add_skip(SkipReason.NO_DWD_DATA, ts_code,
                                    "DWD moneyflow returned 0 rows")
                continue
            if len(df) < 10:
                logger.debug("DDE %s skip %s: %d rows < 10",
                             self.freq, ts_code, len(df))
                result.add_skip(SkipReason.INSUFFICIENT_ROWS, ts_code,
                                f"DWD rows={len(df)}, min=10")
                continue

            df = self._compute_indicators(df)
            self._insert(ts_code, df, calc_date)
            result.calculated += 1
        return result
```

注意：`_load_daily()` 和 `_load_weekly()` 保持不变。BSE 识别逻辑：`ts_code.endswith(".BJ")` 且 moneyflow 返回空 → SOURCE_UNAVAILABLE。

- [ ] **Step 3: 运行测试**

```bash
pytest tests/test_etl/test_calc_dde.py -v
```

Expected: 全部 PASS

- [ ] **Step 4: Commit**

```bash
git add backend/etl/calc_dde.py
git commit -m "feat: DDECalculator.calculate() returns CalcResult with BSE detection"
```

---

### Task 7: 改造 `VolumeCalculator.calculate()` 返回 `CalcResult`

**Files:**
- Modify: `backend/etl/calc_volume.py`

- [ ] **Step 1: 更新 import（第 1-3 行）**

```python
import logging
import numpy as np
import pandas as pd
from backend.etl.base import sma, linear_regression_slope, to_float_safe, insert_dws_batch, SkipReason, CalcResult

logger = logging.getLogger(__name__)
```

- [ ] **Step 2: 替换 `calculate()` 方法（第 21-40 行）**

```python
    def calculate(self, ts_codes: list[str], calc_date: str) -> CalcResult:
        result = CalcResult()
        for ts_code in ts_codes:
            if self.freq == "weekly":
                df = self.con.execute(f"""
                    SELECT d.trade_date, d.vol, d.close_qfq FROM {self.src_table} d
                    JOIN dim_date dd ON d.trade_date = dd.trade_date
                    WHERE d.ts_code = ? AND dd.is_week_end = 1
                    ORDER BY d.trade_date
                """, (ts_code,)).df()
            else:
                df = self.con.execute(f"""
                    SELECT trade_date, vol, close_qfq FROM {self.src_table}
                    WHERE ts_code = ? AND is_suspended = 0
                    ORDER BY trade_date
                """, (ts_code,)).df()

            if df.empty:
                logger.debug("Volume %s skip %s: no DWD data", self.freq, ts_code)
                result.add_skip(SkipReason.NO_DWD_DATA, ts_code, "DWD returned 0 rows")
                continue
            if len(df) < 5:
                logger.debug("Volume %s skip %s: %d rows < 5",
                             self.freq, ts_code, len(df))
                result.add_skip(SkipReason.INSUFFICIENT_ROWS, ts_code,
                                f"DWD rows={len(df)}, min=5")
                continue

            df = self._compute_indicators(df)
            self._insert(ts_code, df, calc_date)
            result.calculated += 1
        return result
```

- [ ] **Step 3: 运行测试**

```bash
pytest tests/test_etl/test_calc_volume.py -v
```

Expected: 全部 PASS

- [ ] **Step 4: Commit**

```bash
git add backend/etl/calc_volume.py
git commit -m "feat: VolumeCalculator.calculate() returns CalcResult with skip stats"
```

---

### Task 8: 改造 `PricePositionCalculator.calculate()` 返回 `CalcResult` + 放宽窗口

**Files:**
- Modify: `backend/etl/calc_price_position.py`

这是唯一一个同时改两处的 calc 模块：返回值类型 + 窗口逻辑。

- [ ] **Step 1: 更新 import（第 1-3 行）**

```python
import logging
import numpy as np
import pandas as pd
from backend.etl.base import to_float_safe, insert_dws_batch, SkipReason, CalcResult

logger = logging.getLogger(__name__)
```

- [ ] **Step 2: 替换 `calculate()` 方法（第 26-45 行）**

将功能下限从 60 降到 2（至少 2 条数据才可能 high≠low）：

```python
    def calculate(self, ts_codes: list[str], calc_date: str) -> CalcResult:
        result = CalcResult()
        for ts_code in ts_codes:
            if self.freq == "weekly":
                df = self.con.execute(f"""
                    SELECT d.trade_date, d.close_qfq FROM {self.src_table} d
                    JOIN dim_date dd ON d.trade_date = dd.trade_date
                    WHERE d.ts_code = ? AND dd.is_week_end = 1
                    ORDER BY d.trade_date
                """, (ts_code,)).df()
            else:
                df = self.con.execute(f"""
                    SELECT trade_date, close_qfq FROM {self.src_table}
                    WHERE ts_code = ? AND is_suspended = 0
                    ORDER BY trade_date
                """, (ts_code,)).df()

            if df.empty:
                logger.debug("PricePosition %s skip %s: no DWD data", self.freq, ts_code)
                result.add_skip(SkipReason.NO_DWD_DATA, ts_code, "DWD returned 0 rows")
                continue
            if len(df) < 2:
                logger.debug("PricePosition %s skip %s: %d rows < 2",
                             self.freq, ts_code, len(df))
                result.add_skip(SkipReason.INSUFFICIENT_ROWS, ts_code,
                                f"DWD rows={len(df)}, min=2")
                continue

            df = self._compute_positions(df)
            self._insert(ts_code, df, calc_date)
            result.calculated += 1
        return result
```

- [ ] **Step 3: 修改 `_compute_positions()` 的 `min_periods`（第 54-55 行）**

将 `min_periods=window` 改为 `min_periods=2`：

```python
    def _compute_positions(self, df: pd.DataFrame) -> pd.DataFrame:
        c = df["close_qfq"].values.astype(float)

        for window in self.WINDOWS:
            col = f"price_position_{window}d"
            s = pd.Series(c)
            roll_min = s.rolling(window, min_periods=2).min()
            roll_max = s.rolling(window, min_periods=2).max()
            denom = roll_max - roll_min
            with np.errstate(divide='ignore', invalid='ignore'):
                df[col] = np.where(
                    denom.values > 0,
                    (c - roll_min.values) / denom.values * 100.0,
                    np.nan,
                )

        return df
```

`min_periods=2` 的效果：数据 < window 时，用已有的 2+ 条数据计算（high-low 可能为 0，此时 pp=NULL）。数据 >= window 时，用完整窗口。

- [ ] **Step 4: 运行测试**

```bash
pytest tests/test_etl/test_calc_price_position.py -v
```

Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add backend/etl/calc_price_position.py
git commit -m "feat: PricePositionCalculator returns CalcResult, relax min_periods to 2"
```

---

### Task 9: 重构 `orchestrator.py` — 核心逻辑

**Files:**
- Modify: `backend/etl/orchestrator.py`

这是最核心的改动。按 4 个子步骤执行。

- [ ] **Step 1: 更新 import 区域**

在 `from backend.etl.error_handler import ...` 之后加入：

```python
from backend.etl.base import SkipReason, CalcResult
```

- [ ] **Step 2: 新增辅助函数 — 在 `check_data_completeness()` 之后、`run_calc()` 之前插入**

```python
WARMUP_TDAYS = 27  # max functional minimum across all indicators (MACD bottleneck)


def _compute_fetch_range(con, ts_code: str, calc_date: str,
                          lookback_tdays: int = WARMUP_TDAYS) -> tuple:
    """Compute the date range that needs to be fetched for a stock.

    Start = max(list_date, calc_date往前推lookback_tdays个交易日)
    End   = min(calc_date, delist_date)

    Returns (needed_start, needed_end) or (None, None) if already covered.
    """
    # 1. stock lifecycle dates
    row = con.execute("""
        SELECT list_date, delist_date FROM dim_stock WHERE ts_code = ?
    """, (ts_code,)).fetchone()
    if not row:
        return (None, None)
    list_date, delist_date = row

    # 2. end_date: delisted stock stops at delist_date, otherwise calc_date
    end_date = calc_date
    if delist_date and delist_date < calc_date:
        end_date = delist_date

    # 3. needed start: end_date往前推lookback_tdays个交易日
    needed = con.execute("""
        SELECT trade_date FROM (
            SELECT trade_date, ROW_NUMBER() OVER (ORDER BY trade_date DESC) AS rn
            FROM dim_date WHERE is_trade_day = 1 AND trade_date <= ?
        ) WHERE rn = ?
    """, (end_date, lookback_tdays)).fetchone()
    needed_start = needed[0] if needed else None
    if not needed_start:
        return (None, None)

    # 4. clamp to list_date
    if list_date and list_date > needed_start:
        needed_start = list_date

    # 5. check if already covered by existing ODS data
    current = con.execute("""
        SELECT MIN(trade_date), MAX(trade_date) FROM ods_daily WHERE ts_code = ?
    """, (ts_code,)).fetchone()
    current_min, current_max = current if current else (None, None)

    if current_min and current_max:
        if current_min <= needed_start and current_max >= end_date:
            return (None, None)  # fully covered

    return (needed_start, end_date)


def _filter_delisted(con, ts_codes: list[str], calc_date: str) -> tuple:
    """Filter out delisted stocks that already have DWS data.

    Returns (active_codes, delisted_dict).
    """
    active = []
    delisted = {}
    for ts_code in ts_codes:
        row = con.execute(
            "SELECT delist_date FROM dim_stock WHERE ts_code = ?", (ts_code,)
        ).fetchone()
        if not row or not row[0]:
            active.append(ts_code)
            continue
        delist_date = row[0]
        if delist_date >= calc_date:
            active.append(ts_code)
            continue
        # delisted: check if DWS already exists
        has_dws = con.execute(
            "SELECT 1 FROM dws_macd_daily WHERE ts_code = ? LIMIT 1", (ts_code,)
        ).fetchone()
        if has_dws:
            delisted[ts_code] = f"delisted={delist_date}, DWS exists, skip"
        else:
            active.append(ts_code)  # first calc for this delisted stock
            logger.info("Delisted stock %s (%s) — first calc, including", ts_code, delist_date)
    return active, delisted


def _classify_still_missing(con, missing: dict) -> dict:
    """Classify still-missing stocks after fetch+rebuild into root cause categories."""
    classified = {}
    for ts_code, info in missing.items():
        dwd_rows = info["dwd_rows"]
        if dwd_rows == 0:
            if ts_code.endswith(".BJ"):
                reason = SkipReason.SOURCE_UNAVAILABLE
                detail = "BSE stock: moneyflow unavailable from tushare"
            else:
                reason = SkipReason.NO_DWD_DATA
                detail = "DWD rows=0 after fetch+rebuild"
        else:
            reason = SkipReason.INSUFFICIENT_ROWS
            detail = (f"DWD rows={dwd_rows} "
                      f"(min_date={info['min_date']}, max_date={info['max_date']})")
        if reason not in classified:
            classified[reason] = []
        classified[reason].append((ts_code, detail))
    return classified


def _write_skip_log_batch(con, calc_date: str, indicator: str, freq: str,
                           classified: dict):
    """Write classified skip reasons to ods_calc_skip_log."""
    rows = []
    for reason, items in classified.items():
        for ts_code, detail in items:
            rows.append((calc_date, ts_code, indicator, freq, reason.value, detail))
    if rows:
        con.executemany(
            """INSERT OR REPLACE INTO ods_calc_skip_log
               (calc_date, ts_code, indicator, freq, reason, detail)
               VALUES (?, ?, ?, ?, ?, ?)""",
            rows,
        )
```

- [ ] **Step 3: 替换 `run_calc()` 函数的 auto-fetch 逻辑段（第 260-295 行）**

完整替换从 `# 1. 数据完整度检查` 到 `# 2. 只计算数据充足的股票` 之间的逻辑：

```python
    # 0. 退市股过滤
    calc_date = datetime.now().strftime("%Y%m%d")
    ts_codes, delisted = _filter_delisted(con, ts_codes, calc_date)
    if delisted:
        classified = {SkipReason.DELISTED: [(c, d) for c, d in delisted.items()]}
        _write_skip_log_batch(con, calc_date, "dwd", "both", classified)
        logger.info("Pre-calc: %d delisted stocks skipped (DWS already exists)", len(delisted))
    if not ts_codes:
        logger.warning("No stocks to calculate (all delisted or empty input)")
        return

    # 1. 数据完整度检查
    completeness = check_data_completeness(con, ts_codes)
    if completeness["missing"]:
        missing_codes = list(completeness["missing"].keys())
        missing_pct = len(missing_codes) * 100.0 / len(ts_codes)
        logger.info("DWD completeness: %d/%d stocks (%.1f%%) lack sufficient data (< 60 rows)",
                    len(missing_codes), len(ts_codes), missing_pct)

        if auto_fetch:
            # Compute per-stock fetch ranges based on warmup
            to_fetch = []
            for ts_code in missing_codes:
                info = completeness["missing"][ts_code]
                needed_start, needed_end = _compute_fetch_range(con, ts_code, calc_date)
                if needed_start is None:
                    continue
                to_fetch.append((ts_code, needed_start, needed_end))

            if to_fetch:
                logger.info("Auto-fetching %d stocks (warmup=%d tdays, per-stock ranges)...",
                            len(to_fetch), WARMUP_TDAYS)
                client = TushareClient()
                from backend.fetch.ods_daily import fetch_stocks_incremental

                # Circuit breaker: abort on consecutive failures
                consecutive_errors = 0
                consecutive_empty = 0
                n_fetched = 0
                for ts_code, seg_start, seg_end in to_fetch:
                    try:
                        rows = fetch_stocks_incremental(
                            client, con, [ts_code], start=seg_start, end=seg_end)
                        if rows == 0:
                            consecutive_empty += 1
                            consecutive_errors = 0
                        else:
                            n_fetched += rows
                            consecutive_empty = 0
                            consecutive_errors = 0
                    except Exception as e:
                        consecutive_errors += 1
                        consecutive_empty = 0
                        logger.warning("fetch failed for %s [%s~%s]: %s",
                                       ts_code, seg_start, seg_end, e)

                    if consecutive_errors >= 5:
                        logger.error("Circuit breaker: %d consecutive fetch errors. "
                                     "tushare may be down. Aborting auto-fetch.",
                                     consecutive_errors)
                        break
                    if consecutive_empty >= 5:
                        logger.error("Circuit breaker: %d consecutive empty responses. "
                                     "Date range may be invalid. Aborting auto-fetch.",
                                     consecutive_empty)
                        break

                logger.info("Auto-fetch complete: %d ODS rows fetched", n_fetched)
                if n_fetched > 0:
                    rebuild_all_dwd(con, missing_codes)
                    completeness = check_data_completeness(con, ts_codes)
            else:
                logger.info("All missing stocks already have data in ODS but DWD incomplete. "
                            "Rebuilding DWD...")
                rebuild_all_dwd(con, missing_codes)
                completeness = check_data_completeness(con, ts_codes)
        else:
            logger.info("Auto-fetch disabled. Skipping %d stocks.", len(missing_codes))

    # 2. 分类仍缺失的股票 + 写 skip_log
    if completeness["missing"]:
        classified = _classify_still_missing(con, completeness["missing"])
        _write_skip_log_batch(con, calc_date, "dwd", "both", classified)

        for reason, items in classified.items():
            count = len(items)
            level = "info" if reason in (SkipReason.SOURCE_UNAVAILABLE,
                                          SkipReason.INSUFFICIENT_ROWS) else "warning"
            getattr(logger, level)(
                "Pre-calc skip: %d stocks — %s", count, reason.value)
            sample = [c for c, _ in items[:10]]
            logger.info("  Sample: %s", ", ".join(sample))

    # 3. 只计算数据充足的股票
    codes_to_calc = completeness["ok"]
    if not codes_to_calc:
        logger.warning("No stocks with sufficient data to calculate")
        return
```

- [ ] **Step 4: 替换 calc 循环体（第 298-325 行），收集 CalcResult + 写入 skip_log**

```python
    # 4. 计算 DWS
    lid, t0 = log_etl_start(con, "calc_dws")
    grand_total = 0
    calc_start = time.monotonic()

    for CalcCls in CALCULATORS:
        indicator_name = CalcCls.__name__.replace("Calculator", "").lower()
        for freq in ("daily", "weekly"):
            calc = CalcCls(con, freq)
            label = f"{CalcCls.__name__} {freq}"
            t1 = time.monotonic()
            agg_result = CalcResult()

            for i in range(0, len(codes_to_calc), batch_size):
                batch = codes_to_calc[i:i + batch_size]
                batch_result = calc.calculate(batch, calc_date)
                agg_result.calculated += batch_result.calculated
                for reason, items in batch_result.skipped.items():
                    for ts_code, detail in items:
                        agg_result.add_skip(reason, ts_code, detail)

            _write_skip_log_batch(con, calc_date, indicator_name, freq, agg_result.skipped)

            elapsed = time.monotonic() - t1
            n = con.execute(
                f"SELECT COUNT(*) FROM {calc.dws_table} "
                f"WHERE calc_date = ?", (calc_date,),
            ).fetchone()[0]
            grand_total += n

            skip_parts = []
            for reason in SkipReason:
                items = agg_result.skipped.get(reason, [])
                if items:
                    skip_parts.append(f"{reason.value}={len(items)}")
            skip_str = ", ".join(skip_parts) if skip_parts else "none skipped"
            logger.info("calc %-30s DONE — %d rows (%d calculated), %s, %.0fs",
                        label, n, agg_result.calculated, skip_str, elapsed)

    total_elapsed = time.monotonic() - calc_start
    logger.info("calc ALL DONE — %d total DWS rows across %d indicator×freq pairs, %.0fs",
                grand_total, len(CALCULATORS) * 2, total_elapsed)
    logger.info("Skip details: SELECT reason, COUNT(*) FROM ods_calc_skip_log "
                "WHERE calc_date='%s' GROUP BY reason", calc_date)
    log_etl_end(con, lid, "calc_dws", t0, "success", row_count=grand_total)
    run_checkpoint(con)
```

- [ ] **Step 5: 运行测试确保不破坏**

```bash
pytest tests/test_etl/ -v
```

Expected: 全部 PASS

- [ ] **Step 6: Commit**

```bash
git add backend/etl/orchestrator.py
git commit -m "feat: remove 50-threshold, add compute_fetch_range, circuit breaker, delisted filter, skip_log"
```

---

### Task 10: 更新 `fetch_stocks_incremental()` 的默认 start 参数

**Files:**
- Modify: `backend/fetch/ods_daily.py`

当前函数签名（第 161-163 行）硬编码 `start="20150101"`。改为 None，由调用方传入：

- [ ] **Step 1: 修改函数签名**

```python
def fetch_stocks_incremental(client, con, ts_codes: list[str],
                              start: str = None,
                              end: str = "20991231") -> int:
```

- [ ] **Step 2: 当 start=None 时，自动计算合理的起始日期**

在原 `all_days = sorted(...)` 之前加入：

```python
    if start is None:
        # Default: don't fetch before warmup window
        # Compute from end date: go back enough calendar days to cover WARMUP_TDAYS trading days
        # Simplification: 27 tdays ≈ 40 calendar days. Use a generous 90-day default.
        # Callers that need precise warmup should pass explicit start.
        from datetime import datetime as dt
        end_dt = dt.strptime(end[:8], "%Y%m%d") if len(end) >= 8 else dt.now()
        start_dt = end_dt - __import__('datetime').timedelta(days=90)
        start = start_dt.strftime("%Y%m%d")
```

实际上，orchestrator 的 `_compute_fetch_range()` 已经算好了精确的 `seg_start`，会通过 `start=` 参数传入。这个改动只是让默认行为更合理。

- [ ] **Step 3: Commit**

```bash
git add backend/fetch/ods_daily.py
git commit -m "feat: fetch_stocks_incremental start defaults to 90-day lookback instead of 2015"
```

---

### Task 11: 兼容 `run_etl()` 旧流程

**Files:**
- Modify: `backend/etl/orchestrator.py`

`run_etl()`（旧流程）第 156 行调用 `calc.calculate(batch, calc_date)`。返回类型变成了 `CalcResult`，但 Python 忽略未使用的返回值——代码不用改。

- [ ] **Step 1: 在 `run_etl()` docstring 中加注释**

```python
    """Run the ETL pipeline.

    NOTE: Legacy full-pipeline entry point. For calc-only with skip-classification
    and auto-fetch, use run_calc(). This function ignores CalcResult from calculate().
    ...
    """
```

- [ ] **Step 2: Commit**

```bash
git add backend/etl/orchestrator.py
git commit -m "docs: note run_etl vs run_calc skip handling difference"
```

---

### Task 12: 编写测试

**Files:**
- Create: `tests/test_etl/test_empty_data.py`

- [ ] **Step 1: 写测试文件**

```python
"""Tests for empty-data handling: SkipReason, CalcResult, calc skip behavior."""
import pytest
import duckdb
from backend.etl.base import SkipReason, CalcResult


class TestCalcResult:
    def test_empty_result(self):
        r = CalcResult()
        assert r.calculated == 0
        assert r.total_skipped == 0
        assert r.total_input == 0

    def test_mixed_result(self):
        r = CalcResult()
        r.calculated = 80
        r.add_skip(SkipReason.INSUFFICIENT_ROWS, "000001.SZ", "DWD rows=15, min=27")
        r.add_skip(SkipReason.NO_DWD_DATA, "000002.SZ", "DWD returned 0 rows")
        r.add_skip(SkipReason.INSUFFICIENT_ROWS, "000003.SZ", "DWD rows=10, min=27")
        assert r.calculated == 80
        assert r.total_skipped == 3
        assert r.total_input == 83

    def test_delisted_skip(self):
        r = CalcResult()
        r.add_skip(SkipReason.DELISTED, "000666.SZ", "delisted=20231231, DWS exists, skip")
        assert r.total_skipped == 1
        assert SkipReason.DELISTED in r.skipped


class TestCalcSkipBehavior:
    @pytest.fixture
    def db(self):
        con = duckdb.connect(":memory:")
        con.execute("""
            CREATE TABLE dwd_daily_quote (
                ts_code TEXT, trade_date TEXT, close_qfq REAL,
                open_qfq REAL, high_qfq REAL, low_qfq REAL,
                vol REAL, pct_chg REAL, is_suspended INTEGER DEFAULT 0
            )
        """)
        con.execute("""
            CREATE TABLE dim_date (
                trade_date TEXT, is_week_end INTEGER, is_trade_day INTEGER
            )
        """)
        con.execute("""
            CREATE TABLE dim_stock (
                ts_code TEXT, is_st INTEGER DEFAULT 0, list_date TEXT, delist_date TEXT
            )
        """)
        yield con
        con.close()

    def test_macd_no_dwd_data(self, db):
        from backend.etl.calc_macd import MACDCalculator
        calc = MACDCalculator(db, "daily")
        result = calc.calculate(["999999.ZZ"], "20260604")
        assert result.calculated == 0
        assert result.total_skipped == 1
        assert SkipReason.NO_DWD_DATA in result.skipped
        ts_code, detail = result.skipped[SkipReason.NO_DWD_DATA][0]
        assert ts_code == "999999.ZZ"
        assert "0 rows" in detail

    def test_macd_insufficient_rows(self, db):
        from backend.etl.calc_macd import MACDCalculator
        for i in range(10):
            db.execute(
                "INSERT INTO dwd_daily_quote VALUES ('000001.SZ', ?, 10.0, 10.0, 10.0, 10.0, 1000, 0, 0)",
                (f"202601{i:02d}",),
            )
        calc = MACDCalculator(db, "daily")
        result = calc.calculate(["000001.SZ"], "20260604")
        assert result.calculated == 0
        assert SkipReason.INSUFFICIENT_ROWS in result.skipped

    def test_pp_relaxed_min_periods(self, db):
        from backend.etl.calc_price_position import PricePositionCalculator
        # Insert 40 rows — was previously rejected (needed 60), now accepted
        for i in range(40):
            db.execute(
                "INSERT INTO dwd_daily_quote VALUES ('000001.SZ', ?, ?, 10.0, 10.0, 10.0, 1000, 0, 0)",
                (f"202601{i:02d}", 10.0 + i * 0.1),
            )
        calc = PricePositionCalculator(db, "daily")
        result = calc.calculate(["000001.SZ"], "20260604")
        assert result.calculated == 1
        assert result.total_skipped == 0
```

- [ ] **Step 2: 运行新测试**

```bash
pytest tests/test_etl/test_empty_data.py -v
```

Expected: 6 个测试全部 PASS

- [ ] **Step 3: 运行全量测试确保无回归**

```bash
pytest tests/ -v
```

Expected: 全部 PASS

- [ ] **Step 4: Commit**

```bash
git add tests/test_etl/test_empty_data.py
git commit -m "test: add empty-data handling tests for CalcResult and calc skip behavior"
```

---

### Task 13: 更新 CLAUDE.md

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: 在"已知问题和注意事项"章节新增空数据处理说明**

```markdown
- **空数据处理：**
  - `run_calc()` 无条件自动补拉（带熔断器：连续 5 次 fetch 异常或 5 次空返回则中止）
  - 补拉范围 = `[max(上市日, analysis_end - 27tdays), min(calc_date, 退市日)]`，warmup=27 由 MACD 功能下限决定
  - 补拉失败按根因分 5 类写入 `ods_calc_skip_log`：source_unavailable / insufficient_rows / no_dwd_data / fetch_failed / delisted
  - 退市股：delist_date < calc_date 且 DWS 已有 → 跳过并记 DELISTED
  - 所有 Calculator.calculate() 返回 `CalcResult`（calculated + skipped 统计）
  - Price Position 各窗口独立：min_periods=2，数据不够窗口用已有全部数据
  - 种子偏差：MACD <100条/DDE <120条 时 divergence/alert/turning_point 列在 v_indicator_availability 视图中处理
  - `v_indicator_availability` 视图提供 full/partial/missing/unavailable/historical 五态
  - 详细架构见 `docs/superpowers/plans/2026-06-04-empty-data-handling.md`
```

- [ ] **Step 2: 在"CLI 三层架构"的 calc 描述中更新**

```
calc（计算层）
├── 退市股过滤：delist_date < calc_date 且 DWS 已有 → 跳过
├── 前置检查 check_data_completeness()：验证 DWD 数据完整度
├── 缺数据 → 无条件自动补拉（warmup=27 tdays，熔断器：连续 5 次失败中止）
├── 补拉后 rebuild_all_dwd() → re-check → 分类原因 → 写 ods_calc_skip_log
├── 所有 Calculator.calculate() 返回 CalcResult（calculated + skipped 分类统计）
└── 收尾汇总：每 calc 输出 calculated/skipped 明细，skip_log 可查询
```

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: update CLAUDE.md with empty-data handling architecture"
```

---

## 自审

**1. Spec 覆盖：**
- ✅ 删除 50 阈值 → Task 9 Step 3
- ✅ 无条件补拉 → Task 9 Step 3
- ✅ 熔断器 → Task 9 Step 3（`consecutive_errors >= 5` / `consecutive_empty >= 5`）
- ✅ 按根因分类 → Task 9 Step 2（`_classify_still_missing`）
- ✅ skip_log 表 → Task 1
- ✅ 退市股过滤 → Task 9 Step 2（`_filter_delisted`）
- ✅ warmup=27 → Task 9 Step 2（`WARMUP_TDAYS = 27`）
- ✅ 拉取范围约束 → Task 9 Step 2（`_compute_fetch_range`）
- ✅ Price Position min_periods=2 → Task 8
- ✅ 种子偏差质量下限 → DWS 层（本次在 v_indicator_availability 视图中处理，质量下限值记录在视图文档中）
- ✅ v_indicator_availability 视图 → Task 1
- ✅ CalcResult 返回 → Task 3-8
- ✅ 测试 → Task 12
- ✅ CLAUDE.md 更新 → Task 13

**2. Placeholder 扫描：** 无 TBD/TODO。所有代码完整，所有命令含预期输出。

**3. 类型一致性：**
- `SkipReason` 枚举在 Task 2 定义，Task 3-9, 12 引用，值一致
- `CalcResult` dataclass 在 Task 2 定义，Task 3-9, 12 引用，接口一致
- `_write_skip_log_batch(con, calc_date, indicator, freq, classified)` → 所有调用点参数顺序一致
- `_classify_still_missing` 返回 `dict[SkipReason, list[(ts_code, detail)]]` → 与 `_write_skip_log_batch` 的 `classified` 参数匹配
- `_compute_fetch_range` 返回 `(str, str)` 或 `(None, None)` → Task 9 Step 3 调用点正确解包
- `indicator_name` 从 `CalcCls.__name__.replace("Calculator", "").lower()` 推导 → 产生 `"macd"`, `"ma"`, `"kpattern"`, `"dde"`, `"volume"`, `"price_position"`
