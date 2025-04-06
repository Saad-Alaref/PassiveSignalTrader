import logging
import MetaTrader5 as mt5 # For order type constants
from .mt5_data_fetcher import MT5DataFetcher # Use relative import

logger = logging.getLogger('TradeBot')

class DecisionLogic:
    """
    Implements the logic to decide whether an identified signal should result
    in a trade order, based on signal type, LLM sentiment, and price action.
    """

    def __init__(self, config, data_fetcher: MT5DataFetcher):
        """
        Initializes the DecisionLogic component.

        Args:
            config (configparser.ConfigParser): The application configuration.
            data_fetcher (MT5DataFetcher): Instance for fetching market data.
        """
        self.config = config
        self.fetcher = data_fetcher
        # Store config reference for dynamic access
        logger.info("DecisionLogic initialized.")
        # Configuration values will be read dynamically in the 'decide' method


    def decide(self, signal_data: dict):
        """
        Makes the decision whether to proceed with a trade based on the signal data.

        Args:
            signal_data (dict): The structured data returned by SignalAnalyzer, containing
                                keys like 'is_signal', 'action', 'entry_type', 'entry_price',
                                'sentiment_score', etc.

        Returns:
            tuple: (bool, str or None, int or None)
                   - bool: True if the trade is approved, False otherwise.
                   - str or None: Reason for rejection if applicable.
                   - int or None: The determined MT5 order type (e.g., mt5.ORDER_TYPE_BUY_LIMIT)
                                  if it's a pending order, None otherwise.
        """
        if not signal_data or not signal_data.get('is_signal'):
            logger.debug("Decision: Not a signal or no data provided.")
            return False, "Not a signal", None

        entry_type = signal_data.get('entry_type')
        action = signal_data.get('action') # BUY or SELL

        # --- Path 1: Market Execution Signals ---
        if entry_type == "Market":
            logger.info("Decision: Market execution signal identified. Bypassing weighted logic.")
            # PRD: Execute immediately if identified by LLM.
            # No sentiment/price check needed here as per logic defined.
            # Determine MT5 order type for market order
            mt5_order_type = mt5.ORDER_TYPE_BUY if action == "BUY" else mt5.ORDER_TYPE_SELL
            return True, "Market order approved", mt5_order_type

        # --- Path 2: Pending Order Signals ---
        elif entry_type == "Pending":
            logger.info("Decision: Pending order signal identified. Applying weighted logic.")
            signal_price_str = signal_data.get('entry_price')
            sentiment_score = signal_data.get('sentiment_score', 0.0) # Default to neutral if missing

            # Validate signal price
            try:
                signal_price = float(signal_price_str)
            except (ValueError, TypeError):
                logger.error(f"Invalid or missing entry price for pending order: {signal_price_str}")
                return False, "Invalid entry price", None

            # A. Price Action Check: Determine pending order type and score
            price_action_score, reason, determined_order_type = self._perform_price_action_check(action, signal_price)
            if price_action_score == 0.0:
                logger.warning(f"Decision Rejected: Price action check failed. Reason: {reason}")
                return False, f"Price action check failed: {reason}", None

            # B. LLM Sentiment Score (Conditional)
            # --- Read config values dynamically ---
            use_sentiment = self.config.getboolean('DecisionLogic', 'use_sentiment_analysis', fallback=True)
            sentiment_weight = self.config.getfloat('DecisionLogic', 'sentiment_weight', fallback=0.5)
            price_action_weight = self.config.getfloat('DecisionLogic', 'price_action_weight', fallback=0.5)
            approval_threshold = self.config.getfloat('DecisionLogic', 'approval_threshold', fallback=0.6)

            # Adjust weights if sentiment is disabled
            if not use_sentiment:
                 price_action_weight = 1.0
                 sentiment_weight = 0.0 # Ensure sentiment term becomes zero

            # --- End Read config values ---

            normalized_sentiment_score = 0.0 # Default if sentiment is disabled
            sentiment_log_str = "Disabled"
            if use_sentiment:
                # Clamp sentiment score to [-1.0, 1.0] before normalizing
                clamped_sentiment_score = max(-1.0, min(1.0, sentiment_score))
                # Normalize clamped score to [0.0, 1.0] for weighting
                normalized_sentiment_score = (clamped_sentiment_score + 1.0) / 2.0
                sentiment_log_str = f"{normalized_sentiment_score:.2f} (Raw: {sentiment_score})"
                logger.debug(f"LLM Sentiment: Raw={sentiment_score}, Normalized={normalized_sentiment_score:.2f}")
            else:
                 logger.debug("LLM Sentiment: Skipped (disabled in config)")


            # C. Combined Decision
            # Note: If sentiment is disabled, self.sentiment_weight is 0, so the term becomes zero.
            # Use dynamically read weights
            total_score = (price_action_score * price_action_weight) + \
                          (normalized_sentiment_score * sentiment_weight)
            logger.info(f"Decision Score: PriceAction({price_action_score:.2f} * {price_action_weight}) + Sentiment({sentiment_log_str} * {sentiment_weight}) = {total_score:.2f}")

            # Use dynamically read threshold
            if total_score >= approval_threshold:
                logger.info(f"Decision Approved: Score {total_score:.2f} >= Threshold {approval_threshold}. Order Type: {determined_order_type}")
                return True, f"Approved (Score: {total_score:.2f})", determined_order_type
            else:
                logger.info(f"Decision Rejected: Score {total_score:.2f} < Threshold {approval_threshold}")
                return False, f"Rejected (Score: {total_score:.2f})", None

        else:
            logger.error(f"Unknown entry type encountered: {entry_type}")
            return False, f"Unknown entry type: {entry_type}", None


    def _perform_price_action_check(self, action, signal_price):
        """
        Compares signal price to current market price to determine pending order type.
        Acts as the 'Price Action' component for the weighted decision.

        Args:
            action (str): "BUY" or "SELL".
            signal_price (float): The entry price from the signal.

        Returns:
            tuple: (float, str, int or None)
                   - float: Score (1.0 for valid type, 0.0 for invalid/error).
                   - str: Reason for the score (e.g., "Determined BUY_LIMIT", "Failed to get market price").
                   - int or None: The determined MT5 order type constant, or None.
        """
        # Read symbol dynamically
        mt5_symbol = self.config.get('MT5', 'symbol', fallback='XAUUSD')
        logger.debug(f"Performing price action check for {action} @ {signal_price} on symbol {mt5_symbol}")
        tick = self.fetcher.get_symbol_tick(mt5_symbol)
        if not tick:
            return 0.0, "Failed to get current market price", None

        current_price_ask = tick.ask
        current_price_bid = tick.bid
        determined_type = None
        reason = "N/A"

        try:
            if action == "BUY":
                # Compare signal price to current ASK price for BUY orders
                if signal_price < current_price_ask:
                    determined_type = mt5.ORDER_TYPE_BUY_LIMIT
                    reason = f"Determined BUY_LIMIT (Signal {signal_price} < Ask {current_price_ask})"
                elif signal_price > current_price_ask:
                    determined_type = mt5.ORDER_TYPE_BUY_STOP
                    reason = f"Determined BUY_STOP (Signal {signal_price} > Ask {current_price_ask})"
                else: # signal_price == current_price_ask
                    # Ambiguous case - could be limit or stop depending on intent/spread
                    # Defaulting to LIMIT for safety, but log warning. Could reject here.
                    determined_type = mt5.ORDER_TYPE_BUY_LIMIT
                    reason = f"Ambiguous BUY: Signal price {signal_price} == Ask {current_price_ask}. Defaulting to LIMIT."
                    logger.warning(reason)

            elif action == "SELL":
                # Compare signal price to current BID price for SELL orders
                if signal_price > current_price_bid:
                    determined_type = mt5.ORDER_TYPE_SELL_LIMIT
                    reason = f"Determined SELL_LIMIT (Signal {signal_price} > Bid {current_price_bid})"
                elif signal_price < current_price_bid:
                    determined_type = mt5.ORDER_TYPE_SELL_STOP
                    reason = f"Determined SELL_STOP (Signal {signal_price} < Bid {current_price_bid})"
                else: # signal_price == current_price_bid
                    # Ambiguous case - defaulting to LIMIT
                    determined_type = mt5.ORDER_TYPE_SELL_LIMIT
                    reason = f"Ambiguous SELL: Signal price {signal_price} == Bid {current_price_bid}. Defaulting to LIMIT."
                    logger.warning(reason)
            else:
                 reason = f"Invalid action '{action}' for price check"
                 logger.error(reason)
                 return 0.0, reason, None

            logger.debug(reason)
            # Simple scoring: 1.0 if a type was determined, 0.0 otherwise.
            score = 1.0 if determined_type is not None else 0.0
            return score, reason, determined_type

        except Exception as e:
            logger.error(f"Exception during price action check: {e}", exc_info=True)
            return 0.0, f"Exception: {e}", None


