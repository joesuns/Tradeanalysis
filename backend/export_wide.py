import duckdb
import pandas as pd
from datetime import datetime
from openpyxl import Workbook
from openpyxl.styles import PatternFill, Font, Border, Side, Alignment
from openpyxl.utils import get_column_letter

VIEW_MAP = {
    "daily": "v_ads_analysis_wide_daily",
    "weekly": "v_ads_analysis_wide_weekly",
}
INDEX_VIEW_MAP = {
    "daily": "v_ads_index_wide",
    "weekly": "v_ads_index_wide_weekly",
}

# Column name translations (English → Chinese) — with units where applicable
_COL_NAMES = {
    "freq": "周期", "trade_date": "交易日期", "ts_code": "股票代码",
    "stock_code": "代码", "stock_name": "股票名称", "exchange": "交易所",
    "sector": "板块", "industry": "行业", "is_st": "ST",
    "close": "收盘价", "pct_chg": "涨跌幅%", "vol": "成交量(手)", "amount": "成交额(千元)",
    "total_mv": "总市值(亿)", "pe_ttm": "市盈率", "turnover_rate": "换手率%",
    "kpattern": "K线形态", "kpattern_strength": "形态强度",
    "ema_12": "EMA12", "ema_26": "EMA26", "dif": "DIF", "dea": "DEA",
    "macd_bar": "MACD柱", "macd_divergence": "MACD背离", "macd_zone": "MACD区域",
    "macd_turning_point": "MACD转折", "macd_alert": "MACD警惕", "macd_trend": "MACD趋势",
    "ma_5": "MA5", "ma_10": "MA10",
    "bias_ma5": "MA5乖离率", "bias_ma10": "MA10乖离率",
    "ma5_slope": "MA5斜率", "ma10_slope": "MA10斜率",
    "ma_alignment": "均线形态", "ma_turning_point": "均线转折",
    "net_mf_amount": "主力净流入(万元)", "ddx": "DDX", "ddx2": "DDX2",
    "dde_trend": "DDE趋势", "dde_alert": "DDE警惕", "dde_divergence": "DDE背离",
    "ma_vol_5": "5日均量", "pct_vol_rank": "量能百分位",
    "vol_zone": "量能区域", "vol_trend": "量能趋势",
}

# Enum value translations (English → Chinese). NULL = no signal, shown as "-"
_ENUM_VALUES = {
    "kpattern": {"yang_bao_yin": "阳包阴", "yang_ke_yin": "阳克阴",
                 "yin_bao_yang": "阴包阳", "yin_ke_yang": "阴克阳",
                 "mu_bei_xian": "墓碑线", "bi_lei_zhen": "避雷针",
                 "gao_kai_chang_yin": "高开长阴"},
    "macd_zone": {"bull": "多头", "bear": "空头"},
    "macd_trend": {"up": "上升", "down": "下降", "flat": "走平"},
    "macd_turning_point": {"golden_cross": "金叉", "dead_cross": "死叉",
                           "near_golden": "近金叉", "near_dead": "近死叉"},
    "macd_divergence": {"top_divergence": "顶背离", "bottom_divergence": "底背离"},
    "macd_alert": {"upturn_reverse": "上升拐头", "downturn_reverse": "下降拐头",
                   "upturn_flat": "上升走平", "downturn_flat": "下降走平"},
    "ma_turning_point": {"golden_cross": "金叉", "dead_cross": "死叉",
                         "near_golden": "近金叉", "near_dead": "近死叉"},
    "dde_trend": {"up": "上升", "down": "下降", "flat": "走平"},
    "dde_divergence": {"top_divergence": "顶背离", "bottom_divergence": "底背离"},
    "dde_alert": {"upturn_reverse": "上升拐头", "downturn_reverse": "下降拐头",
                  "upturn_flat": "上升走平", "downturn_flat": "下降走平"},
    "vol_zone": {"explosive": "爆量", "low_volume": "地量", "normal": "正常"},
    "vol_trend": {"expanding": "放量", "shrinking": "缩量", "flat": "平量"},
}

# Signal columns — NULL means "no signal today" (shown as "-")
_SIGNAL_COLS = {"kpattern", "kpattern_strength", "macd_divergence", "macd_turning_point",
                "macd_alert", "ma_turning_point", "dde_alert", "dde_divergence"}

