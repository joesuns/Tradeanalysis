"""Fetch index data from tushare — index_basic, index_daily, index_dailybasic."""
import json
import logging
import yaml
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)


# ── config loader ────────────────────────────────────────────

def load_index_config() -> dict:
    """Load config/indices.yaml, return {ts_code: name} merged from core+sector."""
    cfg_path = Path(__file__).parent.parent.parent / "config" / "indices.yaml"
    with open(cfg_path) as f:
        cfg = yaml.safe_load(f)
    result: dict = {}
    for group in ("core", "sector"):
        for item in cfg.get("indices", {}).get(group, []):
            result[item["ts_code"]] = item["name"]
    return result


def _get_tracked_codes() -> list:
    """Return list of tracked index ts_codes."""
    return list(load_index_config().keys())


# ── helpers ──────────────────────────────────────────────────

def _sub_calendar_days(date_str: str, days: int) -> str:
    """Subtract N calendar days from YYYYMMDD, return YYYYMMDD."""
    dt = datetime.strptime(date_str, "%Y%m%d") - timedelta(days=days)
    return dt.strftime("%Y%m%d")


def _next_day(date_str: str) -> str:
    """Add 1 calendar day."""
    return (datetime.strptime(date_str, "%Y%m%d") + timedelta(days=1)).strftime("%Y%m%d")


def _today_str() -> str:
    return datetime.now().strftime("%Y%m%d")


# ── fetch functions ──────────────────────────────────────────

def fetch_index_basic(client, con) -> int:
    """Fetch index metadata for tracked indices. UPSERT into ods_index_basic.

    Uses tushare index_basic API (2000pt required). Each tracked index is
    fetched individually to capture full metadata.
    """
    codes = _get_tracked_codes()
    if not codes:
        logger.warning("progress fetch.index_basic: no tracked indices in config")
        return 0

    logger.info("progress fetch.index_basic: fetching %d indices", len(codes))
    rows = 0
    for ts_code in codes:
        recs = client.call("index_basic", ts_code=ts_code)
        for r in recs:
            con.execute("""INSERT OR REPLACE INTO ods_index_basic
                (ts_code, name, fullname, market, publisher, category,
                 base_date, base_point, list_date, exp_date, weight_rule,
                 raw_json, fetched_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,now())""",
                (r["ts_code"], r.get("name", ""), r.get("fullname", ""),
                 r.get("market", ""), r.get("publisher", ""), r.get("category", ""),
                 r.get("base_date", ""), r.get("base_point"), r.get("list_date", ""),
                 r.get("exp_date", ""), r.get("weight_rule", ""),
                 json.dumps(r, ensure_ascii=False)))
            rows += 1
    logger.info("progress fetch.index_basic: done | rows=%d", rows)
    return rows


def fetch_index_daily(client, con, trade_date: str = None) -> int:
    """Fetch index daily OHLCV. Progressive backfill: 250 bars first, then incremental.

    If ods_index_daily is empty for an index → pull last ~400 calendar days (≈250 tdays).
    Otherwise → pull from MAX(trade_date) + 1 to trade_date.
    """
    codes = _get_tracked_codes()
    if not codes:
        return 0

    total_rows = 0
    for ts_code in codes:
        existing = con.execute(
            "SELECT MAX(trade_date) FROM ods_index_daily WHERE ts_code = ?",
            [ts_code]
        ).fetchone()[0]

        if existing is None:
            end_date = trade_date or con.execute(
                "SELECT MAX(cal_date) FROM ods_trade_cal WHERE is_open=1"
            ).fetchone()[0]
            start_date = _sub_calendar_days(end_date, 400)
            logger.info("progress fetch.index_daily: %s warmup %s→%s",
                        ts_code, start_date, end_date)
        else:
            if trade_date and existing >= trade_date:
                logger.debug("progress fetch.index_daily: %s up to date (%s)",
                             ts_code, existing)
                continue
            start_date = _next_day(existing)
            end_date = trade_date or _today_str()
            logger.info("progress fetch.index_daily: %s incremental %s→%s",
                        ts_code, start_date, end_date)

        recs = client.call("index_daily", ts_code=ts_code,
                           start_date=start_date, end_date=end_date)
        for r in recs:
            con.execute("""INSERT OR REPLACE INTO ods_index_daily
                (ts_code, trade_date, close, open, high, low, pre_close,
                 change, pct_chg, vol, amount, fetched_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,now())""",
                (r["ts_code"], r["trade_date"],
                 r.get("close"), r.get("open"), r.get("high"), r.get("low"),
                 r.get("pre_close"), r.get("change"), r.get("pct_chg"),
                 r.get("vol"), r.get("amount")))
        total_rows += len(recs)

    logger.info("progress fetch.index_daily: done | total_rows=%d", total_rows)
    return total_rows


def fetch_index_dailybasic(client, con, trade_date: str = None) -> int:
    """Fetch index valuation data. Only ~6 core indices have data; others return [].

    Uses tushare index_dailybasic API (400pt required).
    Progressive backfill same pattern as fetch_index_daily.
    """
    codes = _get_tracked_codes()
    if not codes:
        return 0

    total_rows = 0
    for ts_code in codes:
        existing = con.execute(
            "SELECT MAX(trade_date) FROM ods_index_dailybasic WHERE ts_code = ?",
            [ts_code]
        ).fetchone()[0]

        if existing is None:
            end_date = trade_date or con.execute(
                "SELECT MAX(cal_date) FROM ods_trade_cal WHERE is_open=1"
            ).fetchone()[0]
            start_date = _sub_calendar_days(end_date, 400)
        else:
            if trade_date and existing >= trade_date:
                continue
            start_date = _next_day(existing)
            end_date = trade_date or _today_str()

        recs = client.call("index_dailybasic", ts_code=ts_code,
                           start_date=start_date, end_date=end_date)
        for r in recs:
            con.execute("""INSERT OR REPLACE INTO ods_index_dailybasic
                (ts_code, trade_date, total_mv, float_mv, total_share,
                 float_share, free_share, turnover_rate, turnover_rate_f,
                 pe, pe_ttm, pb, fetched_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,now())""",
                (r["ts_code"], r["trade_date"],
                 r.get("total_mv"), r.get("float_mv"), r.get("total_share"),
                 r.get("float_share"), r.get("free_share"),
                 r.get("turnover_rate"), r.get("turnover_rate_f"),
                 r.get("pe"), r.get("pe_ttm"), r.get("pb")))
        total_rows += len(recs)

    logger.info("progress fetch.index_dailybasic: done | total_rows=%d", total_rows)
    return total_rows
