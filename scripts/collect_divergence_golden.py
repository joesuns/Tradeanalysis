"""通达信结构背离 golden 采集辅助（导出本实现 / 人工填表 / 合并 / 验收）。

通达信无法 API 拉标注，流程：
  1. sample   — 抽 25 股代码列表
  2. worksheet — 导出对照表（本实现信号 + 空 tdx 列供人工填写）
  3. 在通达信加载「MACD顶底结构」指标，看「结构形成」日，填入 tdx_trade_date / tdx_divergence
  4. import   — 合并到 tests/fixtures/tdx_*_structure_golden.csv
  5. diff     — 算一致率（±N 交易日容差，需人工 golden）
  6. smoke    — 无人工：全市场抽样跑本实现，确认有数据且可算（不对比通达信）

无法人工标注时：跳过 3–5，直接 smoke + calc --force；验收靠 pytest 合成场景 + append 等价性。

用法:
    python -m scripts.collect_divergence_golden sample --count 25 -o exports/golden_sample.txt
    python -m scripts.collect_divergence_golden worksheet --indicator macd \\
        --ts-file exports/golden_sample.txt --start 20230101 --end 20251231 \\
        -o exports/macd_tdx_worksheet.csv
    python -m scripts.collect_divergence_golden import \\
        --worksheet exports/macd_tdx_worksheet.csv \\
        --golden tests/fixtures/tdx_macd_structure_golden.csv
    python -m scripts.collect_divergence_golden diff --indicator macd \\
        --golden tests/fixtures/tdx_macd_structure_golden.csv --tolerance 1
    python -m scripts.collect_divergence_golden export --indicator dde \\
        --ts-code 000001.SZ --start 20240101 --end 20251231
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import duckdb

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.config import DUCKDB_PATH
from backend.etl.divergence_golden_io import (
    GOLDEN_COLUMNS,
    WORKSHEET_COLUMNS,
    diff_golden_vs_impl,
    extract_divergence_events,
    load_dde_daily_frame,
    load_macd_daily_frame,
    merge_worksheet_into_golden,
    read_ts_codes_file,
    read_worksheet,
    sample_ts_codes,
    smoke_impl_on_codes,
    write_csv,
)

DEFAULT_MACD_GOLDEN = "tests/fixtures/tdx_macd_structure_golden.csv"
DEFAULT_DDE_GOLDEN = "tests/fixtures/tdx_dde_structure_golden.csv"


def _parse_ts_codes(args) -> list:
    codes = list(args.ts_code or [])
    if args.ts_file:
        codes.extend(read_ts_codes_file(args.ts_file))
    return codes


def _load_frame(con, indicator: str, ts_code: str, freq: str):
    if indicator == "macd":
        return load_macd_daily_frame(con, ts_code, freq)
    return load_dde_daily_frame(con, ts_code, freq)


def cmd_sample(args) -> int:
    con = duckdb.connect(DUCKDB_PATH, read_only=True)
    codes = sample_ts_codes(con, count=args.count)
    con.close()
    if not codes:
        print("ERROR: dim_stock 无可用代码")
        return 1
    out = args.output
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write("# 通达信 golden 抽样 — 每行一个 ts_code\n")
        for c in codes:
            f.write(c + "\n")
    print(f"sample: {len(codes)} codes -> {out}")
    return 0


def cmd_worksheet(args) -> int:
    codes = _parse_ts_codes(args)
    if not codes:
        print("ERROR: 需要 --ts-code 或 --ts-file")
        return 1

    con = duckdb.connect(DUCKDB_PATH, read_only=True)
    rows = []
    for ts_code in codes:
        df = _load_frame(con, args.indicator, ts_code, args.freq)
        events = extract_divergence_events(
            df, ts_code, freq=args.freq,
            start_date=args.start, end_date=args.end,
        )
        if events:
            rows.extend(events)
        else:
            rows.append({
                "ts_code": ts_code,
                "freq": args.freq,
                "impl_trade_date": "",
                "impl_divergence": "",
                "tdx_trade_date": "",
                "tdx_divergence": "",
                "note": "no_impl_event_in_range",
            })
    con.close()

    write_csv(args.output, rows, WORKSHEET_COLUMNS)
    impl_cnt = sum(1 for r in rows if r.get("impl_trade_date"))
    print(f"worksheet: {len(codes)} stocks, {impl_cnt} impl events -> {args.output}")
    print("请在通达信核对后填写 tdx_trade_date, tdx_divergence (top_divergence/bottom_divergence)")
    return 0


def cmd_export(args) -> int:
    codes = _parse_ts_codes(args)
    if not codes:
        print("ERROR: 需要 --ts-code 或 --ts-file")
        return 1

    con = duckdb.connect(DUCKDB_PATH, read_only=True)
    rows = []
    for ts_code in codes:
        df = _load_frame(con, args.indicator, ts_code, args.freq)
        rows.extend(extract_divergence_events(
            df, ts_code, freq=args.freq,
            start_date=args.start, end_date=args.end,
        ))
    con.close()

    out = args.output
    write_csv(out, rows, WORKSHEET_COLUMNS)
    print(f"export: {len(rows)} events -> {out}")
    return 0


def cmd_import(args) -> int:
    worksheet = read_worksheet(args.worksheet)
    added, skipped = merge_worksheet_into_golden(worksheet, args.golden)
    print(f"import: +{added} rows, skipped_dup={skipped} -> {args.golden}")
    return 0


def cmd_diff(args) -> int:
    con = duckdb.connect(DUCKDB_PATH, read_only=True)
    result = diff_golden_vs_impl(
        con, args.golden, args.indicator,
        tolerance=args.tolerance, freq=args.freq,
    )
    con.close()

    total = result["total"]
    matched = result["matched"]
    rate = result["rate"]
    print(f"diff {args.indicator}: {matched}/{total} matched ({rate:.1%}), tol={args.tolerance}d")
    for d in result["details"]:
        if not d["ok"]:
            print(f"  FAIL {d['ts_code']} expect {d['expect']} @ {d['expect_date']} "
                  f"impl={d.get('impl_hits', [])} {d.get('reason', '')}")

    target = 0.85 if args.indicator == "macd" else 0.70
    if total > 0 and rate < target:
        print(f"WARN: rate {rate:.1%} < KPI {target:.0%}")
        return 1
    return 0


def cmd_smoke(args) -> int:
    codes = _parse_ts_codes(args)
    if not codes:
        con = duckdb.connect(DUCKDB_PATH, read_only=True)
        codes = sample_ts_codes(con, count=args.count)
        con.close()
    if not codes:
        print("ERROR: 无可用股票代码")
        return 1

    con = duckdb.connect(DUCKDB_PATH, read_only=True)
    rows = smoke_impl_on_codes(
        con, args.indicator, codes,
        start_date=args.start, end_date=args.end, freq=args.freq,
    )
    con.close()

    ok = sum(1 for r in rows if r["status"] == "ok")
    no_data = sum(1 for r in rows if r["status"] == "no_data")
    total_top = sum(r["top"] for r in rows)
    total_bottom = sum(r["bottom"] for r in rows)
    print(f"smoke {args.indicator}: {ok} ok, {no_data} no_data, "
          f"events top={total_top} bottom={total_bottom}")
    for r in rows:
        if r["status"] == "no_data":
            print(f"  {r['ts_code']}: no DWD data")
        elif r["top"] or r["bottom"]:
            print(f"  {r['ts_code']}: bars={r['bars']} top={r['top']} bottom={r['bottom']}")
    if args.output:
        write_csv(args.output, rows, ["ts_code", "bars", "top", "bottom", "status"])
        print(f"written {args.output}")
    return 0 if ok > 0 else 1


def cmd_template(args) -> int:
    """空 golden 模板（仅表头 + 填写说明行）。"""
    write_csv(args.output, [], GOLDEN_COLUMNS)
    print(f"template: {args.output}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="MACD/DDE 结构背离 golden 采集")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_sample = sub.add_parser("sample", help="抽样 ts_code 列表")
    p_sample.add_argument("--count", type=int, default=25)
    p_sample.add_argument("-o", "--output", type=Path, default=Path("exports/golden_sample.txt"))
    p_sample.set_defaults(func=cmd_sample)

    p_ws = sub.add_parser("worksheet", help="导出对照表（impl + 空 tdx 列）")
    p_ws.add_argument("--indicator", choices=["macd", "dde"], required=True)
    p_ws.add_argument("--ts-code", nargs="*", default=[])
    p_ws.add_argument("--ts-file", type=Path)
    p_ws.add_argument("--start", default="20230101")
    p_ws.add_argument("--end", default="20251231")
    p_ws.add_argument("--freq", default="daily", choices=["daily", "weekly"])
    p_ws.add_argument("-o", "--output", type=Path,
                      default=Path("exports/divergence_tdx_worksheet.csv"))
    p_ws.set_defaults(func=cmd_worksheet)

    p_exp = sub.add_parser("export", help="仅导出本实现背离事件")
    p_exp.add_argument("--indicator", choices=["macd", "dde"], required=True)
    p_exp.add_argument("--ts-code", nargs="*", default=[])
    p_exp.add_argument("--ts-file", type=Path)
    p_exp.add_argument("--start", default="20230101")
    p_exp.add_argument("--end", default="20251231")
    p_exp.add_argument("--freq", default="daily", choices=["daily", "weekly"])
    p_exp.add_argument("-o", "--output", type=Path,
                       default=Path("exports/divergence_impl_events.csv"))
    p_exp.set_defaults(func=cmd_export)

    p_imp = sub.add_parser("import", help="worksheet 合并入 golden CSV")
    p_imp.add_argument("--worksheet", type=Path, required=True)
    p_imp.add_argument("--golden", type=Path)
    p_imp.set_defaults(func=cmd_import)

    p_diff = sub.add_parser("diff", help="golden vs 本实现一致率")
    p_diff.add_argument("--indicator", choices=["macd", "dde"], required=True)
    p_diff.add_argument("--golden", type=Path)
    p_diff.add_argument("--tolerance", type=int, default=1)
    p_diff.add_argument("--freq", default="daily", choices=["daily", "weekly"])
    p_diff.set_defaults(func=cmd_diff)

    p_smoke = sub.add_parser("smoke", help="抽样跑本实现（无需人工 golden）")
    p_smoke.add_argument("--indicator", choices=["macd", "dde"], required=True)
    p_smoke.add_argument("--ts-code", nargs="*", default=[])
    p_smoke.add_argument("--ts-file", type=Path)
    p_smoke.add_argument("--count", type=int, default=25,
                         help="未指定 ts 时抽样数量")
    p_smoke.add_argument("--start", default="20230101")
    p_smoke.add_argument("--end", default="20251231")
    p_smoke.add_argument("--freq", default="daily", choices=["daily", "weekly"])
    p_smoke.add_argument("-o", "--output", type=Path)
    p_smoke.set_defaults(func=cmd_smoke)

    p_tpl = sub.add_parser("template", help="空 golden CSV 模板")
    p_tpl.add_argument("-o", "--output", type=Path,
                       default=Path("tests/fixtures/tdx_macd_structure_golden.csv"))
    p_tpl.set_defaults(func=cmd_template)

    args = parser.parse_args()

    if args.cmd == "import" and args.golden is None:
        args.golden = Path(DEFAULT_MACD_GOLDEN)
    if args.cmd == "diff" and args.golden is None:
        args.golden = Path(
            DEFAULT_MACD_GOLDEN if args.indicator == "macd" else DEFAULT_DDE_GOLDEN
        )

    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
