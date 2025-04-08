import pytest
from unittest.mock import MagicMock
from src.decision_logic import DecisionLogic

@pytest.fixture
def decision_logic():
    mock_config = MagicMock()
    mock_fetcher = MagicMock()
    return DecisionLogic(mock_config, mock_fetcher)

def test_decide_buy_signal(decision_logic):
    signal_data = MagicMock()
    signal_data.is_signal = True
    signal_data.action = "BUY"
    signal_data.entry_type = "Market"
    decision, reason, order_type = decision_logic.decide(signal_data)
    assert isinstance(decision, bool)
    assert reason is None or isinstance(reason, str)

def test_decide_invalid_signal(decision_logic):
    signal_data = MagicMock()
    signal_data.is_signal = False
    decision, reason, order_type = decision_logic.decide(signal_data)
    assert decision is False
    assert reason is not None

def test_perform_price_action_check(decision_logic):
    score, reason, order_type = decision_logic._perform_price_action_check("BUY", 1950.0)
    assert isinstance(score, float)
    assert isinstance(reason, str)
    assert order_type is None or isinstance(order_type, int)