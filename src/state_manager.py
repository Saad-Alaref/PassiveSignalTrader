import logging
import MetaTrader5 as mt5
from collections import deque
from datetime import datetime, timezone
from telethon import events

logger = logging.getLogger('TradeBot')

class StateManager:
    """
    Manages the application's state, including active trades and message history.
    """

    def __init__(self, config):
        """
        Initializes the StateManager.

        Args:
            config (configparser.ConfigParser): The application configuration.
        """
        self.config = config
        # List to store details of trades initiated by the bot.
        # Each entry example:
        # {
        #  'ticket': 12345, 'symbol': 'XAUUSD', 'open_time': datetime,
        #  'original_msg_id': 50, 'entry_price': 1900.50, 'initial_sl': 1895.0,
        #  'original_volume': 0.02, # Added
        #  'all_tps': [1910.0, 1920.0, "N/A"], # List of TPs from signal
        #  'tp_strategy': 'sequential_partial_close', # Strategy from config
        #  'next_tp_index': 0, # Index of the *next* TP in all_tps to monitor (starts at 0) - Added
        #  'auto_sl_pending_timestamp': datetime|None
        # }
        self.bot_active_trades = []
        # Deque for message history
        history_size = config.getint('LLMContext', 'history_message_count', fallback=5)
        self.message_history = deque(maxlen=history_size)
        logger.info(f"StateManager initialized. History size: {history_size}")

    # --- Active Trade Management ---

    def add_active_trade(self, trade_info):
        """Adds a new trade to the active trades list."""
        if not isinstance(trade_info, dict) or 'ticket' not in trade_info:
            logger.error(f"Attempted to add invalid trade_info: {trade_info}")
            return
        # Ensure it's not already added
        if not any(t['ticket'] == trade_info['ticket'] for t in self.bot_active_trades):
            self.bot_active_trades.append(trade_info)
            logger.info(f"Added active trade info: {trade_info}")
        else:
            logger.warning(f"Attempted to add duplicate active trade ticket: {trade_info['ticket']}")

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
        self.bot_active_trades[:] = [t for t in self.bot_active_trades if t['ticket'] in active_tickets_on_mt5]
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
        return next((t for t in self.bot_active_trades if t['ticket'] == ticket), None)

    def get_trade_by_original_msg_id(self, msg_id):
        """Finds and returns a trade by the original Telegram message ID that triggered it."""
        return next((t for t in self.bot_active_trades if t.get('original_msg_id') == msg_id), None)

    # --- AutoSL Flag Management ---

    def mark_trade_for_auto_sl(self, ticket):
        """Marks a trade as pending AutoSL application by adding a timestamp."""
        trade = self.get_trade_by_ticket(ticket)
        if trade:
            if 'auto_sl_pending_timestamp' not in trade:
                trade['auto_sl_pending_timestamp'] = datetime.now(timezone.utc)
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
        if trade and 'auto_sl_pending_timestamp' in trade:
            del trade['auto_sl_pending_timestamp']
            logger.debug(f"Removed AutoSL pending flag for ticket {ticket}.")
            return True
        return False

    def get_trades_pending_auto_sl(self):
        """Returns a list of trades currently marked for AutoSL check."""
        return [t for t in self.bot_active_trades if 'auto_sl_pending_timestamp' in t]

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
        enable_price = self.config.getboolean('LLMContext', 'enable_price_context', fallback=True)
        enable_trades = self.config.getboolean('LLMContext', 'enable_trade_context', fallback=True)
        enable_history = self.config.getboolean('LLMContext', 'enable_history_context', fallback=True)

        # 1. Price Context
        if enable_price:
            symbol = self.config.get('MT5', 'symbol', fallback='XAUUSD')
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
                    entry_price_display = t.get('entry_price', 'N/A')
                    if entry_price_display is None: entry_price_display = "Market"
                    # Include more details like type? Fetch from MT5? Keep it simple for now.
                    formatted_trades.append(
                        f"{i+1}. Ticket: {t['ticket']}, Symbol: {t['symbol']}, Entry: {entry_price_display}"
                    )
                llm_context['active_trades'] = formatted_trades
                logger.debug(f"Adding active trades context: {formatted_trades}")
            else:
                 logger.debug("No active bot trades found for LLM context.")

        # 3. Message History Context
        if enable_history:
            history = self.get_message_history()
            if history:
                llm_context['message_history'] = history
                logger.debug(f"Adding message history context (Size: {len(history)})")

        return llm_context

# Example usage (optional, for testing within this file)
if __name__ == '__main__':
    import configparser
    from logger_setup import setup_logging
    import time

    # Setup basic logging for test
    setup_logging(log_level_str='DEBUG')

    # Dummy config
    config = configparser.ConfigParser()
    config['LLMContext'] = {'history_message_count': '3'}
    config['MT5'] = {'symbol': 'TESTUSD'}

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

    state = StateManager(config)
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

    print("\nStateManager tests finished.")