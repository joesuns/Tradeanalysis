from fastapi.testclient import TestClient
from backend.api.app import app
from backend.db.schema import create_all_tables

client = TestClient(app)


def test_health_endpoint(temp_db):
    """Health endpoint returns 200 with database=connected."""
    create_all_tables(temp_db)
    # Monkey-patch get_connection to return temp_db
    import backend.api.router as router_module

    original = router_module.get_connection
    router_module.get_connection = lambda read_only=False: temp_db
    try:
        response = client.get("/api/v1/health")
        assert response.status_code == 200
        data = response.json()
        assert data["database"] == "connected"
        assert "latest_trade_date" in data
        assert "freshness" in data
        assert "table_stats" in data
    finally:
        router_module.get_connection = original


def test_analysis_404_for_unknown_stock(temp_db):
    """Unknown stock returns 404 with STOCK_NOT_FOUND detail."""
    create_all_tables(temp_db)
    import backend.api.router as router_module

    original = router_module.get_connection
    router_module.get_connection = lambda read_only=False: temp_db
    try:
        response = client.get("/api/v1/analysis/999999.XZ")
        assert response.status_code == 404
        detail = response.json()["detail"]
        assert detail["code"] == "STOCK_NOT_FOUND"
    finally:
        router_module.get_connection = original


def test_analysis_returns_stock_data(temp_db):
    """Known stock with data returns 200 with MACD and quote info."""
    create_all_tables(temp_db)
    # Insert required dimensional and fact data
    temp_db.execute(
        "INSERT INTO dim_stock (ts_code, stock_code, name) "
        "VALUES ('000001.SZ', '000001', '平安银行')"
    )
    temp_db.execute(
        "INSERT INTO dwd_daily_quote (ts_code, trade_date, close_qfq, pct_chg) "
        "VALUES ('000001.SZ', '20260101', 12.5, 1.5)"
    )
    temp_db.execute(
        "INSERT INTO dws_macd_daily (ts_code, trade_date, calc_date, dif, dea, macd_bar, zone, trend, divergence, turning_point, alert) "
        "VALUES ('000001.SZ', '20260101', '20260101', 0.5, 0.3, 0.2, 'bull', 'up', NULL, 'golden_cross', NULL)"
    )
    # Create the latest view the query depends on
    temp_db.execute("""CREATE VIEW IF NOT EXISTS v_dws_macd_daily_latest AS
        SELECT * FROM dws_macd_daily d WHERE calc_date = (
            SELECT MAX(calc_date) FROM dws_macd_daily
            WHERE ts_code = d.ts_code AND trade_date = d.trade_date)""")

    import backend.api.router as router_module

    original = router_module.get_connection
    router_module.get_connection = lambda read_only=False: temp_db
    try:
        response = client.get("/api/v1/analysis/000001.SZ?freq=daily")
        assert response.status_code == 200
        data = response.json()
        assert data["ts_code"] == "000001.SZ"
        assert data["stock_code"] == "000001"
        assert data["stock_name"] == "平安银行"
        assert data["close"] == 12.5
        assert data["macd"]["dif"] == 0.5
        assert data["macd"]["zone"] == "bull"
        assert "freshness" in data
    finally:
        router_module.get_connection = original


def test_analysis_history_returns_count(temp_db):
    """History endpoint returns row count for a stock."""
    create_all_tables(temp_db)
    temp_db.execute(
        "INSERT INTO dim_stock (ts_code, stock_code, name) "
        "VALUES ('000001.SZ', '000001', '平安银行')"
    )
    # Need dws_macd_daily for the view
    temp_db.execute(
        "INSERT INTO dws_macd_daily (ts_code, trade_date, calc_date, dif, dea, macd_bar, zone, trend) "
        "VALUES ('000001.SZ', '20260101', '20260101', 0.5, 0.3, 0.2, 'bull', 'up')"
    )
    temp_db.execute("""CREATE VIEW IF NOT EXISTS v_dws_macd_daily_latest AS
        SELECT * FROM dws_macd_daily d WHERE calc_date = (
            SELECT MAX(calc_date) FROM dws_macd_daily
            WHERE ts_code = d.ts_code AND trade_date = d.trade_date)""")

    import backend.api.router as router_module

    original = router_module.get_connection
    router_module.get_connection = lambda read_only=False: temp_db
    try:
        response = client.get("/api/v1/analysis/000001.SZ/history")
        assert response.status_code == 200
        data = response.json()
        assert data["ts_code"] == "000001.SZ"
        assert data["freq"] == "daily"
        assert data["count"] >= 0
        assert "freshness" in data
    finally:
        router_module.get_connection = original


def test_screening_returns_empty_results(temp_db):
    """Screening endpoint returns empty result set when no data matches conditions."""
    create_all_tables(temp_db)
    import backend.api.router as router_module

    original = router_module.get_connection
    router_module.get_connection = lambda read_only=False: temp_db
    try:
        response = client.get("/api/v1/screening?macd_zone=bull&limit=10")
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 0
        assert data["results"] == []
        assert data["conditions"]["macd_zone"] == "bull"
        assert "freshness" in data
    finally:
        router_module.get_connection = original


def test_analysis_rejects_invalid_freq(temp_db):
    """Analysis rejects freq values other than daily/weekly."""
    import backend.api.router as router_module

    original = router_module.get_connection
    router_module.get_connection = lambda read_only=False: temp_db
    try:
        response = client.get("/api/v1/analysis/000001.SZ?freq=monthly")
        # FastAPI Query regex validation returns 422 for invalid params
        assert response.status_code == 422
    finally:
        router_module.get_connection = original
