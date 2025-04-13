import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import asyncio

from src.trade_execution_strategies import DistributedLimitsStrategy

@pytest.mark.asyncio
async def test_distributed_limits_strategy_execute_calls_dependencies():
    # Arrange: mock dependencies
    mock_mt5_executor = MagicMock()
    mock_trade_calculator = MagicMock()
    mock_state_manager = MagicMock()
    mock_duplicate_checker = MagicMock()
    mock_config_service = MagicMock()
    mock_mt5_fetcher = MagicMock()
    mock_symbol_info = MagicMock()
    mock_symbol_info.volume_min = 0.01
    mock_symbol_info.volume_step = 0.01
    mock_symbol_info.digits = 5
    mock_mt5_fetcher.get_symbol_info.return_value = mock_symbol_info
    mock_tick = MagicMock()
    mock_tick.ask = 1.2350
    mock_tick.bid = 1.2340
    mock_mt5_fetcher.get_symbol_tick.return_value = mock_tick
    mock_telegram_sender = MagicMock()
    mock_telegram_sender.send_message = AsyncMock()
    mock_signal_data = {
        "symbol": "EURUSD",
        "order_type": 0,
        "entry_price": 1.2345,
        "sl_distance_pips": 20,
        "tp_distance_pips": 40,
        "volume": 0.1,
    }
    # Patch methods that would interact with external systems
    mock_trade_calculator.calculate_adjusted_entry_price.return_value = 1.2345
    mock_mt5_executor.execute_trade.return_value = (MagicMock(order=123456), None)
    # Create instance
    strategy = DistributedLimitsStrategy(
        entry_price_raw="1.2345-1.2345",
        tp_assignment_config={"mode": "none"},
        signal_data=mock_signal_data,
        mt5_executor=mock_mt5_executor,
        trade_calculator=mock_trade_calculator,
        state_manager=mock_state_manager,
        config_service=mock_config_service,
        telegram_sender=mock_telegram_sender,
        duplicate_checker=mock_duplicate_checker,
        mt5_fetcher=mock_mt5_fetcher,
        lot_size=0.1,
        base_split_lot=0.01,
        numeric_tps=[1.0],
        action="BUY",
        trade_symbol="EURUSD",
    )
    # Patch async methods if any
    if hasattr(strategy, "send_confirmation_message"):
        strategy.send_confirmation_message = AsyncMock()
    # Act
    await strategy.execute()
    # Assert: check that dependencies were called as expected
    assert mock_trade_calculator.calculate_adjusted_entry_price.called
    assert mock_mt5_executor.execute_trade.called