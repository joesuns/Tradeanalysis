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

        # MA10 for trend-context filter on yang_ke_yin / yin_ke_yang
        ma_10 = sma(c, 10)

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
                    and max(o[i], c[i]) > max(o[i - 1], c[i - 1])
                    and not pd.isna(ma_10[i]) and c[i] > ma_10[i]):
                df.at[i, "yang_ke_yin"] = 1

            # ============================================================
            # 3. 墓碑线 (mu_bei_xian): Tombstone doji
            #    Uptrend + doji + long upper shadow
            # ============================================================
            is_uptrend = uptrend_60d_high[i] or uptrend_20d_gain[i]
            # Doji: |O-C|/prev_close < 0.5% OR body < 10% of amplitude
            is_doji_by_prev = (i >= 1 and c[i-1] > 0
                               and abs(c[i] - o[i]) / c[i-1] < 0.005)
            is_doji_by_amp = (body_pct[i] < 0.10 if not pd.isna(body_pct[i]) else False)
            is_doji = is_doji_by_prev or is_doji_by_amp
            # Long upper shadow: body>0 → >= 3x body; zero body → uppershadow/full_range > 60%
            if body[i] > 0:
                long_upper = upper_shadow[i] >= 3.0 * body[i]
            else:
                long_upper = full_range[i] > 0 and upper_shadow[i] / full_range[i] > 0.60
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
            if body[i] > 0:
                long_upper_2 = upper_shadow[i] >= 3.0 * body[i]
            else:
                long_upper_2 = full_range[i] > 0 and upper_shadow[i] / full_range[i] > 0.60
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
                    and min(o[i], c[i]) < min(o[i - 1], c[i - 1])
                    and not pd.isna(ma_10[i]) and c[i] < ma_10[i]):
                df.at[i, "yin_ke_yang"] = 1

        # --- Strength computation ---
        df["strength"] = self._compute_strength(df)

        return df

    def _compute_strength(self, df: pd.DataFrame) -> np.ndarray:
        """Compute per-pattern strength scores (0.0-1.0).

        Each of the 7 patterns uses its own dimensions and weights per spec §6.1.
        """
        n = len(df)
        result = np.full(n, np.nan)

        o = df["open_qfq"].values.astype(float)
        c = df["close_qfq"].values.astype(float)
        h = df["high_qfq"].values.astype(float)
        l = df["low_qfq"].values.astype(float)
        v = df["vol"].values.astype(float)
        ma5v = df.get("_ma5_vol", pd.Series([np.nan] * n)).values

        for i in range(n):
            full_range = h[i] - l[i]
            body = abs(c[i] - o[i])
            prev_body = abs(c[i-1] - o[i-1]) if i >= 1 else 0
            prev_vol = v[i-1] if i >= 1 and v[i-1] > 0 else 1.0

            # --- 阳包阴 (0.5 engulf + 0.3 volume + 0.2 close_pos) ---
            if df.at[i, "yang_bao_yin"] == 1:
                engulf = min(body / prev_body / 2.0, 1.0) if prev_body > 0 else 0.0
                vs = min(v[i] / ma5v[i] / 1.5, 1.0) if i >= 4 and not pd.isna(ma5v[i]) and ma5v[i] > 0 else \
                     min(v[i] / prev_vol / 1.5, 1.0)
                cp = (c[i] - l[i]) / full_range if full_range > 0 else 0.5
                result[i] = min(max(0.5*engulf + 0.3*vs + 0.2*cp, 0.0), 1.0)

            # --- 阳克阴 (0.4 top + 0.4 vol + 0.2 close_pos) ---
            elif df.at[i, "yang_ke_yin"] == 1:
                rtop = max(o[i], c[i]); prtop = max(o[i-1], c[i-1])
                top_s = min((rtop - prtop) / prtop / 0.02, 1.0) if prtop > 0 else 0.0
                vr = v[i] / prev_vol
                vs = min((vr - 1.2) / 0.8, 1.0) if vr > 1.2 else 0.0
                cp = (c[i] - l[i]) / full_range if full_range > 0 else 0.5
                result[i] = min(max(0.4*top_s + 0.4*vs + 0.2*cp, 0.0), 1.0)

            # --- 墓碑线 (0.4 shadow + 0.3 doji_purity + 0.3 high_confirm) ---
            elif df.at[i, "mu_bei_xian"] == 1:
                us = h[i] - max(o[i], c[i])
                if body > 0:
                    sh_s = min(us / body / 4.0, 1.0)
                else:
                    sh_s = min(us / full_range / 0.8, 1.0) if full_range > 0 else 1.0
                doji_p = 1.0 - min(abs(c[i]-o[i]) / c[i-1] / 0.005, 1.0) if i >= 1 and c[i-1] > 0 else 0.5
                # high_confirm: 60d high 10% = 0.6, +20d gain >15% = 1.0
                hi_cf = 0.0
                if i >= 59:
                    h60 = h[i-59:i+1].max()
                    if c[i] >= h60 * 0.9:
                        hi_cf = 0.6
                if i >= 20 and c[i-20] > 0 and (c[i]-c[i-20])/c[i-20] > 0.15:
                    hi_cf = 1.0
                result[i] = min(max(0.4*sh_s + 0.3*doji_p + 0.3*hi_cf, 0.0), 1.0)

            # --- 避雷针 (0.4 shadow + 0.3 bottom_pos + 0.3 high_confirm) ---
            elif df.at[i, "bi_lei_zhen"] == 1:
                us = h[i] - max(o[i], c[i])
                if body > 0:
                    sh_s = min(us / body / 4.0, 1.0)
                else:
                    sh_s = min(us / full_range / 0.8, 1.0) if full_range > 0 else 1.0
                bp = 1.0 - (c[i] - l[i]) / full_range if full_range > 0 else 0.0
                hi_cf = 0.0
                if i >= 59:
                    h60 = h[i-59:i+1].max()
                    if c[i] >= h60 * 0.9:
                        hi_cf = 0.6
                if i >= 20 and c[i-20] > 0 and (c[i]-c[i-20])/c[i-20] > 0.15:
                    hi_cf = 1.0
                result[i] = min(max(0.4*sh_s + 0.3*bp + 0.3*hi_cf, 0.0), 1.0)

            # --- 高开长阴 (0.3 bear_body + 0.3 vol + 0.2 gap + 0.2 gain10d) ---
            elif df.at[i, "gao_kai_chang_yin"] == 1:
                bb = min(abs(c[i]-o[i]) / o[i] / 0.08, 1.0) if o[i] > 0 else 0.0
                vs = min(v[i] / ma5v[i] / 2.5, 1.0) if i >= 4 and not pd.isna(ma5v[i]) and ma5v[i] > 0 else \
                     min(v[i] / prev_vol / 2.5, 1.0)
                gp = min((o[i]-c[i-1]) / c[i-1] / 0.03, 1.0) if i >= 1 and c[i-1] > 0 else 0.0
                j = max(0, i-10)
                g10 = min((c[i]-c[j]) / c[j] / 0.25, 1.0) if i >= 10 and c[j] > 0 else 0.0
                result[i] = min(max(0.3*bb + 0.3*vs + 0.2*gp + 0.2*g10, 0.0), 1.0)

            # --- 阴包阳 (0.5 engulf + 0.3 volume + 0.2 close_pos) ---
            elif df.at[i, "yin_bao_yang"] == 1:
                engulf = min(body / prev_body / 2.0, 1.0) if prev_body > 0 else 0.0
                vs = min(v[i] / ma5v[i] / 1.5, 1.0) if i >= 4 and not pd.isna(ma5v[i]) and ma5v[i] > 0 else \
                     min(v[i] / prev_vol / 1.5, 1.0)
                cp = 1.0 - (c[i] - l[i]) / full_range if full_range > 0 else 0.5  # 光脚满分
                result[i] = min(max(0.5*engulf + 0.3*vs + 0.2*cp, 0.0), 1.0)

            # --- 阴克阳 (0.4 bottom + 0.4 vol + 0.2 close_pos) ---
            elif df.at[i, "yin_ke_yang"] == 1:
                rbot = min(o[i], c[i]); prbot = min(o[i-1], c[i-1])
                bot_s = min((prbot - rbot) / prbot / 0.02, 1.0) if prbot > 0 else 0.0
                vr = v[i] / prev_vol
                vs = min((vr - 1.2) / 0.8, 1.0) if vr > 1.2 else 0.0
                cp = 1.0 - (c[i] - l[i]) / full_range if full_range > 0 else 0.5
                result[i] = min(max(0.4*bot_s + 0.4*vs + 0.2*cp, 0.0), 1.0)

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
