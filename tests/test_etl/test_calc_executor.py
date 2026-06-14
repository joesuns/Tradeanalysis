from backend.etl.calc_executor import build_work_queue, group_by_indicator


def test_build_work_queue_splits_by_indicator_not_stock():
    stock_modes = {
        "A.SZ": {
            ("macd", "daily"): ("SKIP", []),
            ("macd", "weekly"): ("FULL", []),
            ("ma", "daily"): ("APPEND", ["20260608"]),
        },
    }
    q = build_work_queue(stock_modes)
    assert ("A.SZ", ("macd", "weekly")) in q.full_items
    assert ("A.SZ", ("ma", "daily"), ["20260608"]) in q.append_items
    assert ("A.SZ", ("macd", "daily")) in q.skip_items
    assert "A.SZ" in q.full_stocks


def test_build_work_queue_respects_completed_keys():
    stock_modes = {
        "A.SZ": {
            ("macd", "daily"): ("APPEND", ["20260608"]),
            ("ma", "daily"): ("FULL", []),
        },
    }
    completed = {("A.SZ", "macd", "daily")}
    q = build_work_queue(stock_modes, completed_keys=completed)
    assert not q.append_items
    assert ("A.SZ", ("ma", "daily")) in q.full_items


def test_group_by_indicator():
    q = build_work_queue({
        "A.SZ": {("macd", "daily"): ("APPEND", ["d1"])},
        "B.SZ": {("macd", "daily"): ("APPEND", ["d1"])},
    })
    groups = group_by_indicator(q.append_items)
    assert groups[("macd", "daily")] == ["A.SZ", "B.SZ"]


def test_group_by_indicator_splits_weekly_full_by_indicator():
    """macd weekly FULL and kpattern weekly FULL land in separate groups."""
    q = build_work_queue({
        "A.SZ": {
            ("macd", "weekly"): ("FULL", []),
            ("kpattern", "weekly"): ("FULL", []),
        },
    })
    groups = group_by_indicator(q.full_items)
    assert set(groups.keys()) == {("macd", "weekly"), ("kpattern", "weekly")}
    assert groups[("macd", "weekly")] == ["A.SZ"]
    assert groups[("kpattern", "weekly")] == ["A.SZ"]


def test_full_items_count_vs_unique_stocks():
    """One stock with two FULL indicators → two work items, one stock."""
    q = build_work_queue({
        "A.SZ": {
            ("macd", "weekly"): ("FULL", []),
            ("kpattern", "weekly"): ("FULL", []),
        },
    })
    assert len(q.full_items) == 2
    assert len(q.full_stocks) == 1