# Example usage (optional, for testing)
if __name__ == '__main__':
    import configparser
    import os
    import sys
    from logger_setup import setup_logging
    from mt5_connector import MT5Connector # Need connector for fetcher
    from mt5_data_fetcher import MT5DataFetcher # Need fetcher

    # Setup basic logging for test
    test_log_path = os.path.join(os.path.dirname(__file__), '..', 'logs', 'decision_test.log')
    setup_logging(log_file_path=test_log_path, log_level_str='DEBUG')

    # Load dummy config
    example_config_path = os.path.join(os.path.dirname(__file__), '..', 'config', 'config.example.ini')
    if not os.path.exists(example_config_path):
        print(f"ERROR: config.example.ini not found at {example_config_path}. Cannot run test.")
        sys.exit(1)

    config = configparser.ConfigParser()
    config.read(example_config_path)
    # --- IMPORTANT: Fill in REAL MT5 details in config.example.ini for this test to work ---

    if 'YOUR_' in config.get('MT5', 'account', fallback=''):
         print("WARNING: Dummy MT5 credentials found in config. Decision test needs market data and may fail.")
         # sys.exit(1) # Optionally exit

    # --- Test Setup ---
    connector = MT5Connector(config)
    fetcher = MT5DataFetcher(connector)
    decision_maker = DecisionLogic(config, fetcher)

    print("Connecting MT5 for DecisionLogic test...")
    if not connector.connect():
        print("MT5 Connection Failed. Cannot run full decision tests.")
        sys.exit(1)
    print("MT5 Connected.")

    # --- Test Cases ---
    print("\n--- Testing Decision Logic ---")

    # 1. Market Order Signal
    signal_market = {
        "is_signal": True, "action": "BUY", "entry_type": "Market", "entry_price": "Market",
        "stop_loss": 3100, "take_profit": 3150, "sentiment_score": 0.8
    }
    print(f"\nTest 1: Market Order Signal: {signal_market}")
    approved, reason, order_type = decision_maker.decide(signal_market)
    print(f"Result: Approved={approved}, Reason='{reason}', OrderType={order_type}")
    assert approved is True and order_type == mt5.ORDER_TYPE_BUY

    # 2. Pending Order Signal (BUY LIMIT - Price below current Ask) - Assume Ask ~3110
    signal_pending_limit = {
        "is_signal": True, "action": "BUY", "entry_type": "Pending", "entry_price": "3105.00",
        "stop_loss": 3100, "take_profit": 3120, "sentiment_score": 0.7 # Positive sentiment
    }
    print(f"\nTest 2: Pending BUY LIMIT Signal (expect Ask > 3105): {signal_pending_limit}")
    approved, reason, order_type = decision_maker.decide(signal_pending_limit)
    print(f"Result: Approved={approved}, Reason='{reason}', OrderType={order_type}")
    # Approval depends on threshold and actual market price

    # 3. Pending Order Signal (BUY STOP - Price above current Ask) - Assume Ask ~3110
    signal_pending_stop = {
        "is_signal": True, "action": "BUY", "entry_type": "Pending", "entry_price": "3115.00",
        "stop_loss": 3110, "take_profit": 3130, "sentiment_score": 0.7 # Positive sentiment
    }
    print(f"\nTest 3: Pending BUY STOP Signal (expect Ask < 3115): {signal_pending_stop}")
    approved, reason, order_type = decision_maker.decide(signal_pending_stop)
    print(f"Result: Approved={approved}, Reason='{reason}', OrderType={order_type}")
    # Approval depends on threshold and actual market price

    # 4. Pending Order Signal - Low Sentiment (expect rejection)
    signal_pending_low_sentiment = {
        "is_signal": True, "action": "SELL", "entry_type": "Pending", "entry_price": "3120.00", # Assume SELL LIMIT
        "stop_loss": 3125, "take_profit": 3110, "sentiment_score": -0.8 # Very negative sentiment
    }
    print(f"\nTest 4: Pending SELL LIMIT Signal - Low Sentiment: {signal_pending_low_sentiment}")
    approved, reason, order_type = decision_maker.decide(signal_pending_low_sentiment)
    print(f"Result: Approved={approved}, Reason='{reason}', OrderType={order_type}")
    # Expect approved=False if threshold is 0.6

     # 5. Not a signal
    signal_not = {"is_signal": False}
    print(f"\nTest 5: Not a signal: {signal_not}")
    approved, reason, order_type = decision_maker.decide(signal_not)
    print(f"Result: Approved={approved}, Reason='{reason}', OrderType={order_type}")
    assert approved is False

    # --- Cleanup ---
    print("\nDisconnecting MT5...")
    connector.disconnect()
    print("Test finished.")