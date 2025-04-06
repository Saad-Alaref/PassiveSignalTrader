import logging
from .llm_interface import LLMInterface # Use relative import
import json # For potential fallback parsing if needed

logger = logging.getLogger('TradeBot')

class SignalAnalyzer:
    """
    Analyzes Telegram messages using the LLMInterface to identify trading signals,
    extract parameters, and assess sentiment.
    """
    # Store symbol digits for rounding
    symbol_digits = 2 # Default

    def __init__(self, llm_interface: LLMInterface, data_fetcher, config):
        """
        Initializes the SignalAnalyzer.

        Args:
            llm_interface (LLMInterface): An instance of the LLMInterface.
            data_fetcher (MT5DataFetcher): Instance for fetching symbol info.
            config (configparser.ConfigParser): Application configuration.
        """
        self.llm = llm_interface
        self.fetcher = data_fetcher
        self.symbol = config.get('MT5', 'symbol', fallback='XAUUSD')
        self.config = config # Store the config object
        self._fetch_symbol_digits() # Fetch digits on init

    def _fetch_symbol_digits(self):
        """Fetches and stores the number of digits for the configured symbol."""
        try:
            symbol_info = self.fetcher.get_symbol_info(self.symbol)
            if symbol_info:
                self.symbol_digits = symbol_info.digits
                logger.info(f"Fetched symbol digits for {self.symbol}: {self.symbol_digits}")
            else:
                logger.warning(f"Could not fetch symbol info for {self.symbol}. Using default digits: {self.symbol_digits}")
        except Exception as e:
            logger.error(f"Error fetching symbol digits: {e}. Using default.", exc_info=True)

    def analyze(self, message_text, image_data=None, context=None):
        """
        Analyzes a message using an LLM to classify it and extract details.

        Args:
            message_text (str): The text content of the message.
            image_data (bytes, optional): Image data associated with the message.

        Returns:
            context (dict, optional): Additional context (price, history, trades). Defaults to None.

        Returns:
            dict: A dictionary containing the analysis results. Structure depends on type:
                  - {'type': 'new_signal', 'data': {... full signal details ...}}
                  - {'type': 'update', 'data': {'update_type': ..., 'symbol': ..., 'new_stop_loss': ..., 'new_take_profits': [...]}}
                  - {'type': 'ignore'}
        """
        logger.info("Analyzing message for signal type (new/update/ignore)...")
        logger.debug(f"Message text: {message_text[:100]}...") # Log snippet

        # Use the updated "analyze_signal" prompt which now handles classification
        llm_result = self.llm.analyze_message(
            message_text=message_text,
            image_data=image_data,
            context=context, # Pass context
            prompt_type="analyze_signal"
        )
        # --- Log Raw LLM Result ---
        logger.debug(f"Raw LLM analysis result: {llm_result}")
        # --------------------------

        if not llm_result:
            logger.warning("LLM analysis returned no result.")
            return {'type': 'ignore'}

        # --- Determine Message Type ---
        message_type = llm_result.get("message_type", "ignore")

        if message_type == "ignore":
            logger.info("Message analyzed, classified as ignore by LLM.")
            return {'type': 'ignore'}

        elif message_type == "update":
            logger.info("Message analyzed, classified as update by LLM.")
            # --- Validate Update Data ---
            update_data = {}
            update_data['update_type'] = llm_result.get('update_type', 'unknown')
            update_data['symbol'] = llm_result.get('symbol') # Can be None
            update_data['new_stop_loss'] = llm_result.get('new_stop_loss', 'N/A')
            # Expecting a list now, default to ["N/A"]
            update_data['new_take_profits'] = llm_result.get('new_take_profits', ['N/A'])

            # Basic validation for update fields
            if update_data['update_type'] not in ["modify_sltp", "move_sl", "set_be", "close_trade", "cancel_pending", "unknown"]:
                 logger.warning(f"LLM returned invalid update_type: {update_data['update_type']}. Treating as unknown.")
                 update_data['update_type'] = 'unknown'

            try:
                if update_data['new_stop_loss'] != "N/A":
                    float(update_data['new_stop_loss'])
                # Validate the list of TPs
                if not isinstance(update_data['new_take_profits'], list):
                    logger.error(f"LLM update result 'new_take_profits' is not a list: {update_data['new_take_profits']}. Result: {llm_result}")
                    update_data['new_take_profits'] = ["N/A"] # Fallback
                else:
                    # Validate each item in the list
                    valid_tps = []
                    invalid_found = False
                    for tp_val in update_data['new_take_profits']:
                        if tp_val == "N/A":
                            valid_tps.append("N/A")
                        else:
                            try:
                                valid_tps.append(float(tp_val))
                            except (ValueError, TypeError):
                                logger.warning(f"Invalid numeric TP value '{tp_val}' in new_take_profits list. Replacing with 'N/A'.")
                                valid_tps.append("N/A")
                                invalid_found = True
                    update_data['new_take_profits'] = valid_tps if valid_tps else ["N/A"] # Ensure list isn't empty
                    if invalid_found:
                         logger.error(f"LLM update result contained invalid numeric TP data. Corrected list: {update_data['new_take_profits']}. Original Result: {llm_result}")

            except (ValueError, TypeError) as e:
                 logger.error(f"LLM update result contains invalid numeric SL/TP data: {e}. Result: {llm_result}")
                 # Don't ignore the whole update, just mark SL/TP as N/A if invalid
                 if update_data['new_stop_loss'] != "N/A": update_data['new_stop_loss'] = "N/A"
                 # No specific correction needed here now, validation loop handles it
                 pass

            logger.info(f"Update details: Type={update_data['update_type']}, Symbol={update_data['symbol']}, NewSL={update_data['new_stop_loss']}, NewTPs={update_data['new_take_profits']}")
            return {'type': 'update', 'data': update_data}


        elif message_type == "new_signal":
            logger.info("Message analyzed, classified as new signal by LLM.")
            # --- Validate New Signal Data ---
            # Ensure is_signal is True if type is new_signal
            if not llm_result.get("is_signal") is True:
                 logger.warning(f"LLM classified as new_signal but is_signal flag is not True. Result: {llm_result}")
                 return {'type': 'ignore'} # Treat as ignore

            # Define expected keys for a new signal (including symbol)
            expected_keys = ["is_signal", "action", "entry_type", "entry_price",
                             "stop_loss", "take_profits", "symbol"] # Changed take_profit to take_profits
            if not all(key in llm_result for key in expected_keys):
                missing_keys = [k for k in expected_keys if k not in llm_result]
                logger.error(f"LLM new_signal result missing expected keys: {missing_keys}. Result: {llm_result}")
                return {'type': 'ignore'}

            # --- Post-processing, Validation, and Entry Price Strategy Application ---
            entry_price_raw = llm_result.get("entry_price")
            entry_price_final = entry_price_raw # Default to raw value
            action = llm_result.get("action") # Needed for closest/farthest strategy

            # Handle Zone Entry based on Strategy HERE
            if isinstance(entry_price_raw, str) and '-' in entry_price_raw and entry_price_raw not in ["Market", "N/A"]:
                log_prefix = f"[SignalAnalyzer][MsgID: {context.get('message_history', [{'id': 'N/A'}])[-1].get('id', 'N/A') if context and context.get('message_history') else 'N/A'}]" # Approx log prefix
                try:
                    low_str, high_str = entry_price_raw.split('-', 1)
                    low = float(low_str.strip())
                    high = float(high_str.strip())
                    if low > high: low, high = high, low # Ensure low <= high

                    # Read strategy dynamically
                    entry_range_strategy = self.config.get('Strategy', 'entry_range_strategy', fallback='midpoint').lower()
                    logger.info(f"{log_prefix} Applying entry range strategy '{entry_range_strategy}' to range '{entry_price_raw}'.")

                    if entry_range_strategy == 'midpoint':
                        entry_price_final = round((low + high) / 2.0, self.symbol_digits)
                    elif entry_range_strategy in ['closest', 'farthest']:
                        tick = self.fetcher.get_symbol_tick(llm_result.get("symbol")) # Use symbol from LLM result
                        if tick:
                            current_market_price = tick.ask if action == "BUY" else tick.bid
                            if entry_range_strategy == 'closest':
                                entry_price_final = low if abs(low - current_market_price) <= abs(high - current_market_price) else high
                            else: # farthest
                                entry_price_final = low if abs(low - current_market_price) > abs(high - current_market_price) else high
                        else:
                            logger.warning(f"{log_prefix} Could not get current tick for {entry_range_strategy} strategy. Falling back to midpoint for range '{entry_price_raw}'.")
                            entry_price_final = round((low + high) / 2.0, self.symbol_digits)
                    else:
                         logger.warning(f"{log_prefix} Invalid entry_range_strategy '{entry_range_strategy}'. Falling back to midpoint for range '{entry_price_raw}'.")
                         entry_price_final = round((low + high) / 2.0, self.symbol_digits)

                    logger.info(f"{log_prefix} Determined entry price: {entry_price_final}")

                except (ValueError, TypeError) as e:
                    logger.error(f"{log_prefix} Failed to parse entry range '{entry_price_raw}': {e}. Result: {llm_result}")
                    return {'type': 'ignore'} # Ignore signal if range format is bad

            # Update the result dict with the final calculated entry price
            llm_result["entry_price"] = entry_price_final

            # Validate numeric fields
            try:
                # Validate numeric entry price if it's not Market or N/A (it should be a float now if it wasn't a range)
                if entry_price_final not in ["Market", "N/A"]:
                    float(entry_price_final) # Check if it's a valid float
                if llm_result.get("stop_loss") != "N/A":
                    float(llm_result["stop_loss"])
                # Validate take_profits list
                take_profits_list = llm_result.get("take_profits", ["N/A"])
                if not isinstance(take_profits_list, list):
                    logger.error(f"LLM new_signal result 'take_profits' is not a list: {take_profits_list}. Result: {llm_result}")
                    return {'type': 'ignore'}
                valid_tps = []
                for tp_val in take_profits_list:
                    if tp_val == "N/A":
                        valid_tps.append("N/A")
                    else:
                        try:
                            valid_tps.append(float(tp_val))
                        except (ValueError, TypeError):
                             logger.error(f"LLM new_signal result contains invalid numeric TP value '{tp_val}' in list. Result: {llm_result}")
                             return {'type': 'ignore'} # Treat as invalid signal if TP format is wrong
                llm_result["take_profits"] = valid_tps if valid_tps else ["N/A"] # Ensure list isn't empty

                if "sentiment_score" in llm_result and llm_result["sentiment_score"] is not None:
                     float(llm_result["sentiment_score"])
            except (ValueError, TypeError) as e:
                 logger.error(f"LLM new_signal result contains invalid numeric data after processing: {e}. Result: {llm_result}")
                 return {'type': 'ignore'}

            logger.info(f"New signal identified and validated: Action={llm_result.get('action')}, Entry={llm_result.get('entry_price')}, SL={llm_result.get('stop_loss')}, TPs={llm_result.get('take_profits')}, Symbol={llm_result.get('symbol')}")
            # Return the full LLM result dict under the 'data' key
            return {'type': 'new_signal', 'data': llm_result}

        else:
            logger.warning(f"LLM returned unknown message_type: {message_type}. Result: {llm_result}")
            return {'type': 'ignore'}

    # This method seems redundant now as analyze handles updates with context
    # We might remove it later, but update signature for now.
    def analyze_update(self, message_text, image_data=None, context=None):
        """
        Analyzes a message (likely an edit or reply) for missing SL/TP info.

        Args:
            message_text (str): The text content of the message.
            image_data (bytes, optional): Image data associated with the message.

        Returns:
            context (dict, optional): Additional context. Defaults to None.

        Returns:
            dict or None: A dictionary containing the update analysis results
                          (provides_sl, sl_price, provides_tp, tp_prices), or None if analysis fails.
        """
        logger.info("Analyzing message for potential signal update (SL/TP)...")
        logger.debug(f"Update message text: {message_text[:100]}...")

        update_result = self.llm.analyze_message(
            message_text=message_text,
            image_data=image_data,
            context=context, # Pass context
            prompt_type="analyze_edit_or_reply"
        )
        # --- Log Raw LLM Result ---
        logger.debug(f"Raw LLM update analysis result: {update_result}")
        # --------------------------

        if not update_result:
            logger.warning("LLM update analysis returned no result.")
            return None

        # Basic validation
        expected_keys = ["provides_sl", "sl_price", "provides_tp", "tp_prices"] # Changed tp_price to tp_prices
        if not all(key in update_result for key in expected_keys):
            logger.error(f"LLM update analysis result missing expected keys. Result: {update_result}")
            return None

        # Log outcome
        if update_result.get("provides_sl") or update_result.get("provides_tp"):
            logger.info(f"Signal update identified: SL={update_result.get('sl_price')}, TPs={update_result.get('tp_prices')}")
        else:
            logger.info("Message analyzed, not identified as providing SL/TP update.")
            # Return None if no update found? Or return the dict showing false?
            # Returning dict for clarity, calling code can check boolean flags.

        # Further validation
        try:
            if update_result.get("sl_price") != "N/A":
                float(update_result["sl_price"])
            # Validate tp_prices list
            tp_prices_list = update_result.get("tp_prices", ["N/A"])
            if not isinstance(tp_prices_list, list):
                 logger.error(f"LLM update analysis result 'tp_prices' is not a list: {tp_prices_list}. Result: {update_result}")
                 return None
            valid_tps = []
            for tp_val in tp_prices_list:
                 if tp_val == "N/A":
                      valid_tps.append("N/A")
                 else:
                      try:
                           valid_tps.append(float(tp_val))
                      except (ValueError, TypeError):
                           logger.error(f"LLM update analysis result contains invalid numeric TP value '{tp_val}' in list. Result: {update_result}")
                           return None # Invalid data
            update_result["tp_prices"] = valid_tps if valid_tps else ["N/A"]
        except (ValueError, TypeError) as e:
             logger.error(f"LLM update analysis result contains invalid numeric data: {e}. Result: {update_result}")
             return None # Invalid data

        return update_result


