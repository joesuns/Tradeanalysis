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

EXPORT_DIR = "exports"


def default_export_path(trade_date: str, output: str = None) -> str:
    """Default Excel path under exports/; explicit output is returned unchanged."""
    if output is not None:
        return output
    gen_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{EXPORT_DIR}/analysis_{trade_date}_gen{gen_ts}.xlsx"


# Column name translations (English → Chinese) — with units where applicable
_COL_NAMES = {
    "freq": "周期", "trade_date": "交易日期", "ts_code": "股票代码",
    "stock_code": "代码", "stock_name": "股票名称", "exchange": "交易所",
    "sector": "板块", "industry": "行业", "is_st": "ST",
    "close": "收盘价", "pct_chg": "涨跌幅%", "vol": "成交量(万手)", "amount": "成交额(亿)",
    "total_mv": "总市值(亿)", "pe_ttm": "市盈率", "turnover_rate": "换手率%",
    "kpattern": "K线形态", "kpattern_strength": "形态强度",
    "ema_12": "EMA12", "ema_26": "EMA26", "dif": "DIF", "dea": "DEA",
    "macd_bar": "MACD柱", "macd_divergence": "MACD背离", "macd_zone": "MACD区域",
    "macd_turning_point": "MACD转折", "macd_alert": "MACD警惕", "macd_trend": "MACD趋势",
    "macd_trend_strength": "MACD趋势强度",
    "ma_5": "MA5", "ma_10": "MA10",
    "bias_ma5": "MA5乖离率", "bias_ma10": "MA10乖离率",
    "ma5_slope": "MA5斜率", "ma10_slope": "MA10斜率",
    "ma_alignment": "均线形态", "ma_turning_point": "均线转折",
    "net_mf_amount": "主力净流入(万元)", "ddx": "DDX", "ddx2": "DDX2",
    "dde_trend": "DDE趋势", "dde_trend_strength": "DDE趋势强度", "dde_alert": "DDE警惕", "dde_divergence": "DDE背离",
    "ma_vol_5": "5日均量(万手)", "pct_vol_rank": "量能百分位",
    "vol_zone": "量能区域", "vol_trend": "量能趋势",
    "volume_ratio": "量比", "vol_ratio": "量比", "vol_trend_strength": "量能趋势强度",
    "vol_divergence": "量价背离",
    "price_position_60d": "60日价格滚动分位(%)", "price_position_120d": "120日价格滚动分位(%)",
    "price_position_250d": "250日价格滚动分位(%)",
    "vol_signal": "量价信号",
}

