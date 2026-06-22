"""Tests for portfolio stock sheet in Excel export."""
import io
import os
import tempfile

import duckdb
import pandas as pd
import pytest
from openpyxl import load_workbook

from backend.cli import _load_portfolio_stocks
from backend.export_wide import (
    _SIGNAL_ONLY,
    _resolve_portfolio_remarks,
    export_wide_to_excel,
)


# ---- _load_portfolio_stocks tests ----

def test_load_portfolio_valid_xlsx():
    """加载合法的持仓股列表 xlsx 文件"""
    # Create temp xlsx
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["stockcode", "stockname"])
    ws.append(["603986", "兆易创新"])
    ws.append(["002709", "天赐材料"])
    tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
    wb.save(tmp.name)
    tmp.close()

    try:
        result = _load_portfolio_stocks(tmp.name)
        assert len(result) == 2
        assert result[0] == {"stockcode": "603986", "stockname": "兆易创新"}
        assert result[1] == {"stockcode": "002709", "stockname": "天赐材料"}
    finally:
        os.unlink(tmp.name)


def test_load_portfolio_case_insensitive_columns():
    """列名大小写不敏感匹配"""
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["StockCode", "StockName"])
    ws.append(["300346", "南大光电"])
    tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
    wb.save(tmp.name)
    tmp.close()

    try:
        result = _load_portfolio_stocks(tmp.name)
        assert len(result) == 1
        assert result[0]["stockcode"] == "300346"
    finally:
        os.unlink(tmp.name)


def test_load_portfolio_strips_whitespace():
    """值自动 strip 前后空格"""
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["stockcode", "stockname"])
    ws.append([" 603986 ", " 兆易创新 "])
    tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
    wb.save(tmp.name)
    tmp.close()

    try:
        result = _load_portfolio_stocks(tmp.name)
        assert result[0]["stockcode"] == "603986"
        assert result[0]["stockname"] == "兆易创新"
    finally:
        os.unlink(tmp.name)


def test_load_portfolio_missing_columns_warns_and_returns_empty(caplog):
    """缺少必需列时 WARNING + 返回空列表"""
    import openpyxl
    import logging
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["foo", "bar"])
    ws.append(["603986", "x"])
    tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
    wb.save(tmp.name)
    tmp.close()

    try:
        with caplog.at_level(logging.WARNING):
            result = _load_portfolio_stocks(tmp.name)
        assert result == []
        assert "stockcode" in caplog.text.lower() or "column" in caplog.text.lower()
    finally:
        os.unlink(tmp.name)


def test_load_portfolio_file_not_found_returns_empty(caplog):
    """文件不存在时 WARNING + 返回空列表"""
    import logging
    with caplog.at_level(logging.WARNING):
        result = _load_portfolio_stocks("/nonexistent/path.xlsx")
    assert result == []


# ---- _resolve_portfolio_remarks tests ----

def test_resolve_remarks_normal_stock():
    """有当日数据的股票 → '正常'"""
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE dim_stock (stock_code VARCHAR, delist_date VARCHAR)")
    con.execute("INSERT INTO dim_stock VALUES ('603986', NULL)")
    con.execute("INSERT INTO dim_stock VALUES ('002709', NULL)")

    daily_stock_codes = {"603986", "002709"}
    portfolio_codes = ["603986", "002709"]
    trade_date = "20260620"

    remarks = _resolve_portfolio_remarks(con, portfolio_codes, trade_date, daily_stock_codes)
    assert remarks["603986"] == "正常"
    assert remarks["002709"] == "正常"
    con.close()


def test_resolve_remarks_delisted():
    """已退市股票 → '已退市'"""
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE dim_stock (stock_code VARCHAR, delist_date VARCHAR)")
    con.execute("INSERT INTO dim_stock VALUES ('000001', '20250101')")

    remarks = _resolve_portfolio_remarks(con, ["000001"], "20260620", set())
    assert remarks["000001"] == "已退市"
    con.close()


def test_resolve_remarks_not_in_db():
    """dim_stock 中不存在的股票 → '未入库'"""
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE dim_stock (stock_code VARCHAR, delist_date VARCHAR)")

    remarks = _resolve_portfolio_remarks(con, ["999999"], "20260620", set())
    assert remarks["999999"] == "未入库"
    con.close()


def test_resolve_remarks_suspended():
    """未退市但无当日行情 → '当日无数据（可能停牌）'"""
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE dim_stock (stock_code VARCHAR, delist_date VARCHAR)")
    con.execute("INSERT INTO dim_stock VALUES ('603986', NULL)")

    remarks = _resolve_portfolio_remarks(con, ["603986"], "20260620", set())
    assert "停牌" in remarks["603986"]
    con.close()


def test_resolve_remarks_mixed():
    """混合场景：正常 + 停牌 + 退市 + 未入库"""
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE dim_stock (stock_code VARCHAR, delist_date VARCHAR)")
    con.execute("INSERT INTO dim_stock VALUES ('A', NULL)")
    con.execute("INSERT INTO dim_stock VALUES ('B', NULL)")
    con.execute("INSERT INTO dim_stock VALUES ('C', '20250101')")
    # D not inserted → 未入库

    remarks = _resolve_portfolio_remarks(
        con, ["A", "B", "C", "D"], "20260620", {"A"},
    )
    assert remarks["A"] == "正常"
    assert "停牌" in remarks["B"]
    assert remarks["C"] == "已退市"
    assert remarks["D"] == "未入库"
    con.close()
