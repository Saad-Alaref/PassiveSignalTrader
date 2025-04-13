import pytest
import types # Import types for SimpleNamespace
from src.tp_assignment import (
    get_tp_assignment_strategy,
    ConfigValidator,
    ConfigValidationError,
)

# --- NONE MODE ---

def test_none_tp_assignment_multi_trade():
    strat = get_tp_assignment_strategy({"mode": "none"})
    mock_signal = types.SimpleNamespace(take_profits=[3112, 3120])
    tps = strat.assign_tps({"num_trades": 3}, mock_signal)
    assert tps == [None, None, None]

def test_none_tp_assignment_single_trade():
    strat = get_tp_assignment_strategy({"mode": "none"})
    mock_signal = types.SimpleNamespace(take_profits=[3112])
    tps = strat.assign_tps({"num_trades": 1}, mock_signal)
    assert tps == [None]

# --- FIRST TP FIRST TRADE MODE ---

def test_first_tp_first_trade_multi_with_tps():
    strat = get_tp_assignment_strategy({"mode": "first_tp_first_trade"})
    mock_signal = types.SimpleNamespace(take_profits=[3112, 3120, 3130])
    tps = strat.assign_tps({"num_trades": 3}, mock_signal)
    assert tps == [3112, None, None]

def test_first_tp_first_trade_multi_no_tps():
    strat = get_tp_assignment_strategy({"mode": "first_tp_first_trade"})
    mock_signal = types.SimpleNamespace(take_profits=[])
    tps = strat.assign_tps({"num_trades": 2}, mock_signal)
    assert tps == [None, None]

def test_first_tp_first_trade_single_with_tp():
    strat = get_tp_assignment_strategy({"mode": "first_tp_first_trade"})
    mock_signal = types.SimpleNamespace(take_profits=[3112])
    tps = strat.assign_tps({"num_trades": 1}, mock_signal)
    assert tps == [3112]

def test_first_tp_first_trade_single_no_tp():
    strat = get_tp_assignment_strategy({"mode": "first_tp_first_trade"})
    mock_signal = types.SimpleNamespace(take_profits=[])
    tps = strat.assign_tps({"num_trades": 1}, mock_signal)
    assert tps == [None]

# --- CUSTOM MAPPING MODE ---

def test_custom_mapping_exact_tps():
    strat = get_tp_assignment_strategy({"mode": "custom_mapping", "mapping": [0, "none", 1]})
    mock_signal = types.SimpleNamespace(take_profits=[3112, 3120, 3130])
    tps = strat.assign_tps({"num_trades": 3}, mock_signal)
    assert tps == [3112, None, 3120]

def test_custom_mapping_index_out_of_range():
    strat = get_tp_assignment_strategy({"mode": "custom_mapping", "mapping": [0, 1, 2, 3]})
    mock_signal = types.SimpleNamespace(take_profits=[3112, 3120])
    tps = strat.assign_tps({"num_trades": 4}, mock_signal)
    assert tps == [3112, 3120, None, None]

def test_custom_mapping_more_tps_than_trades():
    strat = get_tp_assignment_strategy({"mode": "custom_mapping", "mapping": [0, 1]})
    mock_signal = types.SimpleNamespace(take_profits=[3112, 3120, 3130, 3140])
    tps = strat.assign_tps({"num_trades": 2}, mock_signal)
    assert tps == [3112, 3120]

def test_custom_mapping_single_trade():
    strat = get_tp_assignment_strategy({"mode": "custom_mapping", "mapping": [1]})
    mock_signal = types.SimpleNamespace(take_profits=[3112, 3120])
    tps = strat.assign_tps({"num_trades": 1}, mock_signal)
    assert tps == [3120]

def test_custom_mapping_none_and_index():
    strat = get_tp_assignment_strategy({"mode": "custom_mapping", "mapping": ["none", 0]})
    mock_signal = types.SimpleNamespace(take_profits=[3112])
    tps = strat.assign_tps({"num_trades": 2}, mock_signal)
    assert tps == [None, 3112]

def test_custom_mapping_empty_tps():
    strat = get_tp_assignment_strategy({"mode": "custom_mapping", "mapping": [0, "none"]})
    mock_signal = types.SimpleNamespace(take_profits=[])
    tps = strat.assign_tps({"num_trades": 2}, mock_signal)
    assert tps == [None, None]

# --- CONFIG VALIDATION ---

def test_config_validator_valid_modes():
    ConfigValidator.validate_tp_assignment_config({"mode": "none"})
    ConfigValidator.validate_tp_assignment_config({"mode": "first_tp_first_trade"})
    ConfigValidator.validate_tp_assignment_config({"mode": "custom_mapping", "mapping": [0, "none", 1]})

def test_config_validator_invalid_modes():
    with pytest.raises(ConfigValidationError):
        ConfigValidator.validate_tp_assignment_config({})
    with pytest.raises(ConfigValidationError):
        ConfigValidator.validate_tp_assignment_config({"mode": "invalid"})
    with pytest.raises(ConfigValidationError):
        ConfigValidator.validate_tp_assignment_config({"mode": "custom_mapping"})