# Columns to round to 2 decimal places
_ROUND_2DP = {"close", "pct_chg", "pe_ttm", "turnover_rate", "net_mf_amount"}

# Columns to convert 万元 → 亿 (divide by 10000)
_CONVERT_TO_YI = {"total_mv"}


def export_wide_to_excel(
    db_path: str,
    trade_date: str,       # YYYYMMDD
    output_path: str = "",  # .xlsx path (auto-timestamped if empty)
    freq: str = "daily",   # "daily" | "weekly"
    filter_st: bool = True,
    include_index: bool = True,
) -> int:
    """Export analysis wide table to Excel. Returns total rows written."""
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
    df_stocks = _format_numbers(df_stocks)
    df_stocks = _translate_df(df_stocks)
    df_stocks = _reorder_signal_first(df_stocks)

    # ---- Sheet 2: SH Index (optional) ----
    df_index = None
    if include_index:
        index_view = INDEX_VIEW_MAP[freq]
        df_index = con.execute(
            f"SELECT * FROM {index_view} WHERE trade_date = ?", [trade_date]
        ).df()
        df_index = _format_numbers(df_index)
        df_index = _translate_df(df_index)
        df_index = _reorder_signal_first(df_index)

    con.close()

    # ---- Write to Excel ----
    wb = Workbook()
    wb.remove(wb.active)

    _write_sheet(wb, f"个股_{freq}", df_stocks)
    if df_index is not None and len(df_index) > 0:
        _write_sheet(wb, "上证指数", df_index)

    # Auto-timestamp filename if not specified
    if not output_path or output_path == "analysis.xlsx":
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = f"analysis_{ts}.xlsx"

    wb.save(output_path)
    return len(df_stocks) + (len(df_index) if df_index is not None else 0)


def _format_numbers(df: "pd.DataFrame") -> "pd.DataFrame":
    """Round numeric columns + convert 万元→亿 for market cap."""
    for col in _ROUND_2DP:
        if col in df.columns:
            df[col] = df[col].apply(lambda x: round(float(x), 2) if pd.notna(x) else x)
    for col in _CONVERT_TO_YI:
        if col in df.columns:
            df[col] = df[col].apply(lambda x: round(float(x) / 10000, 2) if pd.notna(x) else x)
    return df


def _translate_df(df: "pd.DataFrame") -> "pd.DataFrame":
    """Translate column names to Chinese, enum values to Chinese, NULL signals to '-'."""
    df = df.rename(columns={c: _COL_NAMES.get(c, c) for c in df.columns})
    for col, mapping in _ENUM_VALUES.items():
        cn_col = _COL_NAMES.get(col, col)
        if cn_col in df.columns:
            df[cn_col] = df[cn_col].map(lambda x: mapping.get(x, x) if pd.notna(x) else x)
    for col in _SIGNAL_COLS:
        cn_col = _COL_NAMES.get(col, col)
        if cn_col in df.columns:
            df[cn_col] = df[cn_col].fillna("-")
    return df


def _reorder_signal_first(df: "pd.DataFrame") -> "pd.DataFrame":
    """Reorder columns: signal columns follow identity columns, numeric columns behind."""
    head = [
        "freq", "trade_date", "ts_code", "stock_code", "stock_name", "exchange",
        "sector", "industry", "is_st", "close", "pct_chg",
    ]
    signals = [
        "kpattern", "kpattern_strength",
        "macd_divergence", "macd_zone", "macd_turning_point", "macd_alert", "macd_trend",
        "ma_alignment", "ma_turning_point", "bias_ma5", "bias_ma10", "ma5_slope", "ma10_slope",
        "dde_trend", "dde_alert", "dde_divergence",
        "vol_zone", "vol_trend",
    ]
    tail = [c for c in df.columns if c not in head and c not in signals]
    ordered = [c for c in head + signals + tail if c in df.columns]
    return df[ordered]