# Enum value translations (English → Chinese). NULL = no signal, shown as "-"
_ENUM_VALUES = {
    "kpattern": {"yang_bao_yin": "阳包阴", "yang_ke_yin": "阳克阴",
                 "yin_bao_yang": "阴包阳", "yin_ke_yang": "阴克阳",
                 "contrarian_yin_bao_yang": "阴包阳(反向买入)",
                 "contrarian_yin_ke_yang": "阴克阳(反向买入)",
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
    "vol_divergence": {"top_divergence": "顶背离", "bottom_divergence": "底背离"},
    "vol_signal": {
        "breakout_confirmed": "突破确认", "volume_climax": "放量滞涨",
        "volume_dry_up": "缩量止跌",
        "golden_cross_weakened": "金叉量弱", "dead_cross_weakened": "死叉量弱",
    },
}

# Event columns — NULL means "no signal today" (shown as "-")
_EVENT_SIGNAL_COLS = {
    "kpattern", "kpattern_strength",
    "macd_divergence", "macd_turning_point", "macd_alert",
    "ma_turning_point", "dde_alert", "dde_divergence",
    "vol_divergence", "vol_signal",
}
# State metrics — NULL means "not computable / insufficient history" (shown as "N/A")
_STATE_METRIC_COLS = {
    "pct_vol_rank", "vol_zone", "ma_alignment",
    "macd_zone", "macd_trend", "macd_trend_strength",
    "dde_trend", "dde_trend_strength",
    "vol_trend", "vol_trend_strength", "volume_ratio",
    "price_position_60d", "price_position_120d", "price_position_250d",
}
_FUNDAMENTAL_NA_COLS = {"pe_ttm"}
# Backward-compatible union for highlight logic
_SIGNAL_COLS = _EVENT_SIGNAL_COLS | _STATE_METRIC_COLS | _FUNDAMENTAL_NA_COLS

# Columns to round to 2 decimal places
_ROUND_2DP = {"close", "pct_chg", "pe_ttm", "turnover_rate", "net_mf_amount",
              "volume_ratio", "vol_trend_strength",
              "price_position_60d", "price_position_120d", "price_position_250d"}

# Columns to convert 万元 → 亿 (divide by 10000)
_CONVERT_DIV10000 = {"total_mv", "vol", "ma_vol_5"}  # → 万 (or 亿 for mv)
_CONVERT_DIV10 = set()  # unused, kept for clarity
_CONVERT_AMOUNT = {"amount"}  # 千元 → 亿 (/100000)

# Weekly column name overrides
_WEEKLY_OVERRIDE = {"ma_vol_5": "5周均量(万手)"}


def export_wide_to_excel(
    db_path: str,
    trade_date: str,       # YYYYMMDD
    output_path: str = "",  # .xlsx path (auto-timestamped if empty)
    filter_st: bool = True,
    include_index: bool = True,
    ts_codes: list[str] = None,  # 可选，只导出指定股票
) -> int:
    """Export horizontal daily+weekly merged analysis to Excel.

    Each row = one stock on trade_date, with daily indicators on the left
    and weekly (week-to-date) indicators on the right. Two-row header:
    Row 1 merges group labels (日线指标/周线指标), Row 2 has individual column names.
    Returns total rows written.
    """
    con = duckdb.connect(db_path)

    # ---- Daily data ----
    daily = con.execute(
        f"SELECT * FROM {VIEW_MAP['daily']} WHERE trade_date = ?"
        + (" AND is_st = 0" if filter_st else ""),
        [trade_date]
    ).df()
    if daily.empty:
        con.close()
        return 0

    # ---- Optional ts_code filter ----
    if ts_codes:
        daily = daily[daily["ts_code"].isin(ts_codes)]
        if daily.empty:
            con.close()
            return 0

    daily = _format_numbers(daily)

    # ---- Weekly data: use latest week-end ≤ trade_date ----
    week_end = con.execute(
        "SELECT MAX(trade_date) FROM dim_date "
        "WHERE trade_date <= ? AND is_week_end = 1",
        [trade_date]
    ).fetchone()[0]
    weekly = con.execute(
        f"SELECT * FROM {VIEW_MAP['weekly']} WHERE trade_date = ?"
        + (" AND is_st = 0" if filter_st else ""),
        [week_end]
    ).df() if week_end else pd.DataFrame()
    if ts_codes:
        weekly = weekly[weekly["ts_code"].isin(ts_codes)]
    weekly = _format_numbers(weekly)

    # Drop identity + fundamental columns from weekly (already in basic section from daily)
    id_cols_drop = ["freq", "trade_date", "stock_code", "stock_name",
                    "exchange", "sector", "industry", "is_st",
                    "close", "pct_chg", "vol", "amount", "total_mv",
                    "pe_ttm", "turnover_rate", "volume_ratio"]
    # Keep ts_code for merge
    weekly = weekly.drop(columns=[c for c in id_cols_drop if c in weekly.columns], errors="ignore")

    # Track which columns are daily vs weekly
    daily_cols = [c for c in daily.columns if c != "freq"]
    weekly_cols = list(weekly.columns)

    # Basic info columns (for both sheets)
    _ID_COLS = ["ts_code", "trade_date", "stock_code", "stock_name", "exchange", "sector", "industry", "is_st"]
    _FUND_COLS = ["close", "pct_chg", "vol", "amount", "total_mv", "pe_ttm", "turnover_rate", "volume_ratio"]
    basic_cols_outer = _ID_COLS + [c for c in _FUND_COLS if c in daily_cols]

    # Add __w__ prefix to weekly indicator columns (not ts_code — needed for merge)
    weekly_indicator_cols = [c for c in weekly_cols if c != "ts_code"]
    weekly_renamed = weekly.rename(columns={c: f"__w__{c}" for c in weekly_indicator_cols})

    # LEFT JOIN on ts_code. When weekly is empty (e.g. no week-end ≤ trade_date),
    # the frame has no ts_code column — keep daily rows, omit weekly indicators.
    if "ts_code" in weekly_renamed.columns:
        merged = daily.merge(weekly_renamed, on="ts_code", how="left")
        weekly_cols = weekly_indicator_cols  # for header building, exclude ts_code
    else:
        merged = daily.copy()
        weekly_cols = []

    # ---- Write to Excel ----
    wb = Workbook()
    wb.remove(wb.active)
    # ---- Signal-only analysis sheet (first sheet) ----
    _SIGNAL_ONLY = {"kpattern", "kpattern_strength",
                    "price_position_60d", "price_position_120d", "price_position_250d",
                    "macd_divergence", "macd_zone", "macd_turning_point", "macd_alert", "macd_trend",
                    "macd_trend_strength",
                    "ma_alignment", "ma_turning_point", "bias_ma5", "bias_ma10",
                    "dde_trend", "dde_trend_strength", "dde_alert", "dde_divergence",
                    "vol_zone", "vol_trend"}
    daily_signal_only = [c for c in daily_cols if c in _SIGNAL_ONLY or c in basic_cols_outer]
    weekly_signal_only = [c for c in weekly_cols if c in _SIGNAL_ONLY]
    _write_sheet_merged(wb, "综合分析", merged, daily_signal_only, weekly_signal_only)

    # ---- Full analysis sheet ----
    _write_sheet_merged(wb, "个股分析", merged, daily_cols, weekly_cols)

    # ---- SH Index ----
    if include_index:
        idx_daily = con.execute(
            f"SELECT * FROM {INDEX_VIEW_MAP['daily']} WHERE trade_date = ?", [trade_date]
        ).df()
        idx_daily = _format_numbers(idx_daily)
        if not idx_daily.empty:
            idx_daily = idx_daily.drop(columns=[c for c in id_cols_drop if c in idx_daily.columns], errors="ignore")
            _write_sheet_merged(wb, "上证指数", idx_daily, list(idx_daily.columns), [])

    con.close()

    import os
    if not output_path or output_path == "analysis.xlsx":
        output_path = default_export_path(trade_date)
    parent = os.path.dirname(os.path.abspath(output_path))
    if parent:
        os.makedirs(parent, exist_ok=True)

    wb.save(output_path)
    return len(merged)


def _format_numbers(df: "pd.DataFrame") -> "pd.DataFrame":
    """Round numeric columns + convert 万元→亿 for market cap."""
    for col in _ROUND_2DP:
        if col in df.columns:
            df[col] = df[col].apply(lambda x: round(float(x), 2) if pd.notna(x) else x)
    for col in _CONVERT_DIV10000:
        if col in df.columns:
            df[col] = df[col].apply(lambda x: round(float(x) / 10000, 2) if pd.notna(x) else x)
    for col in _CONVERT_DIV10:
        if col in df.columns:
            df[col] = df[col].apply(lambda x: round(float(x) / 10, 2) if pd.notna(x) else x)
    for col in _CONVERT_AMOUNT:
        if col in df.columns:
            df[col] = df[col].apply(lambda x: round(float(x) / 100000, 2) if pd.notna(x) else x)
    return df


def apply_display_nulls(df: pd.DataFrame) -> pd.DataFrame:
    """Apply export display semantics before Chinese rename."""
    df = df.copy()
    for col in _EVENT_SIGNAL_COLS:
        if col in df.columns:
            df[col] = df[col].fillna("-")
        wcol = f"__w__{col}"
        if wcol in df.columns:
            df[wcol] = df[wcol].fillna("-")
    for col in _STATE_METRIC_COLS | _FUNDAMENTAL_NA_COLS:
        if col in df.columns:
            df[col] = df[col].fillna("N/A")
        wcol = f"__w__{col}"
        if wcol in df.columns:
            df[wcol] = df[wcol].fillna("N/A")
    return df


def _translate_df(df: "pd.DataFrame") -> "pd.DataFrame":
    """Translate column names to Chinese, enum values to Chinese, apply null semantics."""
    df = apply_display_nulls(df)
    df = df.rename(columns={c: _COL_NAMES.get(c, c) for c in df.columns})
    for col, mapping in _ENUM_VALUES.items():
        cn_col = _COL_NAMES.get(col, col)
        if cn_col in df.columns:
            df[cn_col] = df[cn_col].map(lambda x: mapping.get(x, x) if pd.notna(x) else x)
    return df


def _write_sheet_merged(wb, sheet_name, df, daily_cols, weekly_cols):
    """Write merged daily+weekly DataFrame with two-row header and signal highlights."""
    ws = wb.create_sheet(title=sheet_name)

    # ── Build display column names ──
    # Identity columns: pure identification
    id_cols = ["ts_code", "trade_date", "stock_code", "stock_name", "exchange", "sector", "industry", "is_st"]
    # Fundamental columns (price/volume/valuation — not technical indicators)
    fund_cols = ["close", "pct_chg", "vol", "amount", "total_mv", "pe_ttm", "turnover_rate", "volume_ratio"]
    basic_cols = id_cols + [c for c in fund_cols if c in daily_cols]
    basic_names = [_COL_NAMES.get(c, c) for c in basic_cols if c in df.columns]

    # Daily technical indicator columns (same order as weekly — K-line first)
    daily_signal = [c for c in daily_cols if c not in basic_cols and c != "freq"]
    daily_names = [_COL_NAMES.get(c, c) for c in daily_signal]

    # Weekly technical indicator columns (with weekly-specific name overrides)
    weekly_signal = weekly_cols
    weekly_names = [_WEEKLY_OVERRIDE.get(c, _COL_NAMES.get(c, c)) for c in weekly_signal]

    # ── Translate data values (enum + NULL) ──
    # Daily enum translation
    for col, mapping in _ENUM_VALUES.items():
        if col in df.columns:
            df[col] = df[col].map(lambda x: mapping.get(x, x) if pd.notna(x) else x)
    # Weekly enum translation (columns have __w__ prefix in df)
    for col, mapping in _ENUM_VALUES.items():
        wcol = f"__w__{col}"
        if wcol in df.columns:
            df[wcol] = df[wcol].map(lambda x: mapping.get(x, x) if pd.notna(x) else x)
    df = apply_display_nulls(df)

    # Build final display column order: basic → daily_signal → weekly_signal
    display_order = [c for c in basic_cols if c in df.columns] \
                  + [c for c in daily_signal if c in df.columns] \
                  + [f"__w__{c}" for c in weekly_signal if f"__w__{c}" in df.columns]
    df = df[display_order]

    # Column positions in the worksheet
    n_basic = len([c for c in basic_cols if c in df.columns])
    n_daily = len([c for c in daily_signal if c in df.columns])
    n_weekly = len([c for c in weekly_signal if f"__w__{c}" in df.columns])

    # ── UED Styles ──
    # Row 1 group labels
    group_font = Font(name="微软雅黑", color="FFFFFF", size=11)
    group_fill_daily = PatternFill(start_color="1A5276", end_color="1A5276", fill_type="solid")
    group_fill_weekly = PatternFill(start_color="0D6B6B", end_color="0D6B6B", fill_type="solid")
    # Row 2 column names
    col_font = Font(name="微软雅黑", color="FFFFFF", size=10)
    # Data
    data_font = Font(name="微软雅黑", size=10, color="1D1D1F")
    # Basic info header
    basic_fill = PatternFill(start_color="2C3E50", end_color="2C3E50", fill_type="solid")
    # Separator between groups
    sep_left = Side(style="medium", color="FFFFFF")
    # Data borders
    thin_border = Border(
        left=Side(style="thin", color="E5E5EA"), right=Side(style="thin", color="E5E5EA"),
        top=Side(style="thin", color="E5E5EA"), bottom=Side(style="thin", color="E5E5EA"),
    )
    header_bottom = Border(bottom=Side(style="thin", color="5D6D7E"))
    white_fill = PatternFill(start_color="FFFFFF", end_color="FFFFFF", fill_type="solid")
    stripe_fill = PatternFill(start_color="F7F8FA", end_color="F7F8FA", fill_type="solid")

    # Signal highlight colors
    green = PatternFill(start_color="D4EDDA", end_color="D4EDDA", fill_type="solid")
    red = PatternFill(start_color="F8D7DA", end_color="F8D7DA", fill_type="solid")
    blue = PatternFill(start_color="D1ECF1", end_color="D1ECF1", fill_type="solid")

    # Indicator group colors — precise prefix match on English column names
    _GROUP_COLORS = {
        "kpattern": "C0392B",
        "price_position_": "E74C3C",
        "ema_": "8E44AD", "macd_": "8E44AD", "dif": "8E44AD", "dea": "8E44AD",
        "ma_vol_": "27AE60",  # before ma_ to avoid false match
        "ma_": "2980B9", "bias_": "2980B9", "ma5_": "2980B9", "ma10_": "2980B9",
        "dde_": "D35400", "ddx": "D35400", "net_mf": "D35400",
        "vol_": "27AE60", "pct_vol": "27AE60",
    }
    _DEFAULT_COLOR = "7F8C8D"

    def _color_for(col_eng: str) -> str:
        for prefix, color in _GROUP_COLORS.items():
            if col_eng.startswith(prefix):
                return color
        return _DEFAULT_COLOR

    # ── Row 1: Group labels ──
    daily_start = n_basic + 1
    weekly_start = n_basic + n_daily + 1
    weekly_end = n_basic + n_daily + n_weekly

    # Daily group label
    if n_daily > 0:
        ws.merge_cells(start_row=1, start_column=daily_start, end_row=1, end_column=daily_start + n_daily - 1)
        c = ws.cell(row=1, column=daily_start, value="日 线 指 标")
        c.fill = group_fill_daily; c.font = group_font
        c.alignment = Alignment(horizontal="center", vertical="center")
        c.border = header_bottom

    # Weekly group label
    if n_weekly > 0:
        ws.merge_cells(start_row=1, start_column=weekly_start, end_row=1, end_column=weekly_end)
        c = ws.cell(row=1, column=weekly_start, value="周 线 指 标")
        c.fill = group_fill_weekly; c.font = group_font
        c.alignment = Alignment(horizontal="center", vertical="center")
        c.border = header_bottom

    # Basic info columns: merged rows 1-2 with dark header
    for i in range(1, n_basic + 1):
        ws.merge_cells(start_row=1, start_column=i, end_row=2, end_column=i)
        ws.cell(row=1, column=i, value=basic_names[i - 1])
        for r in (1, 2):
            c2 = ws.cell(row=r, column=i)
            c2.fill = basic_fill; c2.font = col_font
            c2.alignment = Alignment(horizontal="center", vertical="center")
            c2.border = header_bottom

    # ── Row 2: Indicator column names ──
    for i, name in enumerate(daily_names):
        c = daily_start + i
        eng = daily_signal[i]  # original English name for precise color matching
        tint = _color_for(eng)
        cell = ws.cell(row=2, column=c, value=name)
        cell.font = col_font
        cell.fill = PatternFill(start_color=tint, end_color=tint, fill_type="solid")
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = header_bottom

    for i, name in enumerate(weekly_names):
        c = weekly_start + i
        eng = weekly_signal[i]  # original English name for precise color matching
        tint = _color_for(eng)
        cell = ws.cell(row=2, column=c, value=name)
        cell.font = col_font
        cell.fill = PatternFill(start_color=tint, end_color=tint, fill_type="solid")
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = header_bottom

    # ── Freeze: 股票名称 column + 2 header rows ──
    stock_name_idx = 1
    for i, c in enumerate(basic_cols):
        if c == "stock_name" and c in df.columns:
            stock_name_idx = i + 2  # +1 for 1-indexed, +1 for next column
            break
    freeze_letter = get_column_letter(stock_name_idx)
    ws.freeze_panes = f"{freeze_letter}3"
    ws.sheet_properties.tabColor = "1A5276"

    # ── Auto-fit column widths ──
    for col_idx in range(1, len(df.columns) + 1):
        header_name = ws.cell(row=2, column=col_idx).value or ""
        header_len = sum(2.2 if '一' <= c <= '鿿' else 1.0 for c in str(header_name))
        width = max(header_len + 2, 8)
        for row_idx in range(3, min(len(df) + 3, 23)):
            val = ws.cell(row=row_idx, column=col_idx).value
            if val is not None:
                val_len = sum(2.2 if '一' <= c <= '鿿' else 1.0 for c in str(val))
                width = max(width, min(val_len + 2, 30))
        ws.column_dimensions[get_column_letter(col_idx)].width = min(width, 22)

    # ── Data rows ──
    for row_idx, row in enumerate(df.itertuples(index=False), 3):
        row_fill = stripe_fill if row_idx % 2 == 1 else white_fill
        for col_idx, value in enumerate(row, 1):
            cell = ws.cell(row=row_idx, column=col_idx, value=value)
            cell.font = data_font
            cell.border = thin_border
            cell.fill = row_fill

    # ── Signal highlights ──
    kpattern_colors = {"阳包阴": green, "阳克阴": green, "墓碑线": red, "避雷针": red,
                       "高开长阴": red, "阴包阳": red, "阴克阳": red,
                       "阴包阳(反向买入)": green, "阴克阳(反向买入)": green}
    text_signal_cols = {
        "MACD转折": {"金叉": green, "死叉": red}, "MACD区域": {"多头": green, "空头": red},
        "MACD背离": {"顶背离": red, "底背离": green},
        "均线形态": {"多头强势": green, "多头初建": green, "多头衰竭": blue, "多头翻转": blue,
                     "空头强势": red, "空头初建": red, "空头衰竭": blue, "空头翻转": blue, "均线缠绕": blue},
        "均线转折": {"金叉": green, "死叉": red}, "DDE趋势": {"上升": green, "下降": red},
        "DDE背离": {"顶背离": red, "底背离": green}, "量能区域": {"爆量": red, "地量": blue},
        "量能趋势": {"放量": green, "缩量": red},
        "量价背离": {"顶背离": red, "底背离": green},
        "量价复合信号": {
            "突破确认": green,
            "放量滞涨": red,
            "缩量止跌": green,
            "金叉量弱": blue,
            "死叉量弱": blue,
        },
    }
    for col_name, value_colors in text_signal_cols.items():
        for prefix in ("", "__w__"):
            cn = col_name if not prefix else col_name  # same Chinese name for weekly
            lookup = col_name if not prefix else f"__w__{col_name}"
            if lookup in df.columns:
                col_idx = list(df.columns).index(lookup) + 1
                for row_idx in range(3, len(df) + 3):
                    val = ws.cell(row=row_idx, column=col_idx).value
                    if val in value_colors:
                        ws.cell(row=row_idx, column=col_idx).fill = value_colors[val]
    if "K线形态" in df.columns:
        col_idx = list(df.columns).index("K线形态") + 1
        for row_idx in range(3, len(df) + 3):
            val = ws.cell(row=row_idx, column=col_idx).value
            if val in kpattern_colors:
                ws.cell(row=row_idx, column=col_idx).fill = kpattern_colors[val]
