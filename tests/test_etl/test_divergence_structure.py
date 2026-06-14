"""MACD/DDE structure divergence unit tests + TDX golden alignment (Task 0 scaffold)."""
import csv
from datetime import datetime
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
MACD_GOLDEN = FIXTURES / "tdx_macd_structure_golden.csv"
DDE_GOLDEN = FIXTURES / "tdx_dde_structure_golden.csv"

_GOLDEN_PENDING = "golden pending manual TDX labels"


def _load_golden(path: Path) -> list:
    """Load golden CSV rows; return [] if file missing."""
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _date_within_tolerance(hits, expect_date, tol=1) -> bool:
    """True if any hit date is within tol calendar days of expect_date (YYYYMMDD)."""
    if not hits:
        return False
    expect_dt = datetime.strptime(str(expect_date), "%Y%m%d")
    for h in hits:
        hit_str = str(h).replace("-", "")[:8]
        hit_dt = datetime.strptime(hit_str, "%Y%m%d")
        if abs((hit_dt - expect_dt).days) <= tol:
            return True
    return False


def test_date_within_tolerance_helper():
    """Smoke test for golden date matching helper."""
    assert _date_within_tolerance(["20240315"], "20240315", tol=1)
    assert _date_within_tolerance(["20240316"], "20240315", tol=1)
    assert not _date_within_tolerance(["20240317"], "20240315", tol=1)
    assert not _date_within_tolerance([], "20240315", tol=1)


def test_mdif_part_matches_tongdaxin():
    """MDIF INTPART normalization matches Tongdaxin."""
    from backend.etl.divergence_structure import mdif_part  # noqa: F401

    assert mdif_part(0.123, ref_peak=0.456) == int(0.123 / 0.1)
    assert mdif_part(-0.05, ref_peak=-0.08) == int(-0.05 / 0.01)


def test_cross_up_detects_golden_cross():
    """cross_up detects fast crossing above slow."""
    import numpy as np
    from backend.etl.divergence_structure import cross_up

    fast = np.array([0.0, 0.1, 0.2, 0.15])
    slow = np.array([0.05, 0.08, 0.18, 0.16])
    assert cross_up(fast, slow, 1) is True
    assert cross_up(fast, slow, 0) is False


def test_cross_down_detects_death_cross():
    """cross_down detects fast crossing below slow."""
    import numpy as np
    from backend.etl.divergence_structure import cross_down

    fast = np.array([0.2, 0.15, 0.1, 0.05])
    slow = np.array([0.18, 0.16, 0.12, 0.08])
    assert cross_down(fast, slow, 1) is True
    assert cross_down(fast, slow, 0) is False


@pytest.mark.skip(reason=_GOLDEN_PENDING)
@pytest.mark.parametrize(
    "row",
    _load_golden(MACD_GOLDEN),
    ids=lambda r: f"{r['ts_code']}_{r['trade_date']}",
)
def test_macd_structure_matches_tdx_golden(row, db_with_schema):
    """结构形成日与通达信 golden 对齐（±1 bar 容差）。"""
    ts_code = row["ts_code"]
    expect_date = row["trade_date"]
    expect = row["divergence"]
    from backend.etl.divergence_golden_io import load_macd_daily_frame

    df = load_macd_daily_frame(db_with_schema, ts_code, row.get("freq", "daily"))
    out = df
    hits = out.loc[out["divergence"] == expect, "trade_date"].tolist()
    assert _date_within_tolerance(hits, expect_date, tol=1), (
        f"{ts_code}: expected {expect} near {expect_date}, got {hits}"
    )


@pytest.mark.skip(reason=_GOLDEN_PENDING)
@pytest.mark.parametrize(
    "row",
    _load_golden(DDE_GOLDEN),
    ids=lambda r: f"{r['ts_code']}_{r['trade_date']}",
)
def test_dde_structure_matches_tdx_golden(row, db_with_schema):
    """DDE 结构形成日与通达信 golden 对齐（±1 bar 容差）。"""
    ts_code = row["ts_code"]
    expect_date = row["trade_date"]
    expect = row["divergence"]
    from backend.etl.divergence_golden_io import load_dde_daily_frame

    df = load_dde_daily_frame(db_with_schema, ts_code, row.get("freq", "daily"))
    out = df
    hits = out.loc[out["divergence"] == expect, "trade_date"].tolist()
    assert _date_within_tolerance(hits, expect_date, tol=1), (
        f"{ts_code}: expected {expect} near {expect_date}, got {hits}"
    )


