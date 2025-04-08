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
    signal_data.action = "buy"
    signal_data.symbol = "EURUSD"
    await event_processor.process_new_signal(
        signal_data,
        message_id=1,
        state_manager=mock_dependencies["state_manager"]
    )
    # Expect some interaction with mt5_executor or telegram_sender
    mock_dependencies["state_manager"].add_active_trade.assert_called_once()

@pytest.mark.asyncio
async def test_process_update(mock_dependencies):
    analysis_result = {"type": "update", "data": {}}
    await event_processor.process_update(
        analysis_result,
        event=MagicMock(),
        state_manager=mock_dependencies["state_manager"]
    )
    # Should handle update without exceptions