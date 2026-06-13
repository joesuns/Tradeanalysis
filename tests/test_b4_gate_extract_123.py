"""extract_123_b4 against in-memory SQLite batch_trend_results."""
import sqlite3

from backend.b4_gate.extract import extract_123_b4


def _create_fixture_db() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.execute("""
        CREATE TABLE batch_trend_results (
            analysis_date TEXT,
            ts_code TEXT,
            short_macd_trend TEXT,
            short_macd_signal TEXT,
            daily_rev_macd_hist_turn TEXT,
            short_ma_regime TEXT,
            short_dde_trend TEXT,
            daily_rev_ddx2_slope_reversal TEXT,
            short_volume_trend TEXT,
            medium_macd_trend TEXT,
            medium_macd_signal TEXT,
            weekly_rev_macd_hist_turn TEXT,
            medium_ma_regime TEXT,
            medium_dde_trend TEXT,
            weekly_rev_ddx2_slope_reversal TEXT,
            medium_volume_trend TEXT
        )
    """)
    con.execute(
        """
        INSERT INTO batch_trend_results VALUES (
            '20260605', '000001.SZ',
            '上升', '金叉', '柱线拐上', '多头上行',
            '下降', '斜率拐头看多', '正常区·放量中',
            '持平', '死叉', '无', '空头收敛',
            '上升', '无数据', '爆量区·缩量中'
        )
        """
    )
    return con


def test_extract_123_b4_maps_daily_and_weekly():
    con = _create_fixture_db()
    df = extract_123_b4(con, "20260605", ["000001.SZ"], weekly_date="20260605")
    row = df.iloc[0]
    assert row["ts_code"] == "000001.SZ"
    assert row["trade_date"] == "20260605"
    assert row["macd_trend"] == "up"
    assert row["dde_trend"] == "down"
    assert row["vol_trend"] == "expanding"
    assert row["w_macd_trend"] == "flat"
    assert row["w_macd_alert"] is None
    assert row["w_dde_trend"] == "up"
    assert row["w_vol_trend"] == "shrinking"
    assert row["week_end"] == "20260605"


def test_extract_123_b4_from_path(tmp_path):
    db_file = tmp_path / "ref123.db"
    con = sqlite3.connect(str(db_file))
    con.executescript("""
        CREATE TABLE batch_trend_results (
            analysis_date TEXT, ts_code TEXT,
            short_macd_trend TEXT, short_macd_signal TEXT,
            daily_rev_macd_hist_turn TEXT, short_ma_regime TEXT,
            short_dde_trend TEXT, daily_rev_ddx2_slope_reversal TEXT,
            short_volume_trend TEXT,
            medium_macd_trend TEXT, medium_macd_signal TEXT,
            weekly_rev_macd_hist_turn TEXT, medium_ma_regime TEXT,
            medium_dde_trend TEXT, weekly_rev_ddx2_slope_reversal TEXT,
            medium_volume_trend TEXT
        );
        INSERT INTO batch_trend_results VALUES (
            '20260605', '000001.SZ',
            '上升', '金叉', '柱线拐上', '多头上行',
            '下降', '斜率拐头看多', '正常区·放量中',
            '持平', '死叉', '无', '空头收敛',
            '上升', '无数据', '爆量区·缩量中'
        );
    """)
    con.close()

    df = extract_123_b4(str(db_file), "20260605", ["000001.SZ"])
    assert len(df) == 1
    assert df.iloc[0]["macd_trend"] == "up"
