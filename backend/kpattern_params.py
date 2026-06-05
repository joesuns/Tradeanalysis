"""K-line pattern detection and strength scoring parameters.

All numeric thresholds and weights are centralized here so backtesting
can systematically sweep them without touching calculator code.

Default values match the original hardcoded constants in calc_kpattern.py.
"""

KPATTERN_PARAMS = {
    # ================================================================
    # Common / cross-cutting
    # ================================================================
    "common": {
        "min_data_rows": 30,        # Minimum rows required per stock
        "st_limit": 4.9,            # ST stock price-change limit (%)
        "non_st_limit": 9.9,        # Non-ST stock price-change limit (%)
    },

    # ================================================================
    # 1. 阳包阴 (yang_bao_yin) — Bull Engulfing (buy)
    # ================================================================
    "yang_bao_yin": {
        "weights": {
            "engulf": 0.5,          # Engulfing magnitude weight
            "volume": 0.3,          # Volume confirmation weight
            "close_pos": 0.2,       # Close position weight
        },
        "engulf_divisor": 2.0,
        "vol_divisor": 1.5,
        # Filters (MA10 enabled — backtest shows +32pp WR in bear markets)
        "ma10_filter": True,        # Require close > MA10 for trend context
        "vol_filter": 0.0,          # 0 = disabled; e.g. 1.2 = vol > ma5_vol * 1.2
    },

    # ================================================================
    # 2. 阳克阴 (yang_ke_yin) — Bull Overcomes Bear (buy)
    # ================================================================
    "yang_ke_yin": {
        "vol_multiplier": 1.2,      # v > ma5_vol * vol_multiplier
        "ma10_filter": True,        # Require close > MA10 for trend context
        "weights": {
            "top": 0.4,
            "volume": 0.4,
            "close_pos": 0.2,
        },
        "top_score_divisor": 0.02,
        "vol_divisor": 1.5,
    },

    # ================================================================
    # 3. 墓碑线 (mu_bei_xian) — Tombstone Doji (sell)
    # ================================================================
    "mu_bei_xian": {
        "trend_60d_high_pct": 0.90,
        "trend_20d_gain": 0.15,
        "doji_body_pct_max": 0.10,
        "doji_prev_close_pct": 0.005,
        "upper_shadow_body_ratio": 3.0,
        "upper_shadow_range_ratio": 0.60,
        "weights": {
            "shadow": 0.4,
            "doji_purity": 0.3,
            "high_confirm": 0.3,
        },
        "shadow_divisor": 4.0,
        "shadow_range_divisor": 0.8,
        "doji_purity_clip": 0.005,
    },

    # ================================================================
    # 4. 避雷针 (bi_lei_zhen) — Lightning Rod (sell)
    # ================================================================
    "bi_lei_zhen": {
        "trend_60d_high_pct": 0.90,
        "trend_20d_gain": 0.15,
        "small_body_pct_max": 0.20,
        "body_lower_third": 1.0 / 3.0,
        "upper_shadow_body_ratio": 3.0,
        "upper_shadow_range_ratio": 0.60,
        "weights": {
            "shadow": 0.4,
            "bottom_pos": 0.3,
            "high_confirm": 0.3,
        },
        "shadow_divisor": 4.0,
        "shadow_range_divisor": 0.8,
    },

    # ================================================================
    # 5. 高开长阴 (gao_kai_chang_yin) — Gap-up Long Bear (sell)
    # ================================================================
    "gao_kai_chang_yin": {
        "trend_window": 10,
        "trend_10d_gain": 0.15,
        "bear_body_min": 0.05,
        "vol_multiplier": 1.5,
        "weights": {
            "bear_body": 0.3,
            "volume": 0.3,
            "gap": 0.2,
            "gain10d": 0.2,
        },
        "body_normalize": 0.08,
        "vol_divisor": 2.5,
        "gap_normalize": 0.03,
        "gain_normalize": 0.25,
    },

    # ================================================================
    # 6. 阴包阳 (yin_bao_yang) — Bear Engulfing (sell)
    # ================================================================
    "yin_bao_yang": {
        "weights": {
            "engulf": 0.5,
            "volume": 0.3,
            "close_pos": 0.2,
        },
        "engulf_divisor": 2.0,
        "vol_divisor": 1.5,
    },

    # ================================================================
    # 7. 阴克阳 (yin_ke_yang) — Bear Overcomes Bull (sell)
    # ================================================================
    "yin_ke_yang": {
        "vol_multiplier": 1.2,
        "ma10_filter": True,
        "weights": {
            "bottom": 0.4,
            "volume": 0.4,
            "close_pos": 0.2,
        },
        "bottom_score_divisor": 0.02,
        "vol_divisor": 1.5,
    },
}
