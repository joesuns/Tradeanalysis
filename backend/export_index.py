"""Export index analysis to Excel — standalone sheet matching stock sheet styling."""
import logging

import pandas as pd

from backend.export_wide import (
    _format_numbers,
    _transform_display_values,
    _resolve_sheet_layout,
    _write_sheet_from_display,
)
import backend.export_wide as _ew  # for _COL_NAMES / _FUND_COLS injection

logger = logging.getLogger(__name__)

# Views for index data
_INDEX_DAILY_VIEW = "v_ads_market_index_daily"
_INDEX_WEEKLY_VIEW = "v_ads_market_index_weekly"

# ── Index-specific Chinese column name overrides ────────────
# Stock _COL_NAMES supplies the baseline; these 4 keys override
# stock-specific names (e.g. ts_code "股票代码" → "指数代码").
_INDEX_COL_NAME_OVERRIDES = {
    "ts_code": "指数代码",
    "index_name": "指数名称",
    "index_category": "分类",
    "pb": "市净率",
}

# ── Index-specific column sets ──────────────────────────────

# 指数基本信息列（对齐综合分析基本面列语义，去掉个股专属列）
_INDEX_BASIC_COLS = [
    "ts_code", "index_name", "index_category",
    "close", "pct_chg", "vol", "amount",
    "pe_ttm", "pb", "total_mv",
    "turnover_rate", "volume_ratio",
]

# 指数信号列（信号聚焦 —— 去掉 EMA/MA/量能原始值；保留 macd_divergence 全文）
_INDEX_SIGNAL_COLS = [
    "macd_divergence",
    "macd_zone", "macd_turning_point", "macd_alert",
    "macd_trend", "macd_trend_strength",
    "bias_ma5", "bias_ma10",
    "ma_alignment", "ma_turning_point",
    "vol_zone", "vol_trend", "vol_divergence",
]


def export_index_sheet(con, trade_date: str, wb) -> int:
    """Write index overview sheet to workbook. Returns data row count.

    Creates a new "指数概览" sheet internally, matching the stock "综合分析" sheet
    styling: two-row headers (Row 1 group merges, Row 2 Chinese names with indicator
    colours), enum values translated to Chinese, proper null semantics, daily+weekly
    horizontal merge.
    """
    # ── Load daily data ─────────────────────────────────────
    daily = con.execute(
        f"SELECT * FROM {_INDEX_DAILY_VIEW} WHERE trade_date = ? "
        "ORDER BY CASE WHEN ts_code='000001.SH' THEN 0 "
        "WHEN ts_code LIKE '%.SH' THEN 1 WHEN ts_code LIKE '%.SZ' THEN 2 ELSE 3 END, ts_code",
        [trade_date],
    ).df()

    if daily.empty:
        ws = wb.create_sheet("指数概览")
        ws.cell(row=1, column=1, value=f"No index data for {trade_date}")
        return 0

    daily = _format_numbers(daily)

    # ── Load weekly data ─────────────────────────────────────
    # Index weekly stores trade_date as Friday (Monday anchor + 4 days),
    # which may differ from dim_date.is_week_end (Thu when Fri is non-trading).
    # Query the view (not the base table) for the latest available week.
    week_date = con.execute(
        f"SELECT MAX(trade_date) FROM {_INDEX_WEEKLY_VIEW} WHERE trade_date <= ?",
        [trade_date],
    ).fetchone()[0]

    weekly = pd.DataFrame()
    if week_date:
        weekly = con.execute(
            f"SELECT * FROM {_INDEX_WEEKLY_VIEW} WHERE trade_date = ?",
            [week_date],
        ).df()
        if not weekly.empty:
            weekly = _format_numbers(weekly)

    # ── Merge daily + weekly with __w__ prefix ────────────────
    # Determine available columns for layout
    daily_basic = [c for c in _INDEX_BASIC_COLS if c in daily.columns]
    daily_signal_cols = [c for c in _INDEX_SIGNAL_COLS if c in daily.columns]
    daily_cols = daily_basic + daily_signal_cols

    # Weekly: drop identity/identity-like columns (already in daily basic info)
    # plus active_days (internal metric, not for display)
    weekly_drop = {
        "ts_code", "index_name", "index_category",
        "close", "pct_chg", "vol", "amount",
        "pe_ttm", "pb", "total_mv",
        "turnover_rate", "volume_ratio",
        "active_days", "freq", "trade_date",
    }
    weekly_cols = []
    if not weekly.empty and "ts_code" in weekly.columns:
        weekly_keep = [c for c in weekly.columns if c not in weekly_drop]
        weekly_signal_cols = [c for c in _INDEX_SIGNAL_COLS if c in weekly_keep]
        weekly_cols = weekly_signal_cols

        weekly_renamed = weekly[["ts_code"] + weekly_keep].rename(
            columns={c: f"__w__{c}" for c in weekly_keep}
        )
        merged = daily.merge(weekly_renamed, on="ts_code", how="left")
    else:
        merged = daily.copy()

    if merged.empty:
        ws = wb.create_sheet("指数概览")
        ws.cell(row=1, column=1, value=f"No index data for {trade_date}")
        return 0

    try:
        # ── Inject index-specific Chinese column names ────────
        # _resolve_sheet_layout and _write_sheet_headers look up _COL_NAMES
        # for Chinese display names.  Only 4 keys need overriding; the rest
        # are already correct from stock _COL_NAMES.
        _orig_col_names = {}
        for k, v in _INDEX_COL_NAME_OVERRIDES.items():
            _orig_col_names[k] = _ew._COL_NAMES.get(k)
            _ew._COL_NAMES[k] = v

        # ── Inject index identity columns into _ID_COLS / _FUND_COLS ─────
        # _resolve_sheet_layout splits daily_cols into basic (matched against
        # _ID_COLS + _FUND_COLS in list order) and signal (everything else).
        # Insert (not append) so index_name/index_category appear right after
        # ts_code, and pb appears right after pe_ttm.
        _extra_id = []
        if "index_name" not in _ew._ID_COLS:
            _ew._ID_COLS.insert(1, "index_name")
            _extra_id.append("index_name")
        if "index_category" not in _ew._ID_COLS:
            _ew._ID_COLS.insert(2, "index_category")
            _extra_id.append("index_category")

        _extra_fund = []
        if "pb" not in _ew._FUND_COLS:
            # pe_ttm is at _FUND_COLS index 8; insert pb after it
            _ew._FUND_COLS.insert(9, "pb")
            _extra_fund.append("pb")

        # ── Build layout + transform values ───────────────────
        layout = _resolve_sheet_layout(daily_cols, weekly_cols, merged.columns)

        english = merged[layout.display_cols]
        display = _transform_display_values(english)

        # ── Write sheet ──────────────────────────────────────
        _write_sheet_from_display(wb, "指数概览", display, layout)
    finally:
        # Restore _ID_COLS
        for c in _extra_id:
            try:
                _ew._ID_COLS.remove(c)
            except ValueError:
                pass
        # Restore _FUND_COLS
        for c in _extra_fund:
            try:
                _ew._FUND_COLS.remove(c)
            except ValueError:
                pass
        # Restore _COL_NAMES
        for k, orig_val in _orig_col_names.items():
            if orig_val is None:
                _ew._COL_NAMES.pop(k, None)
            else:
                _ew._COL_NAMES[k] = orig_val

    return len(merged)
