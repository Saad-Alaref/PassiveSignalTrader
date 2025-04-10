import pytest
from unittest.mock import MagicMock
from src.decision_logic import DecisionLogic

@pytest.fixture
def decision_logic():
    mock_config = MagicMock()
    # Mock config methods to return real values
    mock_config.getboolean.return_value = True
    mock_config.getfloat.side_effect = lambda section, key, fallback=None: {
        ('DecisionLogic', 'sentiment_weight'): 0.5,
        ('DecisionLogic', 'price_action_weight'): 0.5,
        ('DecisionLogic', 'approval_threshold'): 0.6,
    }.get((section, key), fallback if fallback is not None else 0.5)
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

def test_decide_sell_signal(decision_logic):
    signal_data = MagicMock()
    signal_data.is_signal = True
    signal_data.action = "SELL"
    signal_data.entry_type = "Market"
    decision, reason, order_type = decision_logic.decide(signal_data)
    assert isinstance(decision, bool)
    assert reason is None or isinstance(reason, str)

def test_decide_missing_fields(decision_logic):
    signal_data = MagicMock()
    signal_data.is_signal = True
    signal_data.action = None  # Missing action
    signal_data.entry_type = None
    decision, reason, order_type = decision_logic.decide(signal_data)
    assert decision is False
    assert reason is not None

def test_decide_extreme_price(decision_logic):
    signal_data = MagicMock()
    signal_data.is_signal = True
    signal_data.action = "BUY"
    signal_data.entry_type = "Pending"  # Must be Pending to trigger price check
    # Simulate extreme/unrealistic price via fetcher mock
    decision_logic.fetcher.get_symbol_tick.return_value = MagicMock(bid=1e9, ask=1e9)
    signal_data.sentiment_score = 0.0  # Explicitly set to avoid MagicMock
    signal_data.entry_price = 1e9  # Extreme price to trigger rejection
    decision, reason, order_type = decision_logic.decide(signal_data)
    assert decision is False
    assert reason is not None