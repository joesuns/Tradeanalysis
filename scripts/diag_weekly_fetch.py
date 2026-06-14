"""只读诊断：确认 weekly_fetch 538 股的 fetch 空转主导原因。

不写库。复用 orchestrator 的完整度/计数逻辑 + ods_daily 的缺口判定。
"""
import duckdb

from backend.config import DUCKDB_PATH
from backend.etl.orchestrator import (
    check_data_completeness,
    resolve_weekly_warmup_start,
)
from backend.fetch.ods_daily import (
    get_all_active_codes,
    _local_trading_days,
    _get_missing_days_for_stock,
)

CALC_DATE = "20260602"


def main():
    con = duckdb.connect(DUCKDB_PATH, read_only=True)

    codes = get_all_active_codes(con)
    comp = check_data_completeness(con, codes, calc_date=CALC_DATE)
    wf = comp["weekly_fetch"]
    print(f"universe={len(codes)} ok={len(comp['ok'])} "
          f"missing={len(comp['missing'])} weekly_fetch={len(wf)}")

    # fetch 窗口 = [weekly_warmup_start, calc_date]（与 auto-fetch 宽桶对齐）
    wstart = resolve_weekly_warmup_start(con, CALC_DATE)
    all_days = _local_trading_days(con, wstart, CALC_DATE)
    print(f"weekly_warmup_start={wstart} window_tdays={len(all_days) if all_days else 0}")

    # 分桶分析这 538 股
    wf_codes = list(wf.keys())

    n_no_missing = 0          # 无缺口（fetch 应直接 continue，纯 SQL 开销）
    n_internal_only = 0       # 缺口全在内部（停牌型，max 已覆盖到 calc_date 区间末）
    n_tail_missing = 0        # 有尾部缺口（真落后，可拉取）
    total_missing_days = 0
    sample_internal = []
    sample_tail = []

    # 各股 ODS 全历史 max（判断缺口是内部还是尾部）
    ph = ",".join("?" for _ in wf_codes)
    ods_max = {r[0]: r[1] for r in con.execute(
        f"SELECT ts_code, MAX(trade_date) FROM ods_daily "
        f"WHERE ts_code IN ({ph}) GROUP BY ts_code", wf_codes
    ).fetchall()}

    last_window_day = all_days[-1] if all_days else CALC_DATE

    for code in wf_codes:
        missing = _get_missing_days_for_stock(con, code, all_days)
        total_missing_days += len(missing)
        if not missing:
            n_no_missing += 1
            continue
        # 该股 ODS 是否已覆盖到窗口末尾？若是 → 缺口全是内部停牌型
        stock_max = ods_max.get(code) or "00000000"
        if stock_max >= last_window_day:
            n_internal_only += 1
            if len(sample_internal) < 5:
                sample_internal.append((code, len(missing), stock_max))
        else:
            n_tail_missing += 1
            if len(sample_tail) < 5:
                sample_tail.append((code, len(missing), stock_max))

    print("\n=== weekly_fetch 538 股缺口分桶 ===")
    print(f"无缺口(continue, 纯SQL)        : {n_no_missing}")
    print(f"仅内部缺口(停牌型, 拉了拿空)    : {n_internal_only}")
    print(f"有尾部缺口(真落后, 可拉取)      : {n_tail_missing}")
    print(f"合计被判缺失日总数              : {total_missing_days}")
    print(f"内部缺口样本(code, 缺失日数, ODS_max): {sample_internal}")
    print(f"尾部缺口样本(code, 缺失日数, ODS_max): {sample_tail}")

    # 抽一只内部缺口股，看缺失日是否=停牌日（ODS 无 + 日历有）
    if sample_internal:
        code = sample_internal[0][0]
        missing = _get_missing_days_for_stock(con, code, all_days)
        print(f"\n=== 样本 {code} 前10个缺失日 ===")
        print(missing[:10])
        # 这些日子在 dim_date 是交易日（应为 True），在 ods_daily 无行（停牌）
        ph2 = ",".join("?" for _ in missing[:10])
        rows = con.execute(
            f"SELECT trade_date, is_trade_day FROM dim_date "
            f"WHERE trade_date IN ({ph2})", missing[:10]
        ).fetchall()
        print(f"dim_date 交易日判定: {rows}")

    con.close()


if __name__ == "__main__":
    main()
