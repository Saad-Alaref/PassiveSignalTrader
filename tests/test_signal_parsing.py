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

# (rest of tests unchanged)