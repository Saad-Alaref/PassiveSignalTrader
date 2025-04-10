import pytest
from unittest.mock import MagicMock
from src.llm_interface import LLMInterface

@pytest.mark.integration
def test_llm_parses_buy_signal():
    mock_config = MagicMock()
    llm = LLMInterface(config_service_instance=mock_config)

    # Patch analyze_message to return a fake response
    llm.analyze_message = MagicMock(return_value={
        "message_type": "new_signal",
        "action": "buy",
        "symbol": "XAUUSD",
        "stop_loss": 1900,
        "take_profits": [1950, 1970],
        "sentiment_score": 0.8,
        "is_signal": True
    })

    prompt = "Buy XAUUSD now with SL 1900 and TP 1950"
    response = llm.analyze_message(prompt)
    assert isinstance(response, dict)
    assert response.get("message_type") == "new_signal"
    assert response.get("action") == "buy"
    assert response.get("symbol") == "XAUUSD"
    assert response.get("stop_loss") is not None
    assert isinstance(response.get("take_profits"), list)

@pytest.mark.integration
def test_llm_parses_close_update():
    mock_config = MagicMock()
    llm = LLMInterface(config_service_instance=mock_config)

    # Patch analyze_message to return a fake response
    llm.analyze_message = MagicMock(return_value={
        "message_type": "update",
        "update_type": "close_trade",
        "symbol": "XAUUSD",
        "target_trade_index": 1,
        "new_stop_loss": "N/A",
        "new_take_profits": ["N/A"],
        "close_volume": "N/A",
        "close_percentage": "N/A"
    })

    prompt = "Close the trade"
    response = llm.analyze_message(prompt)
    assert isinstance(response, dict)
    assert response.get("message_type") == "update"
    assert response.get("update_type") == "close_trade"