import numpy as np
import pandas as pd
from backend.etl.base import sma, to_float_safe


class KPatternCalculator:
    """K-Line (candlestick) pattern calculator.

    Detects 7 candlestick patterns and computes a composite strength score (0.0-1.0).
    Works for both daily and weekly frequencies.
    """

    def __init__(self, con, freq: str = "daily"):
        self.con = con
        self.freq = freq
        src = "dwd_daily_quote" if freq == "daily" else "dwd_weekly_quote"
        self.src_table = src
        self.dws_table = f"dws_kpattern_{freq}"

    def calculate(self, ts_codes: list[str], calc_date: str):
        """Calculate K-line patterns for a batch of stocks. INSERT results into DWS table."""
        for ts_code in ts_codes:
            df = self.con.execute(f"""
                SELECT trade_date, open_qfq, high_qfq, low_qfq, close_qfq, vol, pct_chg
                FROM {self.src_table} WHERE ts_code = ? {'' if self.freq == 'weekly' else 'AND is_suspended = 0'}
                ORDER BY trade_date
            """, (ts_code,)).df()
            if df.empty or len(df) < 30:
                continue
            is_st = self._is_st_stock(ts_code)
            df = self._compute_patterns(df, is_st)
            self._insert(ts_code, df, calc_date)

    def _is_st_stock(self, ts_code: str) -> bool:
        row = self.con.execute(
            "SELECT is_st FROM dim_stock WHERE ts_code = ?", (ts_code,)
        ).fetchone()
        return bool(row and row[0])

    def _compute_patterns(self, df: pd.DataFrame, is_st: bool) -> pd.DataFrame:
        limit = 4.9 if is_st else 9.9

        o = df["open_qfq"].values.astype(float)
        h = df["high_qfq"].values.astype(float)
        l = df["low_qfq"].values.astype(float)
        c = df["close_qfq"].values.astype(float)
        v = df["vol"].values.astype(float)
        pct = df["pct_chg"].values.astype(float)

        n = len(df)

        # Pre-compute commonly used values
        body = np.abs(c - o)
        upper_shadow = h - np.maximum(o, c)
        lower_shadow = np.minimum(o, c) - l
        full_range = h - l
        is_bull = c >= o
        is_bear = c < o
        body_pct = body / np.where(full_range > 0, full_range, np.nan)

        # Volume MA5 for gao_kai_chang_yin filter + strength score
        ma_vol_5 = sma(v, 5)
        df["_ma5_vol"] = ma_vol_5  # populate for _compute_strength

        # Initialize pattern columns and strength
        df["yang_bao_yin"] = 0
        df["yang_ke_yin"] = 0
        df["mu_bei_xian"] = 0
        df["bi_lei_zhen"] = 0
        df["gao_kai_chang_yin"] = 0
        df["yin_bao_yang"] = 0
        df["yin_ke_yang"] = 0
        df["strength"] = np.nan

        # Compute uptrend conditions for patterns that need them
        uptrend_60d_high = np.zeros(n, dtype=bool)
        uptrend_20d_gain = np.zeros(n, dtype=bool)
        uptrend_10d_gain = np.zeros(n, dtype=bool)

        for i in range(n):
            # 60-day high position: require full 60-day lookback (i >= 59)
            if i >= 59:
                lookback_60 = max(0, i - 59)
                h60 = h[lookback_60:i + 1].max()
                if h60 > 0:
                    uptrend_60d_high[i] = c[i] >= h60 * 0.9

            # 20-day gain > 15%
            if i >= 20:
                prev_close_20 = c[i - 20]
                if prev_close_20 > 0:
                    uptrend_20d_gain[i] = (c[i] - prev_close_20) / prev_close_20 > 0.15

            # 10-day gain > 15%
            if i >= 10:
                prev_close_10 = c[i - 10]
                if prev_close_10 > 0:
                    uptrend_10d_gain[i] = (c[i] - prev_close_10) / prev_close_10 > 0.15

        # --- Pattern detection loop ---
        for i in range(1, n):
            # Filter: extreme price moves (limit up/down) -> null all patterns
            if abs(pct[i]) >= limit:
                continue

            if pd.isna(o[i]) or pd.isna(c[i]) or pd.isna(h[i]) or pd.isna(l[i]):
                continue

            # ============================================================
            # 1. 阳包阴 (yang_bao_yin): Bull engulfing
            #    Prev bear, cur bull, cur body engulfs prev body
            # ============================================================
            if (is_bear[i - 1] and is_bull[i]
                    and o[i] <= c[i - 1] and c[i] >= o[i - 1]
                    and body[i] > 0):
                df.at[i, "yang_bao_yin"] = 1

            # ============================================================
            # 2. 阳克阴 (yang_ke_yin): Bull overcomes bear
            #    vol > prev_vol * 1.2 AND max(open,close) > prev max(open,close)
            # ============================================================
            if (v[i] > v[i - 1] * 1.2
                    and max(o[i], c[i]) > max(o[i - 1], c[i - 1])):
                df.at[i, "yang_ke_yin"] = 1

            # ============================================================
            # 3. 墓碑线 (mu_bei_xian): Tombstone doji
            #    Uptrend + doji + long upper shadow
            # ============================================================
            is_uptrend = uptrend_60d_high[i] or uptrend_20d_gain[i]
            is_doji = body_pct[i] < 0.005 if not pd.isna(body_pct[i]) else False
            long_upper = (body[i] > 0 and upper_shadow[i] >= 3.0 * body[i])
            if is_uptrend and is_doji and long_upper:
                df.at[i, "mu_bei_xian"] = 1

            # ============================================================
            # 4. 避雷针 (bi_lei_zhen): Lightning rod
            #    Uptrend + small body + body in lower 1/3 + long upper shadow
            # ============================================================
            small_body = (body_pct[i] < 0.2 and not pd.isna(body_pct[i]))
            body_lower_third = False
            if full_range[i] > 0 and not pd.isna(full_range[i]):
                body_center_y = min(o[i], c[i]) + body[i] / 2.0
                body_lower_third = (body_center_y - l[i]) / full_range[i] < (1.0 / 3.0)
            long_upper_2 = body[i] > 0 and upper_shadow[i] >= 3.0 * body[i]
            if is_uptrend and small_body and body_lower_third and long_upper_2:
                df.at[i, "bi_lei_zhen"] = 1

            # ============================================================
            # 5. 高开长阴 (gao_kai_chang_yin): Gap-up long bear
            #    10d gain > 15% + gap up + long bear body + high volume
            # ============================================================
            gap_up = (o[i] > c[i - 1] and c[i - 1] > 0)
            long_bear_body = is_bear[i] and body[i] / o[i] >= 0.05
            high_vol = False
            if not pd.isna(ma_vol_5[i]) and ma_vol_5[i] > 0:
                high_vol = v[i] > ma_vol_5[i] * 1.5
            if uptrend_10d_gain[i] and gap_up and long_bear_body and high_vol:
                df.at[i, "gao_kai_chang_yin"] = 1

            # ============================================================
            # 6. 阴包阳 (yin_bao_yang): Bear engulfing
            #    Prev bull, cur bear, cur body engulfs prev body
            # ============================================================
            if (is_bull[i - 1] and is_bear[i]
                    and o[i] >= c[i - 1] and c[i] <= o[i - 1]
                    and body[i] > 0):
                df.at[i, "yin_bao_yang"] = 1

            # ============================================================
            # 7. 阴克阳 (yin_ke_yang): Bear overcomes bull
            #    vol > prev_vol * 1.2 AND min(open,close) < prev min(open,close)
            # ============================================================
            if (v[i] > v[i - 1] * 1.2
                    and min(o[i], c[i]) < min(o[i - 1], c[i - 1])):
                df.at[i, "yin_ke_yang"] = 1

        # --- Strength computation ---
        df["strength"] = self._compute_strength(df)

        return df

    def _compute_strength(self, df: pd.DataFrame) -> np.ndarray:
        """Compute pattern strength as a weighted composite (0.0-1.0).

        Strength is computed only for rows where at least one pattern is active.
        Components: body ratio weight (0.4), volume confirmation (0.3),
        trend alignment (0.3).
        """
        n = len(df)
        result = np.full(n, np.nan)

        o = df["open_qfq"].values.astype(float)
        c = df["close_qfq"].values.astype(float)
        h = df["high_qfq"].values.astype(float)
        l = df["low_qfq"].values.astype(float)
        v = df["vol"].values.astype(float)

        patterns = ["yang_bao_yin", "yang_ke_yin", "mu_bei_xian", "bi_lei_zhen",
                     "gao_kai_chang_yin", "yin_bao_yang", "yin_ke_yang"]

        for i in range(n):
            has_pattern = any(df.at[i, p] == 1 for p in patterns)
            if not has_pattern:
                continue

            # Body weight (0.4): larger body relative to range = stronger signal
            full_range = h[i] - l[i]
            body = abs(c[i] - o[i])
            body_score = min(body / full_range, 1.0) if full_range > 0 else 0.0

            # Volume confirmation (0.3): vol vs MA5_vol
            ma5v = df.get("_ma5_vol", pd.Series([np.nan] * n))
            vol_score = 0.5
            if i >= 5 and not pd.isna(ma5v.iloc[i]) if "_ma5_vol" in df.columns else False:
                ratio = v[i] / ma5v.iloc[i] if ma5v.iloc[i] > 0 else 1.0
                vol_score = min(ratio / 2.0, 1.0)
            else:
                # Use simple prev-volume comparison
                if i >= 1 and v[i - 1] > 0:
                    vol_score = min(v[i] / v[i - 1] / 2.0, 1.0)

            # Trend alignment (0.3): for bullish patterns, being above MA10 is stronger
            trend_score = 0.5  # neutral default
            if i >= 10:
                ma10 = np.mean(c[i - 9:i + 1])
                if ma10 > 0:
                    pct_from_ma = (c[i] - ma10) / ma10
                    # For bullish patterns (yang), above MA is stronger
                    # For bearish patterns (yin), below MA is stronger
                    is_bullish = any(df.at[i, p] == 1 for p in ["yang_bao_yin", "yang_ke_yin"])
                    is_bearish = any(df.at[i, p] == 1 for p in ["yin_bao_yang", "yin_ke_yang",
                                                                  "gao_kai_chang_yin"])
                    if is_bullish:
                        trend_score = min(max((pct_from_ma + 0.1) / 0.2, 0.0), 1.0)
                    elif is_bearish:
                        trend_score = min(max((-pct_from_ma + 0.1) / 0.2, 0.0), 1.0)

            strength = 0.4 * body_score + 0.3 * vol_score + 0.3 * trend_score
            result[i] = min(max(strength, 0.0), 1.0)

        return result

    def _insert(self, ts_code: str, df: pd.DataFrame, calc_date: str):
        for _, row in df.iterrows():
            self.con.execute(
                f"""INSERT OR REPLACE INTO {self.dws_table}
                (ts_code, trade_date, yang_bao_yin, yang_ke_yin, mu_bei_xian,
                 bi_lei_zhen, gao_kai_chang_yin, yin_bao_yang, yin_ke_yang,
                 strength, calc_date)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    ts_code,
                    row["trade_date"],
                    int(row.get("yang_bao_yin", 0)),
                    int(row.get("yang_ke_yin", 0)),
                    int(row.get("mu_bei_xian", 0)),
                    int(row.get("bi_lei_zhen", 0)),
                    int(row.get("gao_kai_chang_yin", 0)),
                    int(row.get("yin_bao_yang", 0)),
                    int(row.get("yin_ke_yang", 0)),
                    to_float_safe(row.get("strength")),
                    calc_date,
                ),
            )
