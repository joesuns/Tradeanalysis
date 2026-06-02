#!/usr/bin/env python3
"""
临时脚本：拉取指定多只股票的 tushare 历史数据 + 计算技术指标 + 导出 Excel。

═══════════════════════════════════════════════════════════════
使用方式
═══════════════════════════════════════════════════════════════

  【场景1】最常用：拉数据 → 算指标 → 导出 Excel（一键完成）
    python3 fetch_stocks.py \
      --codes 000543.SZ,600580.SH,000630.SZ,002709,002837,603986 \
      --start 20250601 --export

  【场景2】只拉数据 + 算指标，不导出
    python3 fetch_stocks.py \
      --codes 002709,002837,603986 --start 20250601

  【场景3】数据已入库，直接导出 Excel
    python3 fetch_stocks.py \
      --codes 000543.SZ,600580.SH --export-only

  【场景4】导出指定日期的数据
    python3 fetch_stocks.py \
      --codes 000543.SZ,600580.SH --export-only --export-date 20260602

  【场景5】只拉原始数据（不计算指标）
    python3 fetch_stocks.py \
      --codes 000543.SZ --step fetch-ods --start 20250601

  【场景6】追加新股 — 已有全市场数据，只需给新股算指标
    python3 fetch_stocks.py \
      --codes 002837,603986 --start 20250601 --step calc-dws

  【场景7】股票代码自动补全（002709 → 002709.SZ，603986 → 603986.SH）
    python3 fetch_stocks.py --codes 002709,002837,603986 --start 20250601

═══════════════════════════════════════════════════════════════
参数说明
═══════════════════════════════════════════════════════════════

  --codes         股票代码，逗号分隔（必填）。支持简写（自动补全后缀）
  --start         起始日期 YYYYMMDD（默认 20250601）
  --end           结束日期 YYYYMMDD（默认今天）
  --step          ETL 步骤：fetch-ods / build-dim / build-dwd / calc-dws / build-all（默认）
  --export        ETL 完成后自动导出 Excel（格式与正式导出完全一致）
  --export-only   跳过 ETL，仅导出（数据必须已入库）
  --export-date   导出指定日期（默认自动取最新 calc_date）
  --output        指定输出路径（默认 exports/analysis_<ts>.xlsx）

═══════════════════════════════════════════════════════════════
核心逻辑
═══════════════════════════════════════════════════════════════

  - fetch-ods：按日期维度调 tushare，一次 API 调用返回全市场数据（无法按股票过滤）
  - DWD（前复权+周线）和 DWS（指标计算）只处理你指定的股票
  - 后缀规则：002/000/300xxx → .SZ，600/601/603/688xxx → .SH
  - 导出直接复用 export_wide_to_excel(ts_codes=...)，输出格式与正式导出 100% 一致
    （双行表头、日线+周线并排、分组着色、信号高亮、"综合分析"+"个股分析"两个 sheet）
  - Excel 保存在 ./exports/ 目录
"""

import argparse
import sys

import duckdb

from backend.config import DUCKDB_PATH
from backend.etl.orchestrator import run_etl
from backend.export_wide import export_wide_to_excel
from backend.log_config import setup_logging

logger = setup_logging("fetch_stocks")


# ═══════════════════════════════════════════════════════════════
# 股票代码自动补全
# ═══════════════════════════════════════════════════════════════

