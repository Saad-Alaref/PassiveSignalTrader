import pytest
from unittest.mock import AsyncMock, MagicMock
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
    pos = MagicMock()
    pos.volume = 0.02
    pos.profit = 5.0
    trade_info = MagicMock()
    trade_info.auto_be_applied = False
    await trade_manager.check_and_apply_auto_be(pos, trade_info)

@pytest.mark.asyncio
async def test_check_and_apply_trailing_stop(trade_manager):
    pos = MagicMock()
    trade_info = MagicMock()
    trade_info.tsl_active = False
    await trade_manager.check_and_apply_trailing_stop(pos, trade_info)

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