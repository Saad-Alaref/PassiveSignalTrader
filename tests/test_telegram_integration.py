import pytest
from unittest.mock import AsyncMock, MagicMock
from src.telegram_sender import TelegramSender

@pytest.fixture
def telegram_sender():
    sender = TelegramSender(MagicMock())
    sender.send_message = AsyncMock(return_value=True)
    sender.send_confirmation_message = AsyncMock(return_value=True)
    sender.edit_message = AsyncMock(return_value=True)
    return sender

@pytest.mark.asyncio
async def test_send_message(telegram_sender):
    result = await telegram_sender.send_message("Test message")
    assert result is True

@pytest.mark.asyncio
async def test_send_confirmation_message(telegram_sender):
    result = await telegram_sender.send_confirmation_message("conf123", {}, "Please confirm")
    assert result is True

@pytest.mark.asyncio
async def test_edit_message(telegram_sender):
    result = await telegram_sender.edit_message(12345, 67890, "Updated text")
    assert result is True