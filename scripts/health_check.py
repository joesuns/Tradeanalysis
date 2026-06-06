"""全链路数据质量体检（只读）。

跑批后快速自检：ODS/DWD/DWS 各层不变量、取值域、参照完整性、跨层对账。
默认连只读，不写库。

用法:
    python -m scripts.health_check
    python scripts/health_check.py
退出码: 0=全部通过, 1=存在 FAIL。
"""
import os
import sys

import duckdb

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.config import DUCKDB_PATH

INDICATORS = ["kpattern", "macd", "ma", "dde", "volume", "price_position"]


class Checker:
    def __init__(self, con):
        self.con = con
        self.failures = 0

    def _scalar(self, sql):
        return self.con.execute(sql).fetchone()[0]

    def expect_zero(self, label, sql):
        try:
            v = self._scalar(sql) or 0
        except Exception as e:
            print(f"  [ERR ] {label}: {e}")
            self.failures += 1
            return
        ok = v == 0
        if not ok:
            self.failures += 1
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}: {v:,}")

    def info(self, label, sql):
        try:
            v = self._scalar(sql)
            print(f"  [info] {label}: {v:,}")
        except Exception as e:
            print(f"  [ERR ] {label}: {e}")


def run(con) -> int:
    c = Checker(con)

    print("=== A. ODS 层不变量 ===")
    for t in ["ods_daily", "ods_daily_basic", "ods_moneyflow"]:
        c.expect_zero(f"{t} 重复主键",
                      f"SELECT COUNT(*) FROM (SELECT ts_code,trade_date FROM {t} "
                      f"GROUP BY 1,2 HAVING COUNT(*)>1)")
    c.expect_zero("ods_daily high<low", "SELECT COUNT(*) FROM ods_daily WHERE high<low")
    c.expect_zero("ods_daily OHLC NULL",
                  "SELECT COUNT(*) FROM ods_daily WHERE open IS NULL OR high IS NULL "
                  "OR low IS NULL OR close IS NULL")
    c.expect_zero("ods_daily adj_factor NULL/<=0",
                  "SELECT COUNT(*) FROM ods_daily WHERE adj_factor IS NULL OR adj_factor<=0")
    c.expect_zero("ods_daily vol/amount<0",
                  "SELECT COUNT(*) FROM ods_daily WHERE vol<0 OR amount<0")

    print("=== B. DWD 日线不变量 ===")
    c.expect_zero("dwd_daily 重复主键",
                  "SELECT COUNT(*) FROM (SELECT ts_code,trade_date FROM dwd_daily_quote "
                  "GROUP BY 1,2 HAVING COUNT(*)>1)")
    c.expect_zero("dwd_daily close_qfq NULL/<=0",
                  "SELECT COUNT(*) FROM dwd_daily_quote WHERE close_qfq IS NULL OR close_qfq<=0")
    c.expect_zero("dwd_daily high<low", "SELECT COUNT(*) FROM dwd_daily_quote WHERE high_qfq<low_qfq")
    c.expect_zero("dwd_daily OHLC_qfq NULL",
                  "SELECT COUNT(*) FROM dwd_daily_quote WHERE open_qfq IS NULL OR "
                  "high_qfq IS NULL OR low_qfq IS NULL")
    c.expect_zero("dwd_daily is_suspended 非0/1",
                  "SELECT COUNT(*) FROM dwd_daily_quote WHERE is_suspended NOT IN (0,1)")
    c.expect_zero("dwd_daily OHLC一致性",
                  "SELECT COUNT(*) FROM dwd_daily_quote WHERE high_qfq<GREATEST(open_qfq,close_qfq) "
                  "OR low_qfq>LEAST(open_qfq,close_qfq)")
    c.expect_zero("dwd_daily 停牌日 vol<>0",
                  "SELECT COUNT(*) FROM dwd_daily_quote WHERE is_suspended=1 AND vol<>0")
    c.expect_zero("dwd_daily 停牌填充越界(超ODS区间)",
                  "WITH rng AS (SELECT ts_code,MIN(trade_date) mn,MAX(trade_date) mx "
                  "FROM ods_daily GROUP BY ts_code) "
                  "SELECT COUNT(*) FROM dwd_daily_quote q JOIN rng r ON q.ts_code=r.ts_code "
                  "WHERE q.is_suspended=1 AND (q.trade_date<r.mn OR q.trade_date>r.mx)")

    print("=== C. DWD 周线不变量 ===")
    c.expect_zero("dwd_weekly 重复主键",
                  "SELECT COUNT(*) FROM (SELECT ts_code,trade_date FROM dwd_weekly_quote "
                  "GROUP BY 1,2 HAVING COUNT(*)>1)")
    c.expect_zero("dwd_weekly close<=0/NULL",
                  "SELECT COUNT(*) FROM dwd_weekly_quote WHERE close_qfq IS NULL OR close_qfq<=0")
    c.expect_zero("dwd_weekly high<low", "SELECT COUNT(*) FROM dwd_weekly_quote WHERE high_qfq<low_qfq")
    c.expect_zero("dwd_weekly active_days∉[1,5]",
                  "SELECT COUNT(*) FROM dwd_weekly_quote WHERE active_days<1 OR active_days>5")

    print("=== D. DWS 12表通用不变量 ===")
    for ind in INDICATORS:
        for freq in ["daily", "weekly"]:
            t, v = f"dws_{ind}_{freq}", f"v_dws_{ind}_{freq}_latest"
            c.expect_zero(f"{t} calc_date NULL", f"SELECT COUNT(*) FROM {t} WHERE calc_date IS NULL")
            c.expect_zero(f"{t} 重复完整PK",
                          f"SELECT COUNT(*) FROM (SELECT ts_code,trade_date,calc_date FROM {t} "
                          f"GROUP BY 1,2,3 HAVING COUNT(*)>1)")
            c.expect_zero(f"{t} latest 视图唯一性",
                          f"SELECT (SELECT COUNT(*) FROM {v}) - "
                          f"(SELECT COUNT(*) FROM (SELECT DISTINCT ts_code,trade_date FROM {v}))")
            if freq == "weekly":
                c.expect_zero(f"{t} 非真周末孤儿",
                              f"SELECT COUNT(*) FROM {t} WHERE trade_date NOT IN "
                              f"(SELECT trade_date FROM dim_date WHERE is_week_end=1)")

    print("=== E. 取值域 ===")
    c.expect_zero("kpattern strength∉[0,1]",
                  "SELECT COUNT(*) FROM dws_kpattern_daily WHERE strength IS NOT NULL "
                  "AND (strength<0 OR strength>1)")
    ppcols = [r[0] for r in con.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name='dws_price_position_daily' AND column_name LIKE 'price_position%'"
    ).fetchall()]
    for col in ppcols:
        c.expect_zero(f"price_position {col}∉[0,100]",
                      f"SELECT COUNT(*) FROM dws_price_position_daily WHERE {col} IS NOT NULL "
                      f"AND ({col}<0 OR {col}>100)")

    print("=== F. 参照完整性 ===")
    c.expect_zero("DWS ts_code 不在 dim_stock",
                  "SELECT COUNT(*) FROM dws_macd_daily d WHERE NOT EXISTS"
                  "(SELECT 1 FROM dim_stock s WHERE s.ts_code=d.ts_code)")
    c.expect_zero("DWD trade_date 非交易日",
                  "SELECT COUNT(*) FROM (SELECT DISTINCT trade_date FROM dwd_daily_quote) x "
                  "WHERE NOT EXISTS(SELECT 1 FROM dim_date dd WHERE dd.trade_date=x.trade_date "
                  "AND dd.is_trade_day=1)")

    print("=== G. 周划分一致性 ===")
    _dt = ("CAST(substr(trade_date,1,4)||'-'||substr(trade_date,5,2)||'-'||"
           "substr(trade_date,7,2) AS DATE)")
    c.expect_zero("跨年周 is_week_end!=1 的自然周",
                  f"SELECT COUNT(*) FROM (SELECT date_trunc('week',{_dt}) w "
                  f"FROM dim_date WHERE is_trade_day=1 GROUP BY 1 HAVING SUM(is_week_end)<>1)")

    print("=== H. 跨层覆盖对账(信息) ===")
    c.info("ods_daily distinct(ts,td)", "SELECT COUNT(*) FROM (SELECT DISTINCT ts_code,trade_date FROM ods_daily)")
    c.info("dwd_daily 总行", "SELECT COUNT(*) FROM dwd_daily_quote")
    c.info("dwd_daily 非停牌行", "SELECT COUNT(*) FROM dwd_daily_quote WHERE is_suspended=0")
    c.info("dwd_daily 停牌填充行", "SELECT COUNT(*) FROM dwd_daily_quote WHERE is_suspended=1")
    c.info("MACD latest 落停牌日(应0)",
           "SELECT COUNT(*) FROM v_dws_macd_daily_latest m JOIN dwd_daily_quote q "
           "ON q.ts_code=m.ts_code AND q.trade_date=m.trade_date WHERE q.is_suspended=1")
    c.info("DDE缺口=非停牌无moneyflow",
           "SELECT COUNT(*) FROM dwd_daily_quote q WHERE q.is_suspended=0 AND NOT EXISTS"
           "(SELECT 1 FROM dwd_daily_moneyflow mf WHERE mf.ts_code=q.ts_code AND mf.trade_date=q.trade_date)")
    c.info("dim_stock 有 delist_date(已知=0)", "SELECT COUNT(*) FROM dim_stock WHERE delist_date IS NOT NULL")

    print("=== I. 周线 volume 状态指标 ===")
    c.info("volume_weekly pct_vol_rank 非空",
           "SELECT COUNT(*) FROM v_dws_volume_weekly_latest v "
           "JOIN dim_date d ON v.trade_date=d.trade_date AND d.is_week_end=1 "
           "WHERE v.pct_vol_rank IS NOT NULL")
    c.info("volume_weekly zone 非空",
           "SELECT COUNT(*) FROM v_dws_volume_weekly_latest v "
           "JOIN dim_date d ON v.trade_date=d.trade_date AND d.is_week_end=1 "
           "WHERE v.zone IS NOT NULL")

    print()
    if c.failures == 0:
        print("✅ 全部正确性检查通过")
    else:
        print(f"❌ {c.failures} 项检查 FAIL，请排查")
    return c.failures


def main():
    con = duckdb.connect(DUCKDB_PATH, read_only=True)
    try:
        failures = run(con)
    finally:
        con.close()
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
