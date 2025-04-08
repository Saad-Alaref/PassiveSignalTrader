from dataclasses import dataclass, field
from typing import Optional, List, Union, Any # Union for flexibility, Any for dicts initially
from datetime import datetime
import MetaTrader5 as mt5 # For order type constants

# Define types for clarity
PriceType = Union[float, str] # Can be float, "N/A", "Market"
OptionalPrice = Optional[float] # For SL/TP which can be None or float
TicketType = int
MsgIdType = int
SymbolType = str
VolumeType = float
TimestampType = datetime

@dataclass
class SignalData:
    """Represents the structured data extracted for a new trade signal."""
    is_signal: bool = False # Should always be True if this object is created for a signal
    action: Optional[str] = None # "BUY" or "SELL"
    entry_type: Optional[str] = None # "Market" or "Pending"
    entry_price: Optional[PriceType] = None # float, "Market", "N/A", or "LOW-HIGH" string initially
    stop_loss: Optional[PriceType] = None # float or "N/A"
    take_profits: List[PriceType] = field(default_factory=lambda: ["N/A"]) # List of floats or "N/A"
    symbol: Optional[SymbolType] = None
    sentiment_score: Optional[float] = None # Optional sentiment score

@dataclass
class UpdateData:
    """Represents the structured data extracted for a trade update."""
    update_type: str = "unknown" # "modify_sltp", "modify_entry", "move_sl", "set_be", "close_trade", "partial_close", "cancel_pending" etc.
    symbol: Optional[SymbolType] = None # Hint for finding the trade
    target_trade_index: Optional[int] = None # Optional index from LLM context
    new_entry_price: Optional[PriceType] = "N/A" # float, range string, or "N/A"
    new_stop_loss: Optional[PriceType] = "N/A" # float or "N/A"
    new_take_profits: List[PriceType] = field(default_factory=lambda: ["N/A"]) # List of floats or "N/A"
    close_volume: Optional[PriceType] = "N/A" # float or "N/A"
    close_percentage: Optional[PriceType] = "N/A" # float or "N/A"

@dataclass
class TradeInfo:
    """Represents the state of an active trade managed by the bot."""
    ticket: TicketType
    symbol: SymbolType
    open_time: TimestampType
    original_msg_id: MsgIdType
    entry_price: OptionalPrice # Actual entry price (float) or pending price
    initial_sl: OptionalPrice
    original_volume: VolumeType # Volume of this specific trade/order
    all_tps: List[PriceType] # List containing only the TP assigned to this specific trade/order
    tp_strategy: str # e.g., 'sequential_partial_close', 'first_tp_full_close'
    assigned_tp: OptionalPrice # The specific TP set for this ticket in MT5
    is_pending: bool = False # True if it's a pending order not yet filled
    tsl_active: bool = False
    auto_tp_applied: bool = False
    next_tp_index: int = 0 # Primarily relevant for the old partial close strategy
    sequence_info: Optional[str] = None # e.g., "Seq 1/3", "Dist 2/5"
    auto_sl_pending_timestamp: Optional[TimestampType] = None

# Potentially add PendingConfirmationData later if needed
# @dataclass
# class PendingConfirmationData:
#     trade_details: dict # Or another dataclass like TradeParams
#     timestamp: TimestampType
#     message_id: int