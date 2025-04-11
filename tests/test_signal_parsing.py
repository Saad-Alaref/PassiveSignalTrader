import pytest
from unittest.mock import MagicMock
from src.signal_analyzer import SignalAnalyzer
from src.llm_interface import LLMInterface

@pytest.fixture
def mock_llm():
    mock = MagicMock(spec=LLMInterface)
    return mock

@pytest.fixture
def signal_analyzer(mock_llm):
    return SignalAnalyzer(mock_llm, data_fetcher=MagicMock(), config_service_instance=MagicMock())

def test_market_buy_signal(signal_analyzer, mock_llm):
    mock_llm.analyze_message.return_value = {
        "message_type": "new_signal",
        "action": "buy",
        "entry_type": "Market",
        "symbol": "XAUUSD",
        "stop_loss": 1900,
        "take_profits": [1950, 1970],
        "sentiment_score": 0.8,
        "is_signal": True
    }
    result = signal_analyzer.analyze("Buy XAUUSD now", image_data=None)
    # The analyze() method returns {'data': SignalData(...), 'type': 'new_signal'}
    assert result.get("type") == "new_signal"
    assert result.get("data").action.lower() == "buy"
    assert result.get("data").entry_type == "Market"

def test_tp_filtering(signal_analyzer, mock_llm):
    """Test that non-numeric TPs like 'open' are filtered out."""
    mock_llm.analyze_message.return_value = {
        "message_type": "new_signal",
        "action": "BUY",
        "entry_type": "Pending",
        "entry_price": "3000",
        "symbol": "XAUUSD",
        "stop_loss": 2990,
        "take_profits": [3010, 3020, "open", "N/A", None], # Mixed valid and invalid TPs
        "sentiment_score": 0.7,
        "is_signal": True
    }
    result = signal_analyzer.analyze("Buy XAUUSD 3000 SL 2990 TP 3010 3020 open", image_data=None)

    assert result.get("type") == "new_signal"
    signal_data = result.get("data")
    assert signal_data is not None
    # Check that only numeric TPs remain
    assert signal_data.take_profits == [3010.0, 3020.0]

def test_no_valid_tps(signal_analyzer, mock_llm):
    """Test that if only invalid TPs are provided, it defaults to ['N/A']."""
    mock_llm.analyze_message.return_value = {
        "message_type": "new_signal",
        "action": "SELL",
        "entry_type": "Market",
        "symbol": "XAUUSD",
        "stop_loss": 3050,
        "take_profits": ["open", "N/A", None], # Only invalid TPs
        "sentiment_score": -0.5,
        "is_signal": True
    }
    result = signal_analyzer.analyze("Sell XAUUSD SL 3050 TP open", image_data=None)

    assert result.get("type") == "new_signal"
    signal_data = result.get("data")
    assert signal_data is not None
    # Check that it defaults to N/A
    assert signal_data.take_profits == ["N/A"]

# (rest of tests unchanged)