def fix_ts_code(code: str) -> str:
    """自动补全交易所后缀。"""
    code = code.strip().upper()
    if "." in code:
        return code
    if len(code) != 6 or not code.isdigit():
        logger.warning(f"无法识别代码格式，跳过: {code}")
        return None

    prefix = code[:3]
    if prefix in ("000", "001", "002", "003", "300", "301"):
        return f"{code}.SZ"
    elif prefix in ("600", "601", "603", "605", "688"):
        return f"{code}.SH"
    elif code.startswith(("4", "8")):
        return f"{code}.BJ"
    else:
        logger.warning(f"无法推断交易所，跳过: {code}")
        return None


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="拉取指定股票历史数据 + 计算技术指标 + 导出 Excel",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
    %(prog)s --codes 000543.SZ,600580.SH,000630.SZ --start 20250601 --export
    %(prog)s --codes 002709,002837,603986 --start 20250601
    %(prog)s --codes 000543.SZ --step fetch-ods --start 20250601
    %(prog)s --codes 000543.SZ,600580.SH --export-only --export-date 20260602
        """,
    )
    parser.add_argument(
        "--codes", required=True,
        help="股票代码，逗号分隔。支持简写（如 002709 自动补全为 002709.SZ）",
    )
    parser.add_argument(
        "--start", default="20250601",
        help="起始日期 YYYYMMDD（默认 20250601）",
    )
    parser.add_argument(
        "--end", default=None,
        help="结束日期 YYYYMMDD（默认今天）",
    )
    parser.add_argument(
        "--step", default="build-all",
        choices=["fetch-ods", "build-dim", "build-dwd", "calc-dws", "build-all"],
        help="ETL 步骤（默认 build-all 全流程）",
    )
    parser.add_argument(
        "--batch-size", type=int, default=100,
        help="每批处理的股票数（默认 100）",
    )

    # 导出选项
    export_group = parser.add_argument_group("导出 Excel")
    export_group.add_argument(
        "--export", action="store_true",
        help="ETL 完成后自动导出 Excel（格式与正式导出完全一致）",
    )
    export_group.add_argument(
        "--export-only", action="store_true",
        help="仅导出 Excel（不跑 ETL，前提是数据已入库）",
    )
    export_group.add_argument(
        "--export-date",
        help="导出指定日期 YYYYMMDD（默认自动取最新 calc_date）",
    )
    export_group.add_argument(
        "--output",
        help="输出 Excel 路径（默认 exports/analysis_<ts>.xlsx）",
    )

    args = parser.parse_args()

    # 解析 + 补全股票代码
    raw_codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    ts_codes = []
    for c in raw_codes:
        fixed = fix_ts_code(c)
        if fixed:
            ts_codes.append(fixed)

    if not ts_codes:
        logger.error("没有有效的股票代码")
        sys.exit(1)

    # ── 仅导出模式 ──
    if args.export_only:
        if not args.export_date:
            con = duckdb.connect(DUCKDB_PATH)
            placeholders = ",".join(["?" for _ in ts_codes])
            # 用周线最新日期（周线以周五为 trade_date，日线必包含对应日期）
            row = con.execute(
                f"SELECT MAX(trade_date) FROM v_ads_analysis_wide_weekly "
                f"WHERE ts_code IN ({placeholders})",
                ts_codes,
            ).fetchone()
            con.close()
            export_date = row[0] if row and row[0] else None
            if not export_date:
                logger.error("数据库中没有这些股票的数据，请先运行 ETL")
                sys.exit(1)
        else:
            export_date = args.export_date

        logger.info(f"导出 {len(ts_codes)} 只标的 — 日期: {export_date}（日线+周线）")
        n = export_wide_to_excel(DUCKDB_PATH, export_date, args.output or "", ts_codes=ts_codes)
        logger.info(f"已导出 {n} 行")
        return

    # ── ETL 模式 ──
    logger.info(f"目标标的 ({len(ts_codes)} 只): {', '.join(ts_codes)}")
    logger.info(f"日期范围: {args.start} ~ {args.end or '今天'}")
    logger.info(f"ETL 步骤: {args.step}")

    run_etl(
        step=args.step,
        ts_codes=ts_codes,
        start=args.start,
        end=args.end,
        batch_size=args.batch_size,
    )

    logger.info(f"完成！{len(ts_codes)} 只标的数据已入库")

    # ── ETL 后自动导出 ──
    if args.export:
        if args.export_date:
            export_date = args.export_date
        else:
            con = duckdb.connect(DUCKDB_PATH)
            placeholders = ",".join(["?" for _ in ts_codes])
            # 用周线最新日期，确保日线+周线都有数据
            row = con.execute(
                f"SELECT MAX(trade_date) FROM v_ads_analysis_wide_weekly "
                f"WHERE ts_code IN ({placeholders})",
                ts_codes,
            ).fetchone()
            con.close()
            export_date = row[0] if row and row[0] else None

        if export_date:
            logger.info(f"自动导出日期: {export_date}（日线+周线）")
            n = export_wide_to_excel(DUCKDB_PATH, export_date, args.output or "", ts_codes=ts_codes)
            logger.info(f"已导出 {n} 行")
        else:
            logger.warning("未找到导出数据，请检查 ETL 是否成功")


if __name__ == "__main__":
    main()
