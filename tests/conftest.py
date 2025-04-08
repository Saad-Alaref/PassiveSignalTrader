import pytest
from unittest.mock import MagicMock, AsyncMock

@pytest.fixture
def mock_mt5_connector():
    connector = MagicMock()
    connector.connect.return_value = True
    connector.is_connected.return_value = True
    return connector

@pytest.fixture
def mock_mt5_executor():
    executor = MagicMock()
    executor.execute_trade.return_value = 123456
    executor.close_position.return_value = True
    return executor

@pytest.fixture
def mock_telegram_sender():
    sender = MagicMock()
    sender.send_message = AsyncMock(return_value=True)
    sender.send_confirmation_message = AsyncMock(return_value=True)
    sender.edit_message = AsyncMock(return_value=True)
    return sender