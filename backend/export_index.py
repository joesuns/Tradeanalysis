"""Export index analysis to Excel — standalone sheet."""
import logging

import pandas as pd

logger = logging.getLogger(__name__)

# View for index daily data
_INDEX_VIEW = "v_ads_market_index_daily"

# Column order for index export sheet
_INDEX_COL_ORDER = [
    "ts_code", "index_name", "index_category",
    "close", "pct_chg", "vol", "amount",
    "pe_ttm", "pb", "total_mv",
    # MACD
    "dif", "dea", "macd_bar", "macd_zone", "macd_turning_point",
    "macd_trend", "macd_trend_strength", "macd_divergence",
    # MA
    "ma_5", "ma_10", "bias_ma5", "ma5_slope", "ma_alignment",
    # Volume
    "volume_ratio", "vol_trend_strength", "vol_zone", "vol_trend",
]


def export_index_sheet(con, trade_date: str, ws) -> int:
    """Write index overview to an openpyxl worksheet. Returns row count."""
    from openpyxl.styles import Font, PatternFill, Alignment

    df = con.execute(f"""
        SELECT * FROM {_INDEX_VIEW}
        WHERE trade_date = ?
        ORDER BY
            CASE
                WHEN ts_code = '000001.SH' THEN 0
                WHEN ts_code LIKE '%.SH' THEN 1
                WHEN ts_code LIKE '%.SZ' THEN 2
                ELSE 3
            END,
            ts_code
    """, [trade_date]).df()

    if df.empty:
        ws.cell(row=1, column=1, value=f"No index data for {trade_date}")
        return 0

    # Select columns that exist
    cols = [c for c in _INDEX_COL_ORDER if c in df.columns]
    df = df[cols]

    # Header styling
    header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
    header_font = Font(bold=True, size=11, color="FFFFFF")
    center_align = Alignment(horizontal="center")

    # Write header
    for j, col in enumerate(cols, 1):
        cell = ws.cell(row=1, column=j, value=col)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = center_align

    # Write data rows
    for i, (_, row) in enumerate(df.iterrows()):
        for j, col in enumerate(cols, 1):
            val = row[col]
            if isinstance(val, float) and pd.isna(val):
                val = None
            ws.cell(row=i + 2, column=j, value=val)

    # Auto-fit column widths
    from openpyxl.utils import get_column_letter
    for j, col in enumerate(cols, 1):
        max_width = max(len(str(col)), 12)
        ws.column_dimensions[get_column_letter(j)].width = min(max_width + 2, 22)

    ws.auto_filter.ref = ws.dimensions
    return len(df)
