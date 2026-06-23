# Export-E1: building sheets 性能优化

**状态：** ✅ 已实施 + 实库签字（2026-06-17）  
**目标：** `export_wide_to_excel` 中 `building sheets` 阶段从 ~105–140s 降至 ≤60s  
**范围：** 仅 `backend/export_wide.py` 写 sheet 路径；不改 DWS/视图/指标语义

## 根因

1. 两个 sheet（综合分析 / 个股分析）各做一次 enum + null transform（~120 列 × 5271 行 × 2）
2. 数据行逐 cell `ws.cell()` + font/border/fill（~120 万次 style 操作）
3. 列宽 autofit 在写 cell 后再读 worksheet（二次遍历）

## 方案

| 项 | 做法 |
|----|------|
| E1a | `_build_merged_display_df` 单次 transform，signal sheet 列子集复用 |
| E1b | enum 用 `Series.replace(dict)` 替代 `.map(lambda)` |
| E1c | 数据行 `dataframe_to_rows` + 从第 3 行批量写值 |
| E1d | 斑马纹改 `conditional_formatting`（`MOD(ROW(),2)=1`），数据行仅设 font |
| E1e | 列宽从 DataFrame 前 20 行估算，不写后读 |

**视觉差异（可接受）：** 数据行去掉 per-cell thin_border（表头样式不变）。

## 验收

### 单测

```bash
pytest tests/test_export/ tests/test_export_wide.py -v
```

### 实库（20260617，5271 行）

| 阶段 | 优化前 | 优化后（开发机） | 签字复测（2026-06-17 22:50） |
|------|--------|------------------|------------------------------|
| building sheets | ~105–140s | **27–29s** | **30s** |
| xlsx saved | — | ~13–14s | **14s** |
| export 总墙钟 | ~145s | **59–63s** | **69s** |

> 总墙钟波动主要来自 tradable enrich weekly（本次 16.1s vs 早先 11.8s）；**building sheets SLA（≤60s）稳定达标**。

命令：

```bash
python3 -m backend.cli export --date 20260617
# grep "progress export"
```

**输出文件：** `exports/analysis_20260617_gen20260617_225057.xlsx`

**Spot-check（openpyxl）：**

| 检查项 | 结果 |
|--------|------|
| Sheet「综合分析」+「个股分析」 | ✅ |
| 数据行 5271（max_row=5273，含 2 行表头） | ✅ |
| 综合分析 61 列 < 个股分析 101 列 | ✅ |
| freeze_panes = E3（股票名称后） | ✅ |
| CF 斑马纹（conditional_formatting 1 规则） | ✅ |
| 表头列注释（MACD 指标列） | ✅ |
| 数据行有值（row 3 spot） | ✅ |

**签字：** Export-E1 实库验收 **PASS**（main @ `7fac612` / PR #15 merged）

## 关键 API

- `SheetLayout` — 列布局（basic / daily / weekly）
- `_build_merged_display_df()` — 单次 transform（列名保留英文/__w__，表头中文）
- `_write_sheet_from_display()` — 快速写 sheet
- `_write_sheet_merged()` — 薄包装（指数 sheet / 单测兼容）
