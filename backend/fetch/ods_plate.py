"""Fetch TDX industry plates and DC concept plates from tushare.

Data sources:
  - TDX (通达信): tdx_index(idx_type='行业板块') → tdx_member per board
  - DC  (东方财富): dc_index(idx_type='概念板块') → dc_member per board

TTL: per-source cache (TDX 7d / DC 3d) tracked via ods_plate_snapshot.
"""

import logging
import time
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Per-source tushare API config
_PLATE_SOURCES = {
    "tdx": {
        "index_func": "tdx_index",
        "member_func": "tdx_member",
        "idx_type": "行业板块",
        "ttl_days": 7,  # industry classification changes slowly
        "ts_code_field": "ts_code",
        "name_field": "name",
        "member_con_code_field": "con_code",
        "member_con_name_field": "con_name",
    },
    "dc": {
        "index_func": "dc_index",
        "member_func": "dc_member",
        "idx_type": "概念板块",
        "ttl_days": 3,  # concept membership rotates faster
        "ts_code_field": "ts_code",
        "name_field": "name",
        "member_con_code_field": "con_code",
        "member_con_name_field": "name",
    },
}


def _is_snapshot_fresh(con, trade_date: str, source: str, idx_type: str,
                       ttl_days: int = 7) -> bool:
    """Check if a valid snapshot exists within *ttl_days* for (trade_date, source, idx_type).

    Returns False for snapshots with n_boards=0 — an empty result at fetch time
    does not guarantee the API will still return empty later (e.g. queried before
    market close on a trading day, or on a weekend).
    """
    cutoff = (datetime.now() - timedelta(days=ttl_days)).strftime("%Y-%m-%d %H:%M:%S")
    row = con.execute(
        """SELECT fetched_at, n_boards FROM ods_plate_snapshot
           WHERE trade_date = ? AND source = ? AND idx_type = ?""",
        [trade_date, source, idx_type]
    ).fetchone()
    if row is None:
        return False
    fetched_at, n_boards = row
    if n_boards == 0:
        return False
    return fetched_at >= cutoff


def _count_members_for_date(con, trade_date: str, source: str) -> int:
    """Count existing member rows for a given date+source (fast stale check)."""
    row = con.execute(
        "SELECT COUNT(*) FROM ods_plate_member WHERE trade_date = ? AND source = ?",
        [trade_date, source]
    ).fetchone()
    return row[0] if row else 0


def _fetch_boards(client, trade_date: str, source_cfg: dict) -> list[dict]:
    """Fetch board list from tushare index API. Returns list of board dicts."""
    records = client.call(
        source_cfg["index_func"],
        trade_date=trade_date,
        idx_type=source_cfg["idx_type"],
    )
    ts_code_field = source_cfg["ts_code_field"]
    name_field = source_cfg["name_field"]
    boards = []
    for r in records:
        ts_code = r.get(ts_code_field, "")
        name = r.get(name_field, "")
        if ts_code and name:
            boards.append({"board_ts_code": ts_code, "board_name": name})
    return boards


def _fetch_members_for_board(client, trade_date: str, board_ts_code: str,
                              source_cfg: dict) -> list[dict]:
    """Fetch member stocks for a single board. Returns list of {con_code, con_name}."""
    records = client.call(
        source_cfg["member_func"],
        trade_date=trade_date,
        ts_code=board_ts_code,
    )
    con_code_field = source_cfg["member_con_code_field"]
    con_name_field = source_cfg["member_con_name_field"]
    members = []
    for r in records:
        con_code = r.get(con_code_field, "")
        con_name = r.get(con_name_field, "")
        if con_code:
            members.append({"con_code": con_code, "con_name": con_name or ""})
    return members


