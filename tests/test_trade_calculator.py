import pytest
from unittest.mock import MagicMock
from src.trade_calculator import TradeCalculator
import MetaTrader5 as mt5

@pytest.fixture
def calculator():
    mock_config = MagicMock()
    mock_config.get.return_value = 'fixed'
    mock_config.getfloat.side_effect = lambda section, key, fallback=None: {
        ('Trading', 'fixed_lot_size'): 0.02,
        ('Trading', 'default_lot_size'): 0.01,
    }.get((section, key), fallback if fallback is not None else 0.01)
    mock_config.get_entry_price_offset.return_value = 4.0  # Mock offset method
    mock_fetcher = MagicMock()
    # Symbol info mock
    symbol_info = MagicMock()
    symbol_info.volume_min = 0.01
    symbol_info.volume_max = 100.0
    symbol_info.volume_step = 0.01
    symbol_info.digits = 2
    mock_fetcher.get_symbol_info.return_value = symbol_info
    return TradeCalculator(mock_config, mock_fetcher)

def test_calculate_fixed_lot_size(calculator):
    lot = calculator.calculate_lot_size({})
    assert lot == 0.02

def test_calculate_sl_buy(calculator):
    sl = calculator.calculate_sl_from_distance('XAUUSD', mt5.ORDER_TYPE_BUY, 2000.0, 5.0)
    assert sl == 1995.0

def test_calculate_sl_sell(calculator):
    sl = calculator.calculate_sl_from_distance('XAUUSD', mt5.ORDER_TYPE_SELL, 2000.0, 5.0)
    assert sl == 2005.0

def test_calculate_tp_buy(calculator):
    tp = calculator.calculate_tp_from_distance('XAUUSD', mt5.ORDER_TYPE_BUY, 2000.0, 10.0)
    assert tp == 2010.0

def test_calculate_tp_sell(calculator):
    tp = calculator.calculate_tp_from_distance('XAUUSD', mt5.ORDER_TYPE_SELL, 2000.0, 10.0)
    assert tp == 1990.0

def test_calculate_adjusted_entry_buy(calculator):
    price = calculator.calculate_adjusted_entry_price(2000.0, 'BUY', 0.5)
    assert price > 2000.0

def test_calculate_adjusted_entry_sell(calculator):
    price = calculator.calculate_adjusted_entry_price(2000.0, 'SELL', 0.5)
    assert price < 2000.0

def test_calculate_trailing_sl_buy(calculator):
    sl = calculator.calculate_trailing_sl_price('XAUUSD', mt5.ORDER_TYPE_BUY, 2000.0, 5.0)
    assert sl == 1995.0

def test_calculate_trailing_sl_sell(calculator):
    sl = calculator.calculate_trailing_sl_price('XAUUSD', mt5.ORDER_TYPE_SELL, 2000.0, 5.0)
    assert sl == 2005.0