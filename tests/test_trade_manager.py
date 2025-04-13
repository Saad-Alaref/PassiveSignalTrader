import pytest
from unittest.mock import AsyncMock, MagicMock
import MetaTrader5 as mt5 # Import the mt5 library for constants
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from src.trade_manager import TradeManager

@pytest.fixture
def trade_manager():
    tm = TradeManager(
        MagicMock(),  # config_service
        MagicMock(),  # state_manager
        MagicMock(),  # mt5_executor
        MagicMock(),  # trade_calculator
        MagicMock(),  # telegram_sender
        MagicMock()   # mt5_fetcher
    )
    tm.mt5_executor.modify_trade = MagicMock(return_value=True)
    tm.mt5_executor.modify_sl_to_breakeven = MagicMock(return_value=True)
    tm.mt5_executor.close_position = MagicMock(return_value=True)
    tm.telegram_sender.send_message = AsyncMock(return_value=True)

    # Remove the generic side_effect for getfloat from the fixture.
    # Specific mocks will be added in individual tests.
    # tm.config_service.getfloat.side_effect = lambda *args, **kwargs: 1.0

    return tm

@pytest.mark.asyncio
async def test_check_and_apply_auto_sl(trade_manager):
    pos = MagicMock()
    trade_info = MagicMock()
    # Use a datetime for auto_sl_pending_timestamp to match production logic
    from datetime import datetime, timezone, timedelta
    trade_info.auto_sl_pending_timestamp = datetime.now(timezone.utc) - timedelta(seconds=31)  # Simulate pending flag, delay passed
    pos.sl = 0.0
    pos.type = mt5.ORDER_TYPE_BUY
    pos.volume = 0.02
    pos.price_open = 2000.0
    pos.symbol = "XAUUSD"
    trade_info.ticket = 12345

    # Mock config to use pips
    trade_manager.config_service.getboolean.return_value = True  # enable_auto_sl
    trade_manager.config_service.getint.side_effect = lambda section, key, fallback=None: 30 if key == 'auto_sl_delay_seconds' else fallback
    trade_manager.config_service.getfloat.side_effect = lambda section, key, fallback=None: 40.0 if key == 'auto_sl_risk_pips' else fallback

    # Mock trade_calculator to check correct pip-based SL calculation
    def mock_calc_sl_from_pips(symbol, order_type, entry_price, sl_distance_pips):
        # Should receive sl_distance_pips = 40.0
        assert symbol == "XAUUSD"
        assert order_type == mt5.ORDER_TYPE_BUY
        assert entry_price == 2000.0
        assert sl_distance_pips == 40.0
        return 1996.0  # Expected SL for BUY

    trade_manager.trade_calculator.calculate_sl_from_pips = MagicMock(side_effect=mock_calc_sl_from_pips)

    trade_manager.mt5_executor.modify_trade.return_value = True
    trade_manager.telegram_sender.send_message = AsyncMock(return_value=True)

    await trade_manager.check_and_apply_auto_sl(pos, trade_info)
    # Assert that calculate_sl_from_pips was called
    trade_manager.trade_calculator.calculate_sl_from_pips.assert_called_once_with(
        symbol="XAUUSD", order_type=mt5.ORDER_TYPE_BUY, entry_price=2000.0, sl_distance_pips=40.0
    )
    # Assert that modify_trade was called with the returned SL
    trade_manager.mt5_executor.modify_trade.assert_called_once_with(ticket=12345, sl=1996.0)

