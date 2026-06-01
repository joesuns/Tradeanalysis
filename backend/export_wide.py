import duckdb
import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import PatternFill, Font
from openpyxl.utils import get_column_letter

VIEW_MAP = {
    "daily": "v_ads_analysis_wide_daily",
    "weekly": "v_ads_analysis_wide_weekly",
}
INDEX_VIEW_MAP = {
    "daily": "v_ads_index_wide",
    "weekly": "v_ads_index_wide_weekly",
}


def export_wide_to_excel(
    db_path: str,
    trade_date: str,       # YYYYMMDD
    output_path: str,      # .xlsx path
    freq: str = "daily",   # "daily" | "weekly"
    filter_st: bool = True,
    include_index: bool = True,
) -> int:
    """Export analysis wide table to Excel. Individual stocks and SH index in separate sheets.

    Returns the total number of rows written across all sheets.
    """
    if freq not in VIEW_MAP:
        raise ValueError(f"Unsupported freq: {freq}. Use 'daily' or 'weekly'.")

    view = VIEW_MAP[freq]

    con = duckdb.connect(db_path)

    # ---- Sheet 1: Individual stocks ----
    sql_stocks = f"SELECT * FROM {view} WHERE trade_date = ?"
    params = [trade_date]
    if filter_st:
        sql_stocks += " AND is_st = 0"
    df_stocks = con.execute(sql_stocks, params).df()
    df_stocks = _reorder_signal_first(df_stocks)

    # ---- Sheet 2: SH Index (optional) ----
    df_index = None
    if include_index:
        index_view = INDEX_VIEW_MAP[freq]
        df_index = con.execute(
            f"SELECT * FROM {index_view} WHERE trade_date = ?", [trade_date]
        ).df()
        df_index = _reorder_signal_first(df_index)

    con.close()

    # ---- Write to Excel ----
    wb = Workbook()
    # Remove default empty sheet
    wb.remove(wb.active)

    _write_sheet(wb, f"个股_{freq}", df_stocks)
    if df_index is not None and len(df_index) > 0:
        _write_sheet(wb, "上证指数", df_index)

    wb.save(output_path)
    return len(df_stocks) + (len(df_index) if df_index is not None else 0)


def _reorder_signal_first(df: "pd.DataFrame") -> "pd.DataFrame":
    """Reorder columns: signal columns follow identity columns, numeric columns behind.

    User opens Excel and immediately sees signals without scrolling right.
    """
    head = [
        "freq", "trade_date", "ts_code", "stock_code", "stock_name", "exchange",
        "sector", "industry", "is_st", "close", "pct_chg",
    ]
    signals = [
        # K-line patterns
        "kpattern", "kpattern_strength",
        # MACD signals
        "macd_divergence", "macd_zone", "macd_turning_point", "macd_alert", "macd_trend",
        # MA signals
        "ma_alignment", "ma_turning_point", "bias_ma5", "bias_ma10", "ma5_slope", "ma10_slope",
        # DDE signals
        "dde_trend", "dde_alert", "dde_divergence",
        # Volume signals
        "vol_zone", "vol_trend",
    ]
    tail = [c for c in df.columns if c not in head and c not in signals]
    ordered = [c for c in head + signals + tail if c in df.columns]
    return df[ordered]


def _write_sheet(wb: Workbook, sheet_name: str, df: "pd.DataFrame"):
    """Write a DataFrame to a sheet with frozen header and K-line signal color highlights."""
    ws = wb.create_sheet(title=sheet_name)

    green = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
    red = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
    blue = PatternFill(start_color="BDD7EE", end_color="BDD7EE", fill_type="solid")
    header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
    header_font = Font(bold=True, color="FFFFFF", size=10)

    # Header row
    for col_idx, col_name in enumerate(df.columns, 1):
        cell = ws.cell(row=1, column=col_idx, value=col_name)
        cell.fill = header_fill
        cell.font = header_font
    ws.freeze_panes = "A2"

    # Data rows
    for row_idx, row in enumerate(df.itertuples(index=False), 2):
        for col_idx, value in enumerate(row, 1):
            ws.cell(row=row_idx, column=col_idx, value=value)

    # Signal highlights: K-line pattern (single enum column)
    kpattern_colors = {
        "yang_bao_yin": green,
        "yang_ke_yin": green,
        "mu_bei_xian": red,
        "bi_lei_zhen": red,
        "gao_kai_chang_yin": red,
        "yin_bao_yang": red,
        "yin_ke_yang": red,
    }
    if "kpattern" in df.columns:
        col_idx = list(df.columns).index("kpattern") + 1
        for row_idx in range(2, len(df) + 2):
            val = ws.cell(row=row_idx, column=col_idx).value
            if val in kpattern_colors:
                ws.cell(row=row_idx, column=col_idx).fill = kpattern_colors[val]

    # Signal highlights: text enum columns (by value match)
    text_signal_cols = {
        "macd_turning_point": {"golden_cross": green, "dead_cross": red},
        "macd_zone": {"bull": green, "bear": red},
        "macd_divergence": {"top_divergence": red, "bottom_divergence": green},
        "ma_alignment": {
            "多头强势": green,
            "多头初建": green,
            "多头衰竭": blue,
            "多头翻转": blue,
            "空头强势": red,
            "空头初建": red,
            "空头衰竭": blue,
            "空头翻转": blue,
            "均线缠绕": blue,
        },
        "ma_turning_point": {"golden_cross": green, "dead_cross": red},
        "dde_trend": {"up": green, "down": red},
        "dde_divergence": {"top_divergence": red, "bottom_divergence": green},
        "vol_zone": {"explosive": red, "low_volume": blue},
        "vol_trend": {"expanding": green, "shrinking": red},
    }
    for col_name, value_colors in text_signal_cols.items():
        if col_name in df.columns:
            col_idx = list(df.columns).index(col_name) + 1
            for row_idx in range(2, len(df) + 2):
                val = ws.cell(row=row_idx, column=col_idx).value
                if val in value_colors:
                    ws.cell(row=row_idx, column=col_idx).fill = value_colors[val]

    # Column widths
    for col_idx in range(1, len(df.columns) + 1):
        ws.column_dimensions[get_column_letter(col_idx)].width = 14
