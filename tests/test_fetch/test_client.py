from unittest.mock import patch, MagicMock
import pytest


class _FakeClock:
    """Deterministic clock: time only advances when sleep() is called."""
    def __init__(self):
        self.t = 1000.0
        self.sleeps = []

    def now(self):
        return self.t

    def sleep(self, secs):
        self.sleeps.append(secs)
        self.t += secs


def test_interface_limiter_allows_up_to_limit_without_sleep():
    from backend.fetch.client import _InterfaceRateLimiter
    clk = _FakeClock()
    lim = _InterfaceRateLimiter(limit_per_min=3, window=60.0)
    for _ in range(3):
        lim.acquire("daily", now_fn=clk.now, sleep_fn=clk.sleep)
    assert clk.sleeps == [], "first N calls within window must not sleep"


def test_interface_limiter_blocks_when_exceeding_limit():
    from backend.fetch.client import _InterfaceRateLimiter
    clk = _FakeClock()
    lim = _InterfaceRateLimiter(limit_per_min=3, window=60.0)
    for _ in range(3):
        lim.acquire("daily", now_fn=clk.now, sleep_fn=clk.sleep)
    # 4th call must wait until the oldest timestamp leaves the 60s window
    lim.acquire("daily", now_fn=clk.now, sleep_fn=clk.sleep)
    assert len(clk.sleeps) >= 1
    assert clk.sleeps[0] == pytest.approx(60.0, abs=0.1)


def test_interface_limiter_is_per_interface():
    from backend.fetch.client import _InterfaceRateLimiter
    clk = _FakeClock()
    lim = _InterfaceRateLimiter(limit_per_min=2, window=60.0)
    lim.acquire("daily", now_fn=clk.now, sleep_fn=clk.sleep)
    lim.acquire("daily", now_fn=clk.now, sleep_fn=clk.sleep)
    # different interface has its own budget — no sleep
    lim.acquire("moneyflow", now_fn=clk.now, sleep_fn=clk.sleep)
    lim.acquire("moneyflow", now_fn=clk.now, sleep_fn=clk.sleep)
    assert clk.sleeps == []


@patch.dict('os.environ', {'TUSHARE_TOKEN': 'test'})
@patch('backend.fetch.client.ts.pro_api')
def test_clients_share_one_process_limiter(mock_pro):
    """All TushareClient instances share a single process-wide limiter."""
    mock_pro.return_value = MagicMock()
    from backend.fetch.client import TushareClient
    a = TushareClient()
    b = TushareClient()
    assert a._limiter is b._limiter


@patch.dict('os.environ', {'TUSHARE_TOKEN': 'test'})
@patch('backend.fetch.client.ts.pro_api')
def test_retry_on_failure_then_succeed(mock_pro):
    mock = MagicMock()
    mock.daily.side_effect = [Exception("timeout"), Exception("timeout"),
                               MagicMock(empty=False, to_dict=lambda _: [{"ts_code": "TEST"}])]
    mock_pro.return_value = mock
    from backend.fetch.client import TushareClient
    client = TushareClient()
    results = client.call("daily", ts_code="TEST")
    assert len(results) == 1
    assert mock.daily.call_count == 3


@patch.dict('os.environ', {'TUSHARE_TOKEN': 'test'})
@patch('backend.fetch.client.ts.pro_api')
def test_empty_response_returns_empty_list(mock_pro):
    mock = MagicMock()
    mock.daily.return_value = None
    mock_pro.return_value = mock
    from backend.fetch.client import TushareClient
    client = TushareClient()
    assert client.call("daily", ts_code="NONE") == []


@patch.dict('os.environ', {'TUSHARE_TOKEN': 'test'})
@patch('backend.fetch.client.ts.pro_api')
def test_invalid_api_name_raises(mock_pro):
    mock = MagicMock(spec=[])  # spec=[] prevents auto-creating attrs; getattr returns None
    mock_pro.return_value = mock
    from backend.fetch.client import TushareClient
    client = TushareClient()
    with pytest.raises(ValueError, match="Unknown tushare API"):
        client.call("nonexistent_api")