def _synthetic_macd_top_scenario():
    """Two golden-cross waves: wave2 price higher, DIF/MACD bar weaker → TG top."""
    import numpy as np

    n = 60
    close = np.full(n, 10.0)
    dif = np.full(n, -0.01)
    dea = np.full(n, 0.0)
    macd = np.full(n, -0.01)

    for i in range(5):
        dif[i] = -0.02
        dea[i] = 0.0
        macd[i] = -0.01
    dif[4], dea[4] = -0.01, 0.0
    dif[5], dea[5] = 0.01, 0.0  # golden cross 1

    for i in range(5, 15):
        close[i] = 10.0 + (i - 5) * 0.1
        dif[i] = 0.02 + (i - 5) * 0.008
        dea[i] = 0.01 + (i - 5) * 0.004
        macd[i] = 0.01 + (i - 5) * 0.004

    for i in range(15, 25):
        close[i] = 11.0 - (i - 14) * 0.05
        dif[i] = 0.10 - (i - 15) * 0.012
        dea[i] = 0.08 - (i - 15) * 0.005
        macd[i] = 0.05 - (i - 15) * 0.005

    dif[24], dea[24] = 0.02, 0.03
    dif[25], dea[25] = 0.04, 0.03  # golden cross 2

    for i in range(25, 38):
        close[i] = 10.5 + (i - 25) * 0.08
        dif[i] = 0.03 + (i - 25) * 0.003
        dea[i] = 0.02 + (i - 25) * 0.002
        macd[i] = 0.01 + (i - 25) * 0.0015

    for i in range(38, 45):
        close[i] = 11.46 + (i - 38) * 0.01
        dif[i] = 0.066 - (i - 38) * 0.005
        dea[i] = 0.048
        macd[i] = 0.028 - (i - 38) * 0.0005

    return close, dif, dea, macd


def _synthetic_macd_bottom_scenario():
    """Two death-cross waves: wave2 price lower, DIF/MACD bar less negative → TG bottom."""
    import numpy as np

    n = 60
    close = np.full(n, 10.0)
    dif = np.full(n, 0.01)
    dea = np.full(n, 0.0)
    macd = np.full(n, 0.01)

    for i in range(5):
        dif[i] = 0.02
        dea[i] = 0.0
        macd[i] = 0.01
    dif[4], dea[4] = 0.01, 0.0
    dif[5], dea[5] = -0.01, 0.0  # death cross 1

    for i in range(5, 15):
        close[i] = 10.0 - (i - 5) * 0.1
        dif[i] = -0.02 - (i - 5) * 0.008
        dea[i] = -0.01 - (i - 5) * 0.004
        macd[i] = -0.01 - (i - 5) * 0.004

    for i in range(15, 25):
        close[i] = 9.0 + (i - 14) * 0.05
        dif[i] = -0.10 + (i - 15) * 0.012
        dea[i] = -0.08 + (i - 15) * 0.005
        macd[i] = -0.05 + (i - 15) * 0.005

    dif[24], dea[24] = 0.02, 0.03
    dif[25], dea[25] = -0.02, 0.01  # death cross 2

    for i in range(25, 38):
        close[i] = 9.5 - (i - 25) * 0.08
        dif[i] = -0.03 - (i - 25) * 0.003
        dea[i] = -0.02 - (i - 25) * 0.002
        macd[i] = -0.01 - (i - 25) * 0.0015

    for i in range(38, 45):
        close[i] = 8.54 - (i - 38) * 0.01
        dif[i] = -0.066 + (i - 38) * 0.005
        dea[i] = -0.048
        macd[i] = -0.028 + (i - 38) * 0.0005

    return close, dif, dea, macd


def test_macd_direct_top_structure_forms_on_tg_day():
    """Synthetic two-wave red-zone scenario must emit top_divergence on TG day only."""
    from backend.etl.divergence_structure import compute_macd_structure_divergence

    close, dif, dea, macd = _synthetic_macd_top_scenario()
    result = compute_macd_structure_divergence(close, dif, dea, macd, dedup=10)
    tg_days = [i for i, v in enumerate(result) if v == "top_divergence"]
    assert len(tg_days) >= 1
    assert result[tg_days[0] - 1] is None


def test_macd_bottom_structure_forms():
    """Symmetric bottom scenario must emit bottom_divergence on TG day."""
    from backend.etl.divergence_structure import compute_macd_structure_divergence

    close, dif, dea, macd = _synthetic_macd_bottom_scenario()
    result = compute_macd_structure_divergence(close, dif, dea, macd, dedup=10)
    tg_days = [i for i, v in enumerate(result) if v == "bottom_divergence"]
    assert len(tg_days) >= 1
    assert result[tg_days[0] - 1] is None


def test_macd_target_indices_subset_of_full():
    """target_indices only writes TG labels at requested bars; full path unchanged elsewhere."""
    from backend.etl.divergence_structure import compute_macd_structure_divergence

    close, dif, dea, macd = _synthetic_macd_top_scenario()
    full = compute_macd_structure_divergence(close, dif, dea, macd, dedup=10)
    tg_days = [i for i, v in enumerate(full) if v is not None]
    assert tg_days
    partial = compute_macd_structure_divergence(
        close, dif, dea, macd, dedup=10, target_indices={tg_days[-1]},
    )
    for i, v in enumerate(full):
        if i == tg_days[-1]:
            assert partial[i] == v
        else:
            assert partial[i] is None


def test_macd_no_divergence_on_t_only():
    """钝化日 T 不写入 DWS；仅结构形成 TG 日标注。"""
    from backend.etl.divergence_structure import compute_macd_structure_divergence

    close, dif, dea, macd = _synthetic_macd_top_scenario()
    result = compute_macd_structure_divergence(close, dif, dea, macd, dedup=10)
    tg_day = next(i for i, v in enumerate(result) if v == "top_divergence")
    t_day = tg_day - 1
    assert result[t_day] is None
    assert result[tg_day] == "top_divergence"
