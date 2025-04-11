import pytest
from unittest.mock import AsyncMock, MagicMock
import MetaTrader5 as mt5 # Import the mt5 library for constants
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

    # Patch config_service.getfloat to return floats
    tm.config_service.getfloat.side_effect = lambda *args, **kwargs: 1.0

    return tm

@pytest.mark.asyncio
async def test_check_and_apply_auto_sl(trade_manager):
    pos = MagicMock()
    trade_info = MagicMock()
    trade_info.auto_sl_pending_timestamp = None
    await trade_manager.check_and_apply_auto_sl(pos, trade_info)

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

    # Mock fetcher results needed for BE calculation
    mock_tick = MagicMock()
    mock_tick.ask = 2000.50
    mock_tick.bid = 2000.00
    trade_manager.mt5_fetcher.get_symbol_tick.return_value = mock_tick

    mock_symbol_info = MagicMock()
    mock_symbol_info.point = 0.01
    mock_symbol_info.digits = 2
    trade_manager.mt5_fetcher.get_symbol_info.return_value = mock_symbol_info

    # Mock config values used in the function
    trade_manager.config_service.getboolean.return_value = True # enable_auto_be
    trade_manager.config_service.getfloat.side_effect = lambda section, key, fallback=None: {
        ('AutoBE', 'auto_be_profit_usd'): 3.0,
        ('Trading', 'base_lot_size_for_usd_targets'): 0.01,
        ('Trading', 'sl_offset_pips'): 2.0, # Example offset
    }.get((section, key), fallback)

    # Mock executor success
    trade_manager.mt5_executor.modify_trade.return_value = True
    # --- End Mocking ---

    await trade_manager.check_and_apply_auto_be(pos, trade_info)

    # Assert that modify_trade was called (basic check)
    trade_manager.mt5_executor.modify_trade.assert_called_once()
    # More specific assertions could check the calculated SL value passed to modify_trade

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

    # Mock fetcher results needed for TSL calculation
    mock_tick = MagicMock()
    mock_tick.ask = 2010.50 # Price moved in profit
    mock_tick.bid = 2010.00
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
    # Expected TSL = current_bid - (trail_pips * point * 10)
    # Expected TSL = 2010.00 - (20.0 * 0.01 * 10) = 2010.00 - 2.0 = 2008.0
    trade_manager.trade_calculator.calculate_trailing_sl_price.return_value = 2008.0

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
    trade_manager.mt5_executor.modify_trade.assert_called_with(ticket=54321, sl=2008.0)

@pytest.mark.asyncio
async def test_check_and_handle_tp_hits(trade_manager):
    pos = MagicMock()
    trade_info = MagicMock()
    trade_info.all_tps = [3110, 3120]
    trade_info.next_tp_index = 0
    trade_info.original_volume = 0.02
    trade_info.symbol = "XAUUSD"
    trade_info.ticket = 123456
    trade_info.original_msg_id = 1
    await trade_manager.check_and_handle_tp_hits(pos, trade_info)