# Example usage (optional, for testing)
if __name__ == '__main__':
    import configparser
    import os
    import sys
    from logger_setup import setup_logging
    from llm_interface import LLMInterface # Need LLMInterface for testing

    # Setup basic logging for test
    test_log_path = os.path.join(os.path.dirname(__file__), '..', 'logs', 'analyzer_test.log')
    setup_logging(log_file_path=test_log_path, log_level_str='DEBUG')

    # Load dummy config
    example_config_path = os.path.join(os.path.dirname(__file__), '..', 'config', 'config.example.ini')
    if not os.path.exists(example_config_path):
        print(f"ERROR: config.example.ini not found at {example_config_path}. Cannot run test.")
        sys.exit(1)

    config = configparser.ConfigParser()
    config.read(example_config_path)
    # --- IMPORTANT: Fill in REAL Gemini API Key in config.example.ini for this test to work ---

    if 'YOUR_' in config.get('Gemini', 'api_key', fallback=''):
         print("WARNING: Dummy Gemini API key found in config. Analyzer test will fail.")
         # sys.exit(1) # Optionally exit

    llm_interface = LLMInterface(config)
    analyzer = SignalAnalyzer(llm_interface)

    if llm_interface.model:
        print("Testing Analyzer with a sample signal message...")
        test_message_signal = """
🧑‍💻XAUUSD Buy gold  Zone 3106 - 3108
🔹SL 3103
🔹TP 3112- 3125- open
"""
        result_signal = analyzer.analyze(test_message_signal)
        print("--- Signal Analysis Result ---")
        if result_signal:
            print(json.dumps(result_signal, indent=2))
        else:
            print("Analysis failed or not a signal.")

        print("\nTesting Analyzer with a sample non-signal message...")
        test_message_non_signal = "Gold is looking bullish today, might break 3150."
        result_non_signal = analyzer.analyze(test_message_non_signal)
        print("--- Non-Signal Analysis Result ---")
        if result_non_signal:
             print(json.dumps(result_non_signal, indent=2)) # Should not print if logic is correct
        else:
            print("Analysis failed or not a signal.")

        print("\nTesting Analyzer with a sample update message...")
        test_message_update = "SL for the last BUY moved to 3105"
        result_update = analyzer.analyze_update(test_message_update)
        print("--- Update Analysis Result ---")
        if result_update:
            print(json.dumps(result_update, indent=2))
        else:
            print("Update analysis failed.")

    else:
        print("LLM Interface could not be initialized. Cannot run analyzer tests.")

    print("\nTest finished.")