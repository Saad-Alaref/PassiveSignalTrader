import logging
import MetaTrader5 as mt5
from typing import Union, Optional
from collections import deque, defaultdict # Keep deque, defaultdict might be useful later
from datetime import datetime, timezone, timedelta # Keep only this one
from telethon import events
from .models import TradeInfo # Import the dataclass
logger = logging.getLogger('TradeBot')

class StateManager:
    """
    Manages the application's state, including active trades and message history.
    """

    def __init__(self, config_service_instance): # Inject service
        """
        Initializes the StateManager.

        Args:
            config (configparser.ConfigParser): The application configuration.
        """
        self.config_service = config_service_instance # Store service instance
        # List to store details of trades initiated by the bot.
        self.bot_active_trades = []
        # Deque for message history - Max size read at init, not easily hot-reloadable
        self.history_message_count = self.config_service.getint('LLMContext', 'history_message_count', fallback=10) # Use service
        self.message_history = deque(maxlen=self.history_message_count)
        self.last_market_execution_time = None # Timestamp of the last market order execution
        self.last_market_execution_time = None # Timestamp of the last market order execution

        # Store closed trades log for summaries
        self.closed_trades_log = []

        # --- New: Store pending trade confirmations ---
        # Structure: { confirmation_id: {'trade_details': {...}, 'timestamp': datetime, 'message_id': int, 'chat_id': int, 'initial_market_price': float | None} }
        self.pending_confirmations = {}
        # --- End New ---

        logger.info(f"StateManager initialized. History size: {self.history_message_count}")

    def record_closed_trade(self, trade_dict):
        """
        Records a closed or canceled trade for daily summary.

        Args:
            trade_dict (dict): Dictionary with keys like 'ticket', 'symbol', 'profit', 'close_time', 'reason'.
        """
        self.closed_trades_log.append(trade_dict)
        logger.debug(f"Recorded closed trade: {trade_dict}")

    def get_closed_trades_log(self):
        """
        Returns the list of recorded closed trades.

        Returns:
            list: List of closed trade dictionaries.
        """
        return self.closed_trades_log

    def clear_closed_trades_log(self):
        """
        Clears the closed trades log (e.g., after daily summary sent).
        """
        self.closed_trades_log.clear()
        logger.debug("Cleared closed trades log after summary.")

    # --- Active Trade Management ---

    def add_active_trade(self, trade_info_data: dict, auto_tp_applied=False): # Accept dict initially
        """
        Adds a new trade to the active trades list, converting dict to TradeInfo object.

        Args:
            trade_info_data (dict): Dictionary containing trade details from execution strategies.
            auto_tp_applied (bool): Flag indicating if AutoTP was used for this trade.
        """
        if not isinstance(trade_info_data, dict) or 'ticket' not in trade_info_data:
            logger.error(f"Attempted to add invalid trade_info_data: {trade_info_data}")
            return

        # Convert dict to TradeInfo dataclass instance
        try:
            # Ensure all required fields for TradeInfo are present or have defaults
            trade_obj = TradeInfo(
                ticket=trade_info_data['ticket'],
                symbol=trade_info_data['symbol'],
                open_time=trade_info_data['open_time'],
                original_msg_id=trade_info_data['original_msg_id'],
                entry_price=trade_info_data.get('entry_price'), # Use .get for optional fields
                initial_sl=trade_info_data.get('initial_sl'),
                original_volume=trade_info_data['original_volume'],
                all_tps=trade_info_data.get('all_tps', []), # Default to empty list
                tp_strategy=trade_info_data['tp_strategy'],
                assigned_tp=trade_info_data.get('assigned_tp'),
                is_pending=trade_info_data.get('is_pending', False),
                tsl_active=False, # Initialize TSL flag
                auto_tp_applied=auto_tp_applied, # Add the flag
                next_tp_index=trade_info_data.get('next_tp_index', 0),
                sequence_info=trade_info_data.get('sequence_info'),
                auto_sl_pending_timestamp=None # Initialize as None
            )
        except KeyError as e:
            logger.error(f"Missing required key '{e}' in trade_info_data when creating TradeInfo object: {trade_info_data}")
            return
        except Exception as e:
            logger.error(f"Error creating TradeInfo object from data: {e}. Data: {trade_info_data}", exc_info=True)
            return
        # Ensure it's not already added (check ticket attribute of objects)
        if not any(t.ticket == trade_obj.ticket for t in self.bot_active_trades):
            self.bot_active_trades.append(trade_obj) # Append the object
            logger.info(f"Added active trade info (Ticket: {trade_obj.ticket})")
            logger.debug(f"Stored TradeInfo object: {trade_obj}")
        else:
            logger.warning(f"Attempted to add duplicate active trade ticket: {trade_obj.ticket}")

    def remove_inactive_trades(self):
        """
        Removes trades from the internal list if they are no longer found
        as open positions or pending orders in MT5.
        """
        if not mt5.terminal_info(): # Ensure MT5 is initialized
             logger.error("Cannot remove inactive trades, MT5 not initialized/connected.")
             return 0

        active_tickets_on_mt5 = set()
        removed_count = 0

        # Check open positions
        positions = mt5.positions_get()
        if positions:
            active_tickets_on_mt5.update(pos.ticket for pos in positions)
        else:
            # Log error only if it wasn't just an empty result
            if mt5.last_error()[0] != 0: # Check error code
                 logger.error(f"Failed to get positions for inactive trade check: {mt5.last_error()}")
                 # Potentially return early if we can't verify? Or proceed cautiously? Proceed for now.

        # Check pending orders
        orders = mt5.orders_get()
        if orders:
            active_tickets_on_mt5.update(ord.ticket for ord in orders)
        else:
             if mt5.last_error()[0] != 0:
                  logger.error(f"Failed to get orders for inactive trade check: {mt5.last_error()}")

        original_count = len(self.bot_active_trades)
        # Filter the internal list in place
        self.bot_active_trades[:] = [t for t in self.bot_active_trades if t.ticket in active_tickets_on_mt5] # Use attribute access
        filtered_count = len(self.bot_active_trades)
        removed_count = original_count - filtered_count

        if removed_count > 0:
            logger.info(f"Cleaned bot_active_trades list. Removed {removed_count} inactive trades.")
        else:
            logger.debug("No inactive trades found during cleanup.")
        return removed_count

    def get_active_trades(self):
        """Returns the list of currently tracked active trades."""
        return self.bot_active_trades

    def get_trade_by_ticket(self, ticket):
        """Finds and returns a trade by its MT5 ticket."""
        return next((t for t in self.bot_active_trades if t.ticket == ticket), None) # Use attribute access

    def get_trade_by_original_msg_id(self, msg_id):
        """Finds and returns a trade by the original Telegram message ID that triggered it."""
        return next((t for t in self.bot_active_trades if t.original_msg_id == msg_id), None) # Use attribute access

    # --- AutoSL Flag Management ---

    def mark_trade_for_auto_sl(self, ticket):
        """Marks a trade as pending AutoSL application by adding a timestamp."""
        trade = self.get_trade_by_ticket(ticket)
        if trade:
            if trade.auto_sl_pending_timestamp is None: # Check attribute
                trade.auto_sl_pending_timestamp = datetime.now(timezone.utc) # Set attribute
                logger.info(f"Trade {ticket} marked for AutoSL check.")
                return True
            else:
                 logger.debug(f"Trade {ticket} already marked for AutoSL check.")
                 return False # Already marked
        else:
            logger.warning(f"Could not mark trade {ticket} for AutoSL: Ticket not found in active trades.")
            return False

    def remove_auto_sl_pending_flag(self, ticket):
        """Removes the AutoSL pending flag from a trade."""
        trade = self.get_trade_by_ticket(ticket)
        if trade and trade.auto_sl_pending_timestamp is not None: # Check attribute
            trade.auto_sl_pending_timestamp = None # Set attribute to None
            logger.debug(f"Removed AutoSL pending flag for ticket {ticket}.")
            return True
        return False

    def get_trades_pending_auto_sl(self):
        """Returns a list of trades currently marked for AutoSL check."""
        return [t for t in self.bot_active_trades if t.auto_sl_pending_timestamp is not None] # Check attribute

    # --- Message History Management ---

    def add_message_to_history(self, event):
        """Adds relevant message details to the history deque."""
        # Store relevant info: id, text, sender_id, timestamp, is_edit
        history_entry = {
            'id': event.id,
            'text': getattr(event, 'text', ''),
            'sender_id': event.sender_id,
            'is_edit': isinstance(event, events.MessageEdited.Event),
            'timestamp': event.date.isoformat() # Store timestamp
        }
        self.message_history.append(history_entry)
        logger.debug(f"Added message {event.id} to history. History size: {len(self.message_history)}")

    def get_message_history(self):
        """Returns the message history as a list."""
        return list(self.message_history)

    # --- New: Pending Confirmation Management ---

    def add_pending_confirmation(self, confirmation_id: str, trade_details: dict, message_id: int, chat_id: int, timestamp: datetime, initial_market_price: Optional[float]):
        """
        Stores details of a trade awaiting user confirmation via Telegram buttons.

        Args:
            confirmation_id (str): The unique ID for this confirmation request.
            trade_details (dict): The dictionary containing all necessary parameters for mt5_executor.execute_trade.
            message_id (int): The ID of the Telegram message sent with the confirmation buttons.
            chat_id (int): The ID of the chat where the confirmation message was sent.
            timestamp (datetime): The time when the confirmation request was initiated.
            initial_market_price (Optional[float]): The market price at the time the confirmation was requested.
        """
        if confirmation_id in self.pending_confirmations:
            logger.warning(f"Attempted to add duplicate pending confirmation ID: {confirmation_id}")
            # Overwrite maybe? Or ignore? Let's overwrite for now, assuming a retry might occur.
        self.pending_confirmations[confirmation_id] = {
            'trade_details': trade_details,
            'timestamp': timestamp,
            'message_id': message_id,
            'chat_id': chat_id, # Store chat_id
            'initial_market_price': initial_market_price # Store initial price
        }
        logger.info(f"Added pending confirmation: ID={confirmation_id}, MsgID={message_id}, InitialPrice={initial_market_price}")
        logger.debug(f"Pending confirmation details for {confirmation_id}: {trade_details}")

    def get_pending_confirmation(self, confirmation_id: str) -> Union[dict, None]:
        """
        Retrieves the details of a pending confirmation by its ID.

        Args:
            confirmation_id (str): The unique ID of the confirmation request.

        Returns:
            dict | None: The dictionary containing confirmation details, or None if not found.
        """
        confirmation_data = self.pending_confirmations.get(confirmation_id)
        if confirmation_data:
            logger.debug(f"Retrieved pending confirmation data for ID: {confirmation_id}")
        else:
            logger.warning(f"Pending confirmation ID not found: {confirmation_id}")
        return confirmation_data

    def remove_pending_confirmation(self, confirmation_id: str) -> bool:
        """
        Removes a pending confirmation from the store after it's handled (confirmed, rejected, or expired).

        Args:
            confirmation_id (str): The unique ID of the confirmation request to remove.

        Returns:
            bool: True if the confirmation was found and removed, False otherwise.
        """
        if confirmation_id in self.pending_confirmations:
            del self.pending_confirmations[confirmation_id]
            logger.info(f"Removed pending confirmation: ID={confirmation_id}")
            return True
        else:
            logger.warning(f"Attempted to remove non-existent pending confirmation ID: {confirmation_id}")
            return False

    def get_active_confirmations(self) -> dict:
        """Returns the dictionary of active pending confirmations."""
        # Return a copy to prevent modification during iteration
        return self.pending_confirmations.copy()

    # --- End New ---

    # --- LLM Context Generation ---

    def get_llm_context(self, mt5_fetcher):
        """
        Gathers and formats context (price, trades, history) for the LLM.

        Args:
            mt5_fetcher (MT5DataFetcher): Instance to fetch current price.

        Returns:
            dict: The context dictionary.
        """
        llm_context = {}
        # Get config settings for context
        # Read context flags dynamically using the service
        enable_price = self.config_service.getboolean('LLMContext', 'enable_price_context', fallback=True)
        enable_trades = self.config_service.getboolean('LLMContext', 'enable_trade_context', fallback=True)
        enable_history = self.config_service.getboolean('LLMContext', 'enable_history_context', fallback=True)

        # 1. Price Context
        if enable_price:
            symbol = self.config_service.get('MT5', 'symbol', fallback='XAUUSD') # Use service
            tick = mt5_fetcher.get_symbol_tick(symbol)
            if tick:
                # Convert timestamp for context
                dt_time = datetime.fromtimestamp(tick.time, tz=timezone.utc)
                llm_context['current_price'] = {'symbol': symbol, 'bid': tick.bid, 'ask': tick.ask, 'time': dt_time.isoformat()}
                logger.debug(f"Adding price context: {symbol} Bid={tick.bid}, Ask={tick.ask}")
            else:
                logger.warning(f"Could not fetch current price for {symbol} for LLM context.")

        # 2. Active Trades Context
        if enable_trades:
            # Clean inactive trades before formatting context
            self.remove_inactive_trades()
            active_trades = self.get_active_trades()
            if active_trades:
                formatted_trades = []
                for i, t in enumerate(active_trades):
                    entry_price_display = getattr(t, 'entry_price', 'N/A') or 'N/A'
                    if entry_price_display is None: entry_price_display = "Market"
                    # Include more details like type? Fetch from MT5? Keep it simple for now.
                    # Access attributes of the TradeInfo object
                    formatted_trades.append(
                        f"{i+1}. Ticket: {t.ticket}, Symbol: {t.symbol}, Entry: {entry_price_display}"
                    )
                llm_context['active_trades'] = formatted_trades
                logger.debug(f"Adding active trades context: {formatted_trades}")
            else:
                 logger.debug("No active bot trades found for LLM context.")

        # 3. Message History Context
        enable_history = self.config_service.getboolean('LLMContext', 'enable_history_context', fallback=True) # Use service
        if enable_history:
            history = self.get_message_history()
            if history:
                llm_context['message_history'] = history
                logger.debug(f"Adding message history context (Size: {len(history)})")

        return llm_context

    # --- Market Order Cooldown Management ---

    def record_market_execution(self):
        """Records the timestamp of the current time as the last market execution."""
        self.last_market_execution_time = datetime.now(timezone.utc)
        logger.info(f"Recorded market execution time: {self.last_market_execution_time}")

    def is_market_cooldown_active(self, cooldown_seconds: int) -> bool:
        """
        Checks if the market order cooldown period is currently active.

        Args:
            cooldown_seconds (int): The duration of the cooldown in seconds.

        Returns:
            bool: True if cooldown is active, False otherwise.
        """
        if cooldown_seconds <= 0:
            return False # Cooldown disabled if duration is zero or negative

        if self.last_market_execution_time is None:
            return False # No previous market execution recorded

        now = datetime.now(timezone.utc)
        time_since_last = now - self.last_market_execution_time
        is_active = time_since_last < timedelta(seconds=cooldown_seconds)

        if is_active:
            remaining = timedelta(seconds=cooldown_seconds) - time_since_last
            # Use seconds for logging remaining time for clarity
            logger.info(f"Market order cooldown is ACTIVE. Time remaining: {remaining.total_seconds():.1f} seconds.")
        else:
            logger.debug(f"Market order cooldown is INACTIVE. Time since last: {time_since_last}")

        return is_active

