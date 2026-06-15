#!/usr/bin/env bash
# 实库 smoke：change-driven refresh Wave 1–4（plan 2026-06-15 §9）
#
# 用法:
#   export ANALYSIS_DATE=20260612          # 已有 calc 快照的交易日
#   export DUCKDB_PATH=data/tradeanalysis.duckdb
#   ./scripts/smoke_change_driven_refresh.sh           # 只读检查 + 步骤 1/6
#   ./scripts/smoke_change_driven_refresh.sh --run-all # 含 run/refresh（写库+API）
#
# 前置: TUSHARE_TOKEN 已设；该日 ODS/DWS 已存在；非生产并发写。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

DATE="${ANALYSIS_DATE:-}"
DB="${DUCKDB_PATH:-data/tradeanalysis.duckdb}"
RUN_ALL=false
if [[ "${1:-}" == "--run-all" ]]; then
  RUN_ALL=true
fi

if [[ -z "$DATE" ]]; then
  echo "ERROR: 请设置 ANALYSIS_DATE=YYYYMMDD（建议选已有 calc 的快照日）" >&2
  exit 1
fi

if [[ ! -f "$DB" ]]; then
  echo "ERROR: DuckDB 不存在: $DB" >&2
  exit 1
fi

duck_sql() {
  python3 - <<PY
import duckdb
con = duckdb.connect("$DB", read_only=True)
rows = con.execute("""$1""").fetchall()
for r in rows:
    print("\t".join(str(x) for x in r))
con.close()
PY
}

section() { echo; echo "======== $1 ========"; }

section "0. 基线：该日是否已有 calc"
duck_sql "
SELECT step_name, status, row_count, started_at,
       left(data_completeness, 120) AS completeness_preview
FROM ods_etl_log
WHERE step_name IN ('run_fetch','run_rebuild_dwd','calc_dws','cli_refresh')
  AND data_completeness IS NOT NULL AND data_completeness != ''
  AND (
    data_completeness LIKE '%\"analysis_date\": \"$DATE\"%'
    OR data_completeness LIKE '%\"calc_date\": \"$DATE\"%'
  )
ORDER BY started_at DESC
LIMIT 10
"

if ! $RUN_ALL; then
  section "模式: 只读预览（加 --run-all 执行写库步骤）"
  echo "将执行的写库步骤:"
  echo "  1) python -m backend.cli run --date $DATE --skip-export"
  echo "  2) 手动 ODS 改一行 → 再 run（见下方 SQL）"
  echo "  4) python -m backend.cli refresh --date $DATE --indicator ma"
  echo "  5) python -m backend.cli refresh --date $DATE --dry-run"
  echo "  6) python -m backend.cli refresh --from $DATE --to $DATE --dry-run"
fi

if $RUN_ALL; then
  section "1. 同 day 二次 run（期望 pipeline_shortcut=true）"
  python3 -m backend.cli run --date "$DATE" --skip-export
  echo "--- 检查 run_fetch / run_rebuild_dwd ---"
  duck_sql "
  SELECT step_name, row_count, started_at,
         left(data_completeness, 160) AS completeness_preview
  FROM ods_etl_log
  WHERE step_name IN ('run_fetch','run_rebuild_dwd')
    AND data_completeness IS NOT NULL AND data_completeness != ''
  ORDER BY started_at DESC
  LIMIT 4
  "
fi

section "2. 手动 ODS 变更连锁（人工）"
cat <<'MANUAL'
在 DuckDB 中改一行 ODS（示例，请换成真实 ts_code/trade_date）:

  UPDATE ods_daily SET close = close + 0.01
  WHERE ts_code = '000001.SZ' AND trade_date = 'ANALYSIS_DATE';

再执行:
  python -m backend.cli run --date ANALYSIS_DATE --skip-export

期望 ods_etl_log.run_fetch.data_completeness.ods_rows_written >= 1
     且 run_rebuild_dwd 非 pipeline_shortcut（或 stale_count > 0）
MANUAL
echo '（将 ANALYSIS_DATE 替换为 '"$DATE"'）'

section "3. spec stale → run 仅 ma FULL（可选，需 bump MACalculator.SPEC_VERSION 后）"
echo "见 runbook「算法 SPEC_VERSION 发布」；本 smoke 可跳过。"

if $RUN_ALL; then
  section "4. refresh 仅 ma"
  python3 -m backend.cli refresh --date "$DATE" --indicator ma
  duck_sql "
  SELECT 'dws_ma_daily' AS tbl, COUNT(*) AS n
  FROM dws_ma_daily WHERE calc_date = '$DATE'
  UNION ALL
  SELECT 'dws_ma_weekly', COUNT(*) FROM dws_ma_weekly WHERE calc_date = '$DATE'
  "
fi

section "5. refresh dry-run（12 路由规模，只读）"
python3 -m backend.cli refresh --date "$DATE" --dry-run

section "6. refresh 范围 dry-run"
python3 -m backend.cli refresh --from "$DATE" --to "$DATE" --dry-run

section "7. health_check（只读）"
python3 scripts/health_check.py || true

echo
echo "smoke 脚本完成。请人工核对 pipeline_shortcut / ods_rows_written / refresh dry-run 输出。"
