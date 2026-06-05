"""Tests for fetch_stocks.py — path building helper."""

from fetch_stocks import _build_output_path, fix_ts_code


class TestBuildOutputPath:
    """Verify _build_output_path produces paths under exports/ directory."""

    def test_no_output_generates_exports_prefixed_path(self):
        """When output is None, result starts with exports/ and includes analysis date."""
        path = _build_output_path("20260604", output=None)
        assert path.startswith("exports/"), f"Expected exports/ prefix, got: {path}"
        assert "20260604" in path, f"Expected analysis date in filename, got: {path}"
        assert path.endswith(".xlsx"), f"Expected .xlsx extension, got: {path}"

    def test_no_output_generates_timestamp_in_path(self):
        """Auto-generated path contains the export date and a timestamp pattern."""
        path = _build_output_path("20260604", output=None)
        # Path format: exports/analysis_{date}_gen{YYYYMMDD_HHMMSS}.xlsx
        assert "exports/analysis_20260604_gen" in path
        # Verify timestamp part is present (14 chars: YYYYMMDD_HHMMSS)
        basename = path.split("/")[-1]
        assert basename.endswith(".xlsx")
        # e.g. analysis_20260604_gen20260604_174313.xlsx
        parts = basename.replace(".xlsx", "").split("_gen")
        assert len(parts) == 2
        assert len(parts[1]) == 15  # YYYYMMDD_HHMMSS

    def test_explicit_output_passed_through(self):
        """When output is explicitly given, it is returned unchanged."""
        path = _build_output_path("20260604", output="custom/path/report.xlsx")
        assert path == "custom/path/report.xlsx"

    def test_explicit_output_with_exports_prefix_stays(self):
        """When output already includes exports/, it stays as-is."""
        path = _build_output_path("20260604", output="exports/my_report.xlsx")
        assert path == "exports/my_report.xlsx"


class TestFixTsCode:
    """Verify stock code auto-completion logic."""

    def test_shenzhen_prefix_000(self):
        assert fix_ts_code("000543") == "000543.SZ"

    def test_shenzhen_prefix_002(self):
        assert fix_ts_code("002709") == "002709.SZ"

    def test_shenzhen_prefix_300(self):
        assert fix_ts_code("300750") == "300750.SZ"

    def test_shanghai_prefix_600(self):
        assert fix_ts_code("600580") == "600580.SH"

    def test_shanghai_prefix_603(self):
        assert fix_ts_code("603986") == "603986.SH"

    def test_shanghai_prefix_688(self):
        assert fix_ts_code("688981") == "688981.SH"

    def test_beijing_prefix_8(self):
        assert fix_ts_code("838402") == "838402.BJ"

    def test_already_full_code(self):
        assert fix_ts_code("000543.SZ") == "000543.SZ"

    def test_invalid_code_returns_none(self):
        assert fix_ts_code("abc123") is None
        assert fix_ts_code("12") is None