# Example usage (optional, for testing within this file)
# Add tests for pending confirmations if running standalone
if __name__ == '__main__':
    # import configparser # No longer needed directly
    from logger_setup import setup_logging
    import time
    from config_service import ConfigService # Import service for testing

    # Setup basic logging for test
    setup_logging(log_level_str='DEBUG')

    # Dummy config service for testing
    # Create a dummy config file content
    dummy_config_content = """
[LLMContext]
history_message_count = 3
enable_price_context = true
enable_trade_context = true
enable_history_context = true

[MT5]
symbol = TESTUSD
"""
    # Write to a temporary file (or use StringIO if preferred)
    dummy_config_path = "dummy_config_for_state_test.ini"
    with open(dummy_config_path, "w") as f:
        f.write(dummy_config_content)

    # Instantiate ConfigService with the dummy file
    test_config_service = ConfigService(config_file=dummy_config_path)


    # Dummy event class
    class DummyEvent:
        def __init__(self, id, text, sender_id, date, is_edit=False):
            self.id = id
            self.text = text
            self.sender_id = sender_id
            self.date = date
            self.is_edit = is_edit

    # Dummy fetcher
    class DummyFetcher:
        def get_symbol_tick(self, symbol): return None # Simulate no price data

    # Instantiate StateManager with the test service
    state = StateManager(test_config_service)
    fetcher = DummyFetcher()

    # Test history
    state.add_message_to_history(DummyEvent(1, "Msg 1", 123, datetime.now(timezone.utc)))
    time.sleep(0.1)
    state.add_message_to_history(DummyEvent(2, "Msg 2", 456, datetime.now(timezone.utc)))
    time.sleep(0.1)
    state.add_message_to_history(DummyEvent(3, "Msg 3", 123, datetime.now(timezone.utc)))
    time.sleep(0.1)
    state.add_message_to_history(DummyEvent(4, "Msg 4", 789, datetime.now(timezone.utc), is_edit=True))

    print("History:", state.get_message_history())
    assert len(state.get_message_history()) == 3 # Max size
    assert state.get_message_history()[0]['id'] == 2 # Check eviction

    # Test trades
    state.add_active_trade({'ticket': 1001, 'symbol': 'TESTUSD', 'open_time': datetime.now(timezone.utc), 'original_msg_id': 1})
    state.add_active_trade({'ticket': 1002, 'symbol': 'TESTUSD', 'open_time': datetime.now(timezone.utc), 'original_msg_id': 3, 'entry_price': 1.2345})
    print("Active Trades:", state.get_active_trades())
    assert len(state.get_active_trades()) == 2

    # Test AutoSL marking
    state.mark_trade_for_auto_sl(1001)
    state.mark_trade_for_auto_sl(1003) # Non-existent
    trade1 = state.get_trade_by_ticket(1001)
    assert 'auto_sl_pending_timestamp' in trade1
    print("Trades pending AutoSL:", state.get_trades_pending_auto_sl())
    state.remove_auto_sl_pending_flag(1001)
    assert 'auto_sl_pending_timestamp' not in trade1
    print("Trades pending AutoSL after removal:", state.get_trades_pending_auto_sl())


    # Test context generation (requires MT5 connection for full test)
    print("\nLLM Context (No MT5):", state.get_llm_context(fetcher))

    # Test Pending Confirmations
    print("\n--- Testing Pending Confirmations ---")
    conf_id_1 = "abc-123"
    details_1 = {'action': 'BUY', 'symbol': 'TESTUSD', 'volume': 0.01}
    msg_id_1 = 999
    ts_1 = datetime.now(timezone.utc)

    state.add_pending_confirmation(conf_id_1, details_1, msg_id_1, ts_1)
    print("Pending Confirmations after add:", state.pending_confirmations)
    assert conf_id_1 in state.pending_confirmations

    retrieved_conf = state.get_pending_confirmation(conf_id_1)
    print("Retrieved Confirmation:", retrieved_conf)
    assert retrieved_conf is not None
    assert retrieved_conf['message_id'] == msg_id_1
    assert retrieved_conf['trade_details']['action'] == 'BUY'

    retrieved_conf_bad = state.get_pending_confirmation("bad-id")
    assert retrieved_conf_bad is None

    removed = state.remove_pending_confirmation(conf_id_1)
    assert removed is True
    print("Pending Confirmations after remove:", state.pending_confirmations)
    assert conf_id_1 not in state.pending_confirmations

    removed_bad = state.remove_pending_confirmation("bad-id")
    assert removed_bad is False

    print("\nStateManager tests finished.")
    # Clean up dummy config file
    import os
    if os.path.exists(dummy_config_path):
        os.remove(dummy_config_path)