def _write_sheet(wb: Workbook, sheet_name: str, df: "pd.DataFrame"):
    """Write a DataFrame to a sheet with Apple-style clean header, auto-fit widths,
    row striping, borders, frozen identity columns, and signal color highlights."""
    ws = wb.create_sheet(title=sheet_name)

    # ── Apple-style clean design ──
    header_fill = PatternFill(start_color="F5F5F7", end_color="F5F5F7", fill_type="solid")
    header_font = Font(bold=True, color="1D1D1F", size=10)
    header_border = Border(bottom=Side(style="medium", color="8E8E93"))
    data_font = Font(size=10, color="1D1D1F")
    thin_border = Border(
        left=Side(style="thin", color="E5E5EA"),
        right=Side(style="thin", color="E5E5EA"),
        top=Side(style="thin", color="E5E5EA"),
        bottom=Side(style="thin", color="E5E5EA"),
    )
    stripe_fill = PatternFill(start_color="FAFAFA", end_color="FAFAFA", fill_type="solid")
    white_fill = PatternFill(start_color="FFFFFF", end_color="FFFFFF", fill_type="solid")

    green = PatternFill(start_color="D4EDDA", end_color="D4EDDA", fill_type="solid")
    red = PatternFill(start_color="F8D7DA", end_color="F8D7DA", fill_type="solid")
    blue = PatternFill(start_color="D1ECF1", end_color="D1ECF1", fill_type="solid")

    # ── Header row ──
    for col_idx, col_name in enumerate(df.columns, 1):
        cell = ws.cell(row=1, column=col_idx, value=col_name)
        cell.fill = header_fill
        cell.font = header_font
        cell.border = header_border
        cell.alignment = Alignment(horizontal="center", vertical="center")

    # ── Freeze: header row + identity columns (up to 股票名称) ──
    freeze_col = "A"
    if "股票名称" in df.columns:
        idx = list(df.columns).index("股票名称") + 2  # +1 for 1-indexed, +1 for next col
        freeze_col = get_column_letter(idx)
    ws.freeze_panes = f"{freeze_col}2"

    # ── Auto-fit column widths ──
    for col_idx, col_name in enumerate(df.columns, 1):
        header_len = sum(2.2 if '一' <= c <= '鿿' else 1.0 for c in str(col_name))
        width = max(header_len + 2, 8)
        for row_idx in range(2, min(len(df) + 2, 22)):
            val = ws.cell(row=row_idx, column=col_idx).value
            if val is not None:
                val_len = sum(2.2 if '一' <= c <= '鿿' else 1.0 for c in str(val))
                width = max(width, min(val_len + 2, 30))
        ws.column_dimensions[get_column_letter(col_idx)].width = min(width, 22)

    # ── Data rows ──
    for row_idx, row in enumerate(df.itertuples(index=False), 2):
        row_fill = stripe_fill if row_idx % 2 == 0 else white_fill
        for col_idx, value in enumerate(row, 1):
            cell = ws.cell(row=row_idx, column=col_idx, value=value)
            cell.font = data_font
            cell.border = thin_border
            cell.fill = row_fill

    # ── Signal highlights: K-line pattern ──
    kpattern_colors = {
        "阳包阴": green, "阳克阴": green,
        "墓碑线": red, "避雷针": red, "高开长阴": red,
        "阴包阳": red, "阴克阳": red,
    }
    if "K线形态" in df.columns:
        col_idx = list(df.columns).index("K线形态") + 1
        for row_idx in range(2, len(df) + 2):
            val = ws.cell(row=row_idx, column=col_idx).value
            if val in kpattern_colors:
                ws.cell(row=row_idx, column=col_idx).fill = kpattern_colors[val]

    # ── Signal highlights: text enum columns ──
    text_signal_cols = {
        "MACD转折": {"金叉": green, "死叉": red},
        "MACD区域": {"多头": green, "空头": red},
        "MACD背离": {"顶背离": red, "底背离": green},
        "均线形态": {
            "多头强势": green, "多头初建": green,
            "多头衰竭": blue, "多头翻转": blue,
            "空头强势": red, "空头初建": red,
            "空头衰竭": blue, "空头翻转": blue,
            "均线缠绕": blue,
        },
        "均线转折": {"金叉": green, "死叉": red},
        "DDE趋势": {"上升": green, "下降": red},
        "DDE背离": {"顶背离": red, "底背离": green},
        "量能区域": {"爆量": red, "地量": blue},
        "量能趋势": {"放量": green, "缩量": red},
    }
    for col_name, value_colors in text_signal_cols.items():
        if col_name in df.columns:
            col_idx = list(df.columns).index(col_name) + 1
            for row_idx in range(2, len(df) + 2):
                val = ws.cell(row=row_idx, column=col_idx).value
                if val in value_colors:
                    ws.cell(row=row_idx, column=col_idx).fill = value_colors[val]
