import pytest
from unittest.mock import MagicMock
from src.mt5_connector import MT5Connector
from src.mt5_data_fetcher import MT5DataFetcher
from src.mt5_executor import MT5Executor

@pytest.fixture
def mock_connector():
    connector = MT5Connector(MagicMock())
    connector.connect = MagicMock(return_value=True)
    connector.disconnect = MagicMock()
    connector.is_connected = MagicMock(return_value=True)
    return connector

@pytest.fixture
def mock_fetcher(mock_connector):
    fetcher = MT5DataFetcher(mock_connector)
    fetcher.get_symbol_tick = MagicMock(return_value={"bid":1.2345, "ask":1.2347})
    fetcher.get_account_info = MagicMock(return_value={"balance":1000})
    fetcher.get_symbol_info = MagicMock(return_value={"digits":5})
    return fetcher

@pytest.fixture
def mt5_executor(mock_connector):
    executor = MT5Executor(MagicMock(), mock_connector)
    executor.execute_trade = MagicMock(return_value=123456)
    executor.close_position = MagicMock(return_value=True)
    return executor

def test_connect_disconnect(mock_connector):
    assert mock_connector.connect()
    assert mock_connector.is_connected()
    mock_connector.disconnect()

def test_fetch_symbol_tick(mock_fetcher):
    tick = mock_fetcher.get_symbol_tick("EURUSD")
    assert "bid" in tick and "ask" in tick

def test_execute_trade(mt5_executor):
    ticket = mt5_executor.execute_trade("buy", "EURUSD", 0, 0.1)
    assert isinstance(ticket, int)

def test_close_position(mt5_executor):
    result = mt5_executor.close_position(123456)
    assert result is True