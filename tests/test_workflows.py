import pytest
from unittest.mock import AsyncMock, MagicMock
import asyncio
from src import event_processor

@pytest.fixture
def mock_dependencies():
    deps = {
        "state_manager": MagicMock(),
        "mt5_executor": MagicMock(),
        "telegram_sender": MagicMock(),
    }
    deps["telegram_sender"].send_message = AsyncMock()
    deps["mt5_executor"].execute_trade = MagicMock(return_value=123456)
    return deps

@pytest.mark.asyncio
async def test_process_new_signal(mock_dependencies):
    signal_data = MagicMock()
    signal_data.is_signal = True
    signal_data.action = "BUY"
    signal_data.entry_type = "Pending"
    signal_data.entry_price = "3100-3102"
    signal_data.stop_loss = 3095
    signal_data.take_profits = [3110, 3120]
    signal_data.symbol = "XAUUSD"
    signal_data.sentiment_score = 0.8

    await event_processor.process_new_signal(
        signal_data,
        1,  # message_id
        mock_dependencies["state_manager"],
        MagicMock(),  # decision_logic
        MagicMock(),  # trade_calculator
        mock_dependencies["mt5_executor"],
        mock_dependencies["telegram_sender"],
        MagicMock(),  # duplicate_checker
        MagicMock(),  # config_service_instance
        "TestPrefix",
        mock_dependencies["mt5_fetcher"] if "mt5_fetcher" in mock_dependencies else MagicMock()
    )

@pytest.mark.asyncio
async def test_process_update(mock_dependencies):
    update_data = MagicMock()
    update_data.update_type = "modify_sltp"
    update_data.target_trade_index = 1
    update_data.new_stop_loss = 3095
    update_data.new_take_profits = [3110, 3120]

    await event_processor.process_update(
        update_data,
        MagicMock(),  # event with .id attribute
        mock_dependencies["state_manager"],
        MagicMock(),  # signal_analyzer
        mock_dependencies["mt5_executor"],
        mock_dependencies["telegram_sender"],
        MagicMock(),  # duplicate_checker
        MagicMock(),  # config_service_instance
        "TestPrefix",
        {},  # llm_context dummy
        None  # image_data dummy
    )