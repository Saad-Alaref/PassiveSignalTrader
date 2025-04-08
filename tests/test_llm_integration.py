import pytest
from unittest.mock import MagicMock
from src.llm_interface import LLMInterface

@pytest.mark.integration
def test_llm_parses_buy_signal():
    mock_config = MagicMock()
    # Optionally set mock_config.get() to return your API key or other config values
    llm = LLMInterface(config_service_instance=mock_config)
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
    prompt = "Close the trade"
    response = llm.analyze_message(prompt)
    assert isinstance(response, dict)
    assert response.get("message_type") == "update"
    assert response.get("update_type") == "close_trade"