import pytest
from src.event_processor import _format_html_message
from src.logger_setup import setup_logging

def test_format_html_message_success():
    html = _format_html_message("Trade Executed", 12345, {"Symbol": "XAUUSD", "Volume": 0.01})
    assert "<b>Trade Executed</b>" in html
    assert "XAUUSD" in html
    assert "0.01" in html

def test_format_html_message_failure():
    html = _format_html_message("Trade FAILED", 12345, "Invalid price", success=False)
    assert "❌" in html or "⚠️" in html or "🚫" in html or "ℹ️" in html
    assert "Invalid price" in html

def test_setup_logging_runs(tmp_path):
    log_file = tmp_path / "test.log"
    setup_logging(log_file_path=str(log_file), log_level_str="DEBUG")
    # Should not raise exceptions