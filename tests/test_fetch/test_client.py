from unittest.mock import patch, MagicMock
import pytest


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
