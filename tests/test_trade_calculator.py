import pytest
from unittest.mock import MagicMock
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from src.trade_calculator import TradeCalculator
import MetaTrader5 as mt5

@pytest.fixture
def calculator():
    mock_config = MagicMock()
    mock_config.get.return_value = 'default'
    mock_config.getfloat.side_effect = lambda section, key, fallback=None: {
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
    symbol_info.point = 0.01 # Add mock value for point
    mock_fetcher.get_symbol_info.return_value = symbol_info
    return TradeCalculator(mock_config, mock_fetcher)

# Removed obsolete test for fixed lot size mode (no longer supported)

def test_calculate_sl_buy(calculator):
    # Simulate pips-to-price conversion: 40 pips * 0.1 = 4.0 price units
    sl = calculator.calculate_sl_from_distance('XAUUSD', mt5.ORDER_TYPE_BUY, 2000.0, 40.0) # Corrected pip distance
    assert sl == 1996.0 # Reverted: 40 pips = 4.0 price distance for XAUUSD (User Def)

def test_calculate_sl_sell(calculator):
    sl = calculator.calculate_sl_from_distance('XAUUSD', mt5.ORDER_TYPE_SELL, 2000.0, 40.0) # Corrected pip distance
    assert sl == 2004.0 # Reverted: 40 pips = 4.0 price distance for XAUUSD (User Def)

def test_calculate_tp_buy(calculator):
    # Test with 100 pips distance (100 * 0.01 * 10 = 10.0 price distance)
    tp = calculator.calculate_tp_from_distance('XAUUSD', mt5.ORDER_TYPE_BUY, 2000.0, 100.0)
    assert tp == 2010.0 # Reverted: 100 pips = 10.0 price distance for XAUUSD (User Def)

def test_calculate_tp_sell(calculator):
    # Test with 100 pips distance (100 * 0.01 * 10 = 10.0 price distance)
    tp = calculator.calculate_tp_from_distance('XAUUSD', mt5.ORDER_TYPE_SELL, 2000.0, 100.0)
    assert tp == 1990.0 # Reverted: 100 pips = 10.0 price distance for XAUUSD (User Def)

def test_calculate_adjusted_entry_buy(calculator):
    price = calculator.calculate_adjusted_entry_price('XAUUSD', 2000.0, 'BUY', 0.5)
    assert price > 2000.0

def test_calculate_adjusted_entry_sell(calculator):
    price = calculator.calculate_adjusted_entry_price('XAUUSD', 2000.0, 'SELL', 0.5)
    assert price < 2000.0

def test_calculate_trailing_sl_buy(calculator):
    sl = calculator.calculate_trailing_sl_price('XAUUSD', mt5.ORDER_TYPE_BUY, 2000.0, 5.0)
    assert sl == 1999.5 # Reverted: 5 pips = 0.5 price distance for XAUUSD (User Def)

def test_calculate_trailing_sl_sell(calculator):
    sl = calculator.calculate_trailing_sl_price('XAUUSD', mt5.ORDER_TYPE_SELL, 2000.0, 5.0)
    assert sl == 2000.5 # Reverted: 5 pips = 0.5 price distance for XAUUSD (User Def)

def test_calculate_sl_from_pips_buy(calculator):
    # 40 pips * 0.01 * 10 = 4.0, BUY: 2000.0 - 4.0 = 1996.0
    import MetaTrader5 as mt5
    sl = calculator.calculate_sl_from_pips('XAUUSD', mt5.ORDER_TYPE_BUY, 2000.0, 40.0)
    assert sl == 1996.0 # Reverted: 40 pips = 4.0 price distance for XAUUSD (User Def)

def test_calculate_sl_from_pips_sell(calculator):
    # 40 pips * 0.01 * 10 = 4.0, SELL: 2000.0 + 4.0 = 2004.0
    import MetaTrader5 as mt5
    sl = calculator.calculate_sl_from_pips('XAUUSD', mt5.ORDER_TYPE_SELL, 2000.0, 40.0)
    assert sl == 2004.0 # Reverted: 40 pips = 4.0 price distance for XAUUSD (User Def)

def test_calculate_sl_from_pips_zero_distance(calculator):
    # Should return None for zero pips
    import MetaTrader5 as mt5
    sl = calculator.calculate_sl_from_pips('XAUUSD', mt5.ORDER_TYPE_BUY, 2000.0, 0.0)
    assert sl is None

def test_calculate_sl_from_pips_invalid_order_type(calculator):
    # Should return None for invalid order type
    sl = calculator.calculate_sl_from_pips('XAUUSD', 999, 2000.0, 40.0)
    assert sl is None

def test_calculate_sl_from_pips_missing_symbol_info(calculator):
    # Should return None if symbol_info is missing
    calculator.fetcher.get_symbol_info.return_value = None
    import MetaTrader5 as mt5
    sl = calculator.calculate_sl_from_pips('XAUUSD', mt5.ORDER_TYPE_BUY, 2000.0, 40.0)
    assert sl is None