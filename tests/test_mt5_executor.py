import pytest
from unittest.mock import MagicMock
from src.mt5_executor import MT5Executor
import MetaTrader5 as mt5

import MetaTrader5 as mt5

@pytest.fixture(autouse=True)
def mock_mt5(monkeypatch):
    # Mock symbol_info
    mock_symbol_info = MagicMock()
    mock_symbol_info.point = 0.01
    mock_symbol_info.digits = 2
    monkeypatch.setattr(mt5, "symbol_info", lambda symbol: mock_symbol_info)

    # Mock symbol_info_tick
    mock_tick = MagicMock()
    mock_tick.ask = 2000.2
    mock_tick.bid = 2000.0
    monkeypatch.setattr(mt5, "symbol_info_tick", lambda symbol: mock_tick)

    # Mock order_send to always succeed
    mock_result = MagicMock()
    mock_result.retcode = mt5.TRADE_RETCODE_DONE
    mock_result.order = 123456
    mock_result.deal = 123456
    mock_result.price = 2000.1
    mock_result.comment = "Success"
    mock_result.request_id = 1
    monkeypatch.setattr(mt5, "order_send", lambda req: mock_result)

    # Mock positions_get and orders_get
    monkeypatch.setattr(mt5, "positions_get", lambda *args, **kwargs: [MagicMock(ticket=123, sl=0, tp=0, type=mt5.ORDER_TYPE_BUY, symbol='XAUUSD')])
    monkeypatch.setattr(mt5, "orders_get", lambda *args, **kwargs: [])

@pytest.fixture
def executor():
    mock_config = MagicMock()
    mock_config.get.return_value = 'XAUUSD'
    mock_config.getint.return_value = 3
    mock_config.getfloat.return_value = 4.0
    mock_connector = MagicMock()
    return MT5Executor(mock_config, mock_connector)

def test_adjust_sl_for_buy(executor):
    sl = 2000.0
    order_type = mt5.ORDER_TYPE_BUY
    symbol = 'XAUUSD'
    executor.sl_offset_pips = 4.0
    adjusted = executor._adjust_sl_for_spread_offset(sl, order_type, symbol)
    assert adjusted < sl

def test_adjust_sl_for_sell(executor):
    sl = 2000.0
    order_type = mt5.ORDER_TYPE_SELL
    symbol = 'XAUUSD'
    executor.sl_offset_pips = 4.0
    adjusted = executor._adjust_sl_for_spread_offset(sl, order_type, symbol)
    assert adjusted > sl

def test_execute_trade_calls_send_order(executor):
    executor._send_order_with_retry = MagicMock(return_value=('result', 2000.0))
    result = executor.execute_trade('BUY', 'XAUUSD', mt5.ORDER_TYPE_BUY, 0.01)
    assert result[0] == 'result'

def test_modify_trade_calls_order_send(executor):
    executor.connector.ensure_connection.return_value = True
    mt5.positions_get = MagicMock(return_value=[MagicMock(ticket=123, sl=0, tp=0, type=mt5.ORDER_TYPE_BUY, symbol='XAUUSD')])
    mt5.order_send = MagicMock(return_value=MagicMock(retcode=mt5.TRADE_RETCODE_DONE))
    success = executor.modify_trade(123, sl=2000.0, tp=2010.0)
    assert success

def test_close_position_calls_order_send(executor):
    executor.connector.ensure_connection.return_value = True
    mt5.positions_get = MagicMock(return_value=[MagicMock(ticket=123, volume=0.01, type=mt5.ORDER_TYPE_BUY, symbol='XAUUSD')])
    mt5.symbol_info_tick = MagicMock(return_value=MagicMock(ask=2000.0, bid=1999.0))
    mt5.order_send = MagicMock(return_value=MagicMock(retcode=mt5.TRADE_RETCODE_DONE))
    success = executor.close_position(123)
    assert success