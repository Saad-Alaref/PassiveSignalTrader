import pytest
from unittest.mock import MagicMock, patch

from src.state_manager import StateManager

@pytest.fixture
def config_service():
    mock = MagicMock()
    mock.getint.return_value = 10
    return mock

@pytest.fixture
def state_manager(config_service):
    return StateManager(config_service)

def test_add_active_trade_valid(state_manager):
    trade_data = {
        'ticket': 1001,
        'symbol': 'XAUUSD',
        'open_time': '2024-01-01T00:00:00Z',
        'original_msg_id': 1,
        'original_volume': 0.1,
        'entry_price': 2000.0,
        'initial_sl': 1990.0,
        'assigned_tp': 2010.0
    }
    state_manager.add_active_trade(trade_data)
    assert len(state_manager.bot_active_trades) == 1
    assert state_manager.bot_active_trades[0].ticket == 1001

def test_add_active_trade_duplicate(state_manager):
    trade_data = {
        'ticket': 1002,
        'symbol': 'XAUUSD',
        'open_time': '2024-01-01T00:00:00Z',
        'original_msg_id': 2,
        'original_volume': 0.1,
        'entry_price': 2000.0,
        'initial_sl': 1990.0,
        'assigned_tp': 2010.0
    }
    state_manager.add_active_trade(trade_data)
    state_manager.add_active_trade(trade_data)  # Duplicate
    assert len(state_manager.bot_active_trades) == 1

def test_add_active_trade_missing_key(state_manager):
    trade_data = {
        'symbol': 'XAUUSD',
        'open_time': '2024-01-01T00:00:00Z',
        'original_msg_id': 3,
        'original_volume': 0.1,
        'entry_price': 2000.0,
        'initial_sl': 1990.0,
        'assigned_tp': 2010.0
    }
    state_manager.add_active_trade(trade_data)
    assert len(state_manager.bot_active_trades) == 0

@patch("src.state_manager.mt5")
def test_remove_inactive_trades_removes_closed_trades(mock_mt5, state_manager):
    # Setup: Add two trades, only one is still active on MT5
    trade1 = {
        'ticket': 2001,
        'symbol': 'XAUUSD',
        'open_time': '2024-01-01T00:00:00Z',
        'original_msg_id': 4,
        'original_volume': 0.1,
        'entry_price': 2000.0,
        'initial_sl': 1990.0,
        'assigned_tp': 2010.0
    }
    trade2 = {
        'ticket': 2002,
        'symbol': 'XAUUSD',
        'open_time': '2024-01-01T00:00:00Z',
        'original_msg_id': 5,
        'original_volume': 0.1,
        'entry_price': 2000.0,
        'initial_sl': 1990.0,
        'assigned_tp': 2010.0
    }
    state_manager.add_active_trade(trade1)
    state_manager.add_active_trade(trade2)
    assert len(state_manager.bot_active_trades) == 2

    # Mock MT5: Only trade1 is still open
    mock_mt5.terminal_info.return_value = True
    mock_mt5.positions_get.return_value = [MagicMock(ticket=2001)]
    mock_mt5.orders_get.return_value = []
    mock_mt5.last_error.return_value = (0, "")

    removed = state_manager.remove_inactive_trades()
    assert removed == 1
    assert len(state_manager.bot_active_trades) == 1
    assert state_manager.bot_active_trades[0].ticket == 2001

@patch("src.state_manager.mt5")
def test_remove_inactive_trades_mt5_not_initialized(mock_mt5, state_manager):
    mock_mt5.terminal_info.return_value = False
    removed = state_manager.remove_inactive_trades()
    assert removed == 0