def fetch_plate_data(client, con, trade_date: str) -> dict:
    """Fetch TDX + DC plate members for a trade_date. TTL-cached; degraded on failure.

    Returns dict: {source: {n_boards, n_members, cached, error}}
    """
    results = {}
    ts_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for source, cfg in _PLATE_SOURCES.items():
        idx_type = cfg["idx_type"]
        result = {"n_boards": 0, "n_members": 0, "cached": False, "error": None}

        # TTL gate
        if _is_snapshot_fresh(con, trade_date, source, idx_type, ttl_days=cfg["ttl_days"]):
            n = _count_members_for_date(con, trade_date, source)
            result["n_members"] = n
            result["cached"] = True
            logger.info(
                "progress fetch.plate: %s %s cache hit | members=%d",
                source, idx_type, n,
            )
            results[source] = result
            continue

        logger.info(
            "progress fetch.plate: %s %s cache miss | fetching...",
            source, idx_type,
        )
        t_start = time.monotonic()

        try:
            # Step A: fetch board list
            boards = _fetch_boards(client, trade_date, cfg)
            result["n_boards"] = len(boards)
            logger.info(
                "progress fetch.plate: %s %s boards=%d",
                source, idx_type, len(boards),
            )

            # Step B: fetch members per board
            total_members = 0
            for i, b in enumerate(boards):
                try:
                    members = _fetch_members_for_board(
                        client, trade_date, b["board_ts_code"], cfg,
                    )
                    # UPSERT board
                    con.execute(
                        """INSERT OR REPLACE INTO ods_plate_board
                           (trade_date, source, board_ts_code, board_name, fetched_at)
                           VALUES (?, ?, ?, ?, ?)""",
                        [trade_date, source, b["board_ts_code"], b["board_name"], ts_now],
                    )
                    # UPSERT members
                    for m in members:
                        con.execute(
                            """INSERT OR REPLACE INTO ods_plate_member
                               (trade_date, source, board_ts_code, con_code, con_name, fetched_at)
                               VALUES (?, ?, ?, ?, ?, ?)""",
                            [trade_date, source, b["board_ts_code"],
                             m["con_code"], m["con_name"], ts_now],
                        )
                        total_members += 1
                except Exception as e:
                    logger.warning(
                        "fetch.plate: %s member %s failed: %s",
                        source, b["board_ts_code"], e,
                    )
                    continue

                # Progress heartbeat every 100 boards
                if (i + 1) % 100 == 0:
                    logger.info(
                        "progress fetch.plate: %s %d/%d boards | members=%d",
                        source, i + 1, len(boards), total_members,
                    )

            result["n_members"] = total_members

            # Step C: write snapshot meta record
            con.execute(
                """INSERT OR REPLACE INTO ods_plate_snapshot
                   (trade_date, source, idx_type, n_boards, n_members, fetched_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                [trade_date, source, idx_type, len(boards), total_members, ts_now],
            )

            elapsed = time.monotonic() - t_start
            logger.info(
                "progress fetch.plate: %s %s done | boards=%d members=%d | %.0fs",
                source, idx_type, len(boards), total_members, elapsed,
            )

        except Exception as e:
            result["error"] = str(e)
            logger.warning(
                "fetch.plate: %s %s degraded: %s",
                source, idx_type, e,
            )

        results[source] = result

    return results


def load_plate_enrichment(con, trade_date: str) -> dict[str, dict[str, str]]:
    """Load plate enrichment for export.

    Returns dict[ts_code -> {'tdx_industry_board': '...', 'dc_concept_board': '...'}].

    If no plate data exists for trade_date, returns empty dicts for all stocks.
    """
    enrichment = {}

    # TDX industry plates → tdx_industry_board column
    tdx_rows = con.execute(
        """SELECT m.con_code AS ts_code,
                  STRING_AGG(DISTINCT b.board_name, ',' ORDER BY b.board_name) AS boards
           FROM ods_plate_member m
           JOIN ods_plate_board b ON m.trade_date = b.trade_date
               AND m.source = b.source
               AND m.board_ts_code = b.board_ts_code
           WHERE m.trade_date = ? AND m.source = 'tdx'
           GROUP BY m.con_code""",
        [trade_date],
    ).fetchall()
    for ts_code, boards in tdx_rows:
        enrichment.setdefault(ts_code, {})["tdx_industry_board"] = boards

    # DC concept plates → dc_concept_board column
    dc_rows = con.execute(
        """SELECT m.con_code AS ts_code,
                  STRING_AGG(DISTINCT b.board_name, ',' ORDER BY b.board_name) AS boards
           FROM ods_plate_member m
           JOIN ods_plate_board b ON m.trade_date = b.trade_date
               AND m.source = b.source
               AND m.board_ts_code = b.board_ts_code
           WHERE m.trade_date = ? AND m.source = 'dc'
           GROUP BY m.con_code""",
        [trade_date],
    ).fetchall()
    for ts_code, boards in dc_rows:
        enrichment.setdefault(ts_code, {})["dc_concept_board"] = boards

    return enrichment