@pytest.mark.asyncio
async def test_check_and_apply_auto_be(trade_manager):
    # --- Mocking for AutoBE ---
    # Mock position details
    pos = MagicMock()
    pos.ticket = 12345
    pos.volume = 0.02
    pos.profit = 6.0 # Ensure profit meets threshold (e.g., 3.0 * (0.02/0.01) = 6.0)
    pos.sl = 0.0 # Assume no initial SL
    pos.price_open = 2000.0
    pos.type = mt5.ORDER_TYPE_BUY # Example type
    pos.symbol = "XAUUSD"

    # Mock trade info
    trade_info = MagicMock()
    trade_info.auto_be_applied = False
    trade_info.ticket = 12345
    trade_info.entry_price = pos.price_open # Mock adjusted entry price

    # Mock fetcher results needed for BE calculation
    mock_tick = MagicMock()
    mock_tick.ask = 2003.50 # Set ask > bid for a positive spread
    mock_tick.bid = 2003.00 # Set bid to 2003.0 so profit distance = 3.0 (activation threshold)
    trade_manager.mt5_fetcher.get_symbol_tick.return_value = mock_tick

    mock_symbol_info = MagicMock()
    mock_symbol_info.point = 0.01
    mock_symbol_info.digits = 2
    trade_manager.mt5_fetcher.get_symbol_info.return_value = mock_symbol_info

    # Mock config values used in the function
    trade_manager.config_service.getboolean.return_value = True # enable_auto_be
    # Mock config values explicitly for this test
    trade_manager.config_service.getboolean.return_value = True # enable_auto_be
    # Define a specific side_effect function for getfloat
    def mock_getfloat_autobe(section, key, fallback=None):
        if section == 'AutoBE' and key == 'auto_be_profit_pips':
            return 30.0
        if section == 'Trading' and key == 'sl_offset_pips':
            return 2.0
        # Return fallback or a default float if key not matched
        return fallback if fallback is not None else 0.0
    trade_manager.config_service.getfloat.side_effect = mock_getfloat_autobe

    # Mock executor success
    trade_manager.mt5_executor.modify_trade.return_value = True
    # --- End Mocking ---

    # Add logging before the call
    print("\n[Test] Calling check_and_apply_auto_be...")
    print(f"[Test] Mock config getfloat side_effect: {trade_manager.config_service.getfloat.side_effect}")
    print(f"[Test] Mock tick bid: {trade_manager.mt5_fetcher.get_symbol_tick.return_value.bid}")
    print(f"[Test] Mock symbol point: {trade_manager.mt5_fetcher.get_symbol_info.return_value.point}")
    print(f"[Test] Mock pos price_open: {pos.price_open}")

    # Print the values used for the activation check
    point = trade_manager.mt5_fetcher.get_symbol_info.return_value.point
    digits = trade_manager.mt5_fetcher.get_symbol_info.return_value.digits
    profit_pips_threshold = 30.0
    required_price_distance = round(profit_pips_threshold * (point * 10), digits)
    entry_price = pos.price_open
    relevant_market_price = trade_manager.mt5_fetcher.get_symbol_tick.return_value.bid
    current_price_distance_profit = relevant_market_price - entry_price
    print(f"[Test] required_price_distance: {required_price_distance}, current_price_distance_profit: {current_price_distance_profit}")

    await trade_manager.check_and_apply_auto_be(pos, trade_info)

    # Add logging after the call
    print("[Test] Finished check_and_apply_auto_be call.")
    print(f"[Test] modify_trade call count: {trade_manager.mt5_executor.modify_trade.call_count}")

    # Assert that modify_trade was called
    trade_manager.mt5_executor.modify_trade.assert_called_once()

    # Assert the correct SL was passed to modify_trade
    # Expected BE SL = entry + spread + offset_price
    # spread = ask - bid = 2000.50 - 2000.00 = 0.50
    # offset_price = offset_pips * point * 10 = 2.0 * 0.01 * 10 = 0.20
    # be_sl = 2000.0 + 0.50 + 0.20 = 2000.70
    trade_manager.mt5_executor.modify_trade.assert_called_with(ticket=12345, sl=2000.70)
    # Assert that the auto_be_applied flag was set
    assert trade_info.auto_be_applied is True

@pytest.mark.asyncio
async def test_check_and_apply_trailing_stop(trade_manager):
    # --- Mocking for TSL ---
    # Mock position details
    pos = MagicMock()
    pos.ticket = 54321
    pos.volume = 0.01
    pos.profit = 10.0 # Example profit
    pos.sl = 0.0
    pos.price_open = 2000.0
    pos.type = mt5.ORDER_TYPE_BUY
    pos.symbol = "XAUUSD"

    # Mock trade info
    trade_info = MagicMock()
    trade_info.tsl_active = False
    trade_info.ticket = 54321
    trade_info.entry_price = pos.price_open # Mock adjusted entry price

    # Mock fetcher results needed for TSL calculation
    mock_tick = MagicMock()
    mock_tick.ask = 2070.50 # Price moved significantly in profit ( > 60.0 activation distance)
    mock_tick.bid = 2070.00
    trade_manager.mt5_fetcher.get_symbol_tick.return_value = mock_tick

    mock_symbol_info = MagicMock()
    mock_symbol_info.point = 0.01
    mock_symbol_info.digits = 2
    trade_manager.mt5_fetcher.get_symbol_info.return_value = mock_symbol_info

    # Mock config values used in the function
    trade_manager.config_service.getboolean.return_value = True # enable_trailing_stop
    trade_manager.config_service.getfloat.side_effect = lambda section, key, fallback=None: {
        ('TrailingStop', 'activation_profit_pips'): 60.0, # e.g., 6 pips = 0.6 price distance
        ('TrailingStop', 'trail_distance_pips'): 20.0, # e.g., 2 pips = 0.2 price distance
        # ('Trading', 'base_lot_size_for_usd_targets'): 0.01, # No longer needed for pip TSL
    }.get((section, key), fallback)

    # Mock calculator result (optional, but good practice)
    # Expected TSL = current_bid - trail_distance_price
    # Expected TSL = 2070.00 - (20.0 pips * 1.0 price/pip) = 2070.00 - 20.0 = 2050.0
    trade_manager.trade_calculator.calculate_trailing_sl_price.return_value = 2050.0
    # Mock the new pips_to_price_distance call for activation threshold
    # Expected activation distance = 60 pips * 1.0 price/pip (for XAUUSD) = 60.0
    trade_manager.trade_calculator.pips_to_price_distance.return_value = 60.0

    # Mock executor methods used in the function
    trade_manager.mt5_executor.modify_trade.return_value = True
    # Mock the adjustment function to return a plausible float
    # Example: entry(2000) - spread(0.5) - offset(0.2) = 1999.3 for BUY
    trade_manager.mt5_executor._adjust_sl_for_spread_offset.return_value = 1999.3
    # --- End Mocking ---

    await trade_manager.check_and_apply_trailing_stop(pos, trade_info)

    # Assert that modify_trade was called (basic check for activation)
    trade_manager.mt5_executor.modify_trade.assert_called_once()
    # Assert that the TSL flag was set
    assert trade_info.tsl_active is True
    # Assert the correct SL was passed to modify_trade
    trade_manager.mt5_executor.modify_trade.assert_called_with(ticket=54321, sl=2050.0)

# Removed obsolete test_check_and_handle_tp_hits: function no longer exists in TradeManager.