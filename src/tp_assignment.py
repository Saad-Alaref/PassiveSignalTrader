from abc import ABC, abstractmethod
from typing import List, Any, Dict, Optional, Union


class ConfigValidationError(Exception):
    pass


class ConfigValidator:
    """Validates TP assignment configuration for supported modes."""
    @staticmethod
    def validate_tp_assignment_config(config: dict):
        required_keys = ["mode"]
        allowed_modes = {"none", "first_tp_first_trade", "custom_mapping"}
        for key in required_keys:
            if key not in config:
                raise ConfigValidationError(f"Missing required config key: {key}")
        if config["mode"] not in allowed_modes:
            raise ConfigValidationError(f"Invalid TP assignment mode: {config['mode']}")
        if config["mode"] == "custom_mapping" and "mapping" not in config:
            raise ConfigValidationError("Missing 'mapping' for custom_mapping mode.")
        # No extra validation needed for 'none' or 'first_tp_first_trade'


class TPAssignmentStrategy(ABC):
    """Abstract base for TP assignment strategies."""
    @abstractmethod
    def assign_tps(self, trade_data: dict, signal_data: dict) -> List[Optional[float]]:
        pass


class NoneTPAssignment(TPAssignmentStrategy):
    """Assigns no TPs."""
    def assign_tps(self, trade_data: dict, signal_data: dict) -> List[Optional[float]]:
        return [None] * trade_data.get("num_trades", 1)


class FirstTPFirstTradeAssignment(TPAssignmentStrategy):
    """
    Assigns the first TP from the signal to the first trade, and None to all subsequent trades.
    For single-trade scenarios, assigns the first TP if available, else None.
    """
    def assign_tps(self, trade_data: dict, signal_data: dict) -> List[Optional[float]]:
        num_trades = trade_data.get("num_trades", 1)
        tps_from_signal = signal_data.get("take_profits", [])
        first_tp = None
        # Find the first valid numeric TP
        for tp in tps_from_signal:
            try:
                val = float(tp)
                first_tp = val
                break
            except Exception:
                continue
        if num_trades == 1:
            return [first_tp]
        else:
            return [first_tp] + [None] * (num_trades - 1)


class CustomMappingTPAssignment(TPAssignmentStrategy):
    """
    Assigns TPs to trades based on a user-provided mapping list.
    The mapping is a list of indices (0-based) or 'none' for each trade.
    For single-trade, uses the first mapping index.
    """
    def __init__(self, mapping: list):
        self.mapping = mapping

    def assign_tps(self, trade_data: dict, signal_data: dict) -> List[Optional[float]]:
        num_trades = trade_data.get("num_trades", 1)
        tps_from_signal = signal_data.get("take_profits", [])
        result = []
        for i in range(num_trades):
            if i < len(self.mapping):
                idx = self.mapping[i]
                if isinstance(idx, int) and 0 <= idx < len(tps_from_signal):
                    try:
                        result.append(float(tps_from_signal[idx]))
                    except Exception:
                        result.append(None)
                elif isinstance(idx, str) and idx.lower() == "none":
                    result.append(None)
                else:
                    result.append(None)
            else:
                result.append(None)
        return result


# Removed obsolete CustomTPAssignment (function-based)
# Removed obsolete SequenceMapper


def get_tp_assignment_strategy(config: dict) -> TPAssignmentStrategy:
    ConfigValidator.validate_tp_assignment_config(config)
    mode = config["mode"]
    if mode == "none":
        return NoneTPAssignment()
    elif mode == "first_tp_first_trade":
        return FirstTPFirstTradeAssignment()
    elif mode == "custom_mapping":
        return CustomMappingTPAssignment(config["mapping"])
    else:
        raise ConfigValidationError(f"Unknown TP assignment mode: {mode}")