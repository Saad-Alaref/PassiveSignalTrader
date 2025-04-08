import logging
from .llm_interface import LLMInterface # Use relative import
import json
from .models import SignalData, UpdateData # Import the new data classes

logger = logging.getLogger('TradeBot')

class SignalAnalyzer:
    """
    Analyzes Telegram messages using the LLMInterface to identify trading signals,
    extract parameters, and assess sentiment.
    """
    # Store symbol digits for rounding
    symbol_digits = 2 # Default

    def __init__(self, llm_interface: LLMInterface, data_fetcher, config_service_instance): # Inject service
        """
        Initializes the SignalAnalyzer.

        Args:
            llm_interface (LLMInterface): An instance of the LLMInterface.
            data_fetcher (MT5DataFetcher): Instance for fetching symbol info.
            config_service_instance (ConfigService): The application config service.
        """
        self.llm = llm_interface
        self.fetcher = data_fetcher
        self.config_service = config_service_instance # Store service instance
        self.symbol = self.config_service.get('MT5', 'symbol', fallback='XAUUSD') # Use service
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

    def analyze(self, message_text, image_data=None, context=None, analysis_mode=None):
        """
        Analyzes a message using an LLM to classify it and extract details.

        Args:
            message_text (str): The text content of the message.
            image_data (bytes, optional): Image data associated with the message.
            context (dict, optional): Additional context (price, history, trades). Defaults to None.
            analysis_mode (str, optional): Hint for analysis type ('extract_update_params' for edits). Defaults to None.


        Returns:
            dict: A dictionary containing the analysis type and data (as a dataclass instance).
                  - {'type': 'new_signal', 'data': SignalData(...)}
                  - {'type': 'update', 'data': UpdateData(...)}
                  - {'type': 'ignore', 'data': None}
        """
        logger.info("Analyzing message for signal type (new/update/ignore)...")
        logger.debug(f"Message text: {message_text[:100]}...") # Log snippet

        # Determine prompt type based on analysis mode
        prompt_type_to_use = "analyze_signal" # Default
        if analysis_mode == 'extract_update_params':
            prompt_type_to_use = "analyze_edit_update" # Use a specific prompt for edits
            logger.info("Using 'analyze_edit_update' prompt type for edit analysis.")

        llm_result = self.llm.analyze_message(
            message_text=message_text,
            image_data=image_data,
            context=context, # Pass context
            prompt_type=prompt_type_to_use # Pass determined prompt type
        )
        # --- Log Raw LLM Result ---
        logger.debug(f"Raw LLM analysis result: {llm_result}")
        # --------------------------

        if not llm_result:
            logger.warning("LLM analysis returned no result.")
            return {'type': 'ignore', 'data': None}

        # --- Determine Message Type and Process Result ---
        # If analyzing an edit specifically for update params, force the type to 'update'
        if analysis_mode == 'extract_update_params':
            logger.info("Forcing result type to 'update' for edit analysis.")
            message_type = 'update'
            # Assume the LLM (using the 'analyze_edit_update' prompt) returns SL/TP/Entry directly
            # We'll structure this into an UpdateData object with type 'modify_sltp'
            try:
                # Extract potential SL, TP, and Entry from the LLM result
                # Use .get with fallback to "N/A"
                new_sl_val = llm_result.get('stop_loss', "N/A")
                new_tp_list_raw = llm_result.get('take_profits', ["N/A"])
                new_entry_raw = llm_result.get('entry_price', "N/A") # Check if entry was modified

                # Basic validation/conversion for SL
                validated_sl = "N/A"
                if new_sl_val != "N/A":
                    try:
                        validated_sl = float(new_sl_val)
                    except (ValueError, TypeError):
                        logger.warning(f"Invalid numeric SL '{new_sl_val}' in edit analysis result. Treating as N/A.")
                        validated_sl = "N/A"

                # Basic validation/conversion for TPs
                if not isinstance(new_tp_list_raw, list): new_tp_list_raw = [new_tp_list_raw] # Ensure list
                validated_tps = []
                for tp in new_tp_list_raw:
                    if tp == "N/A" or tp is None or (isinstance(tp, str) and tp.upper() == "OPEN"):
                        validated_tps.append("N/A")
                    else:
                        try:
                            validated_tps.append(float(tp))
                        except (ValueError, TypeError):
                             logger.warning(f"Invalid numeric TP '{tp}' in edit analysis result. Treating as N/A.")
                             validated_tps.append("N/A")
                if not validated_tps: validated_tps = ["N/A"] # Ensure not empty

                # TODO: Handle potential modification of entry price/range if needed.
                # For now, assuming edits primarily target SL/TP. Entry modification might need a different update_type.

                # Determine update type - default to modify_sltp if only SL/TP/Entry change
                # If LLM explicitly provides close_volume/percentage, assume partial_close
                # If LLM explicitly provides entry_price and it's different, assume modify_entry? Needs careful prompting.
                extracted_update_type = llm_result.get('update_type', 'modify_sltp') # Default if not specified by LLM

                # Extract potential close params
                close_vol_raw = llm_result.get('close_volume', 'N/A')
                close_perc_raw = llm_result.get('close_percentage', 'N/A')
                validated_close_vol = "N/A"
                validated_close_perc = "N/A"
                try:
                    if close_vol_raw != "N/A": validated_close_vol = float(close_vol_raw)
                    if close_perc_raw != "N/A": validated_close_perc = float(close_perc_raw)
                    # If volume/perc provided, override type to partial_close
                    if validated_close_vol != "N/A" or validated_close_perc != "N/A":
                        extracted_update_type = "partial_close"
                except (ValueError, TypeError):
                    logger.warning(f"Invalid numeric close vol/perc '{close_vol_raw}/{close_perc_raw}' in edit analysis result. Ignoring.")

                # TODO: Add logic to detect modify_entry based on new_entry_raw if needed

                update_data_obj = UpdateData(
                    update_type=extracted_update_type,
                    symbol=llm_result.get('symbol'), # Get symbol if provided
                    target_trade_index=None, # Edits target specific message, index not needed here
                    new_entry_price=new_entry_raw, # Add extracted entry price
                    new_stop_loss=validated_sl,
                    new_take_profits=validated_tps,
                    close_volume=validated_close_vol,
                    close_percentage=validated_close_perc
                )

                # Override: if message contains 'close' and no SL/TP specified, treat as close_trade
                if (
                    update_data_obj.update_type == "modify_sltp" and
                    (update_data_obj.new_stop_loss == "N/A" or update_data_obj.new_stop_loss is None) and
                    (not update_data_obj.new_take_profits or update_data_obj.new_take_profits == ["N/A"]) and
                    "close" in message_text.lower()
                ):
                    logger.info("Overriding update_type to 'close_trade' based on message content and empty SL/TP.")
                    update_data_obj.update_type = "close_trade"
                logger.info(f"Constructed UpdateData from edit analysis: {update_data_obj}")
                return {'type': 'update', 'data': update_data_obj}

            except Exception as e:
                 logger.error(f"Error processing LLM result during forced update analysis for edit: {e}. LLM Result: {llm_result}", exc_info=True)
                 return {'type': 'ignore', 'data': None} # Ignore if structuring fails

        # --- Original Logic for non-edit analysis ---
        else:
            message_type = llm_result.get("message_type", "ignore")

            if message_type == "ignore":
                logger.info("Message analyzed, classified as ignore by LLM.")
                return {'type': 'ignore', 'data': None}

            # Indent the original logic under the 'else' for non-edit analysis
            elif message_type == "update":
                logger.info("Message analyzed, classified as update by LLM.")
                # --- Create and Validate UpdateData Object ---
                try:
                    # Extract potential close volume/percentage
                    close_vol = llm_result.get('close_volume', 'N/A')
                    close_perc = llm_result.get('close_percentage', 'N/A')

                    update_data_obj = UpdateData(
                        update_type=llm_result.get('update_type', 'unknown'),
                        symbol=llm_result.get('symbol'),
                        target_trade_index=llm_result.get('target_trade_index'), # Can be None
                        new_stop_loss=llm_result.get('new_stop_loss', 'N/A'),
                        new_take_profits=llm_result.get('new_take_profits', ['N/A']),
                        close_volume=float(close_vol) if close_vol != "N/A" else "N/A",
                        close_percentage=float(close_perc) if close_perc != "N/A" else "N/A"
                    )
                except (ValueError, TypeError) as e:
                     logger.error(f"LLM update result contains invalid numeric data for close vol/perc: {e}. Result: {llm_result}")
                     return {'type': 'ignore', 'data': None} # Ignore if basic structure is wrong

                # Basic validation for update_type
                if update_data_obj.update_type not in ["modify_sltp", "move_sl", "set_be", "close_trade", "cancel_pending", "unknown", "partial_close"]: # Added partial_close
                     logger.warning(f"LLM returned invalid update_type: {update_data_obj.update_type}. Treating as unknown.")
                     update_data_obj.update_type = 'unknown'

                # Validate numeric SL and list/numeric TPs
                try:
                    if update_data_obj.new_stop_loss != "N/A":
                        update_data_obj.new_stop_loss = float(update_data_obj.new_stop_loss)

                    if not isinstance(update_data_obj.new_take_profits, list):
                        logger.error(f"LLM update result 'new_take_profits' is not a list: {update_data_obj.new_take_profits}. Result: {llm_result}")
                        update_data_obj.new_take_profits = ["N/A"] # Fallback
                    else:
                        # Validate each item in the list
                        validated_tps = []
                        invalid_found = False
                        for tp_val in update_data_obj.new_take_profits:
                            if tp_val == "N/A" or tp_val is None: # Handle None as well
                                validated_tps.append("N/A")
                            else:
                                try:
                                    validated_tps.append(float(tp_val))
                                except (ValueError, TypeError):
                                    logger.warning(f"Invalid numeric TP value '{tp_val}' in update new_take_profits list. Replacing with 'N/A'.")
                                    validated_tps.append("N/A")
                                    invalid_found = True
                        update_data_obj.new_take_profits = validated_tps if validated_tps else ["N/A"] # Ensure list isn't empty
                        if invalid_found:
                             logger.error(f"LLM update result contained invalid numeric TP data. Corrected list: {update_data_obj.new_take_profits}. Original Result: {llm_result}")

                except (ValueError, TypeError) as e:
                     logger.error(f"LLM update result contains invalid numeric SL/TP data: {e}. Result: {llm_result}")
                     # Mark SL as N/A if it was invalid
                     if update_data_obj.new_stop_loss != "N/A": update_data_obj.new_stop_loss = "N/A"
                     # TP list validation already handled above
                     pass

                logger.info(f"Update details: Type={update_data_obj.update_type}, Symbol={update_data_obj.symbol}, NewSL={update_data_obj.new_stop_loss}, NewTPs={update_data_obj.new_take_profits}")
                return {'type': 'update', 'data': update_data_obj}


            # Indent the original logic under the 'else' for non-edit analysis
            elif message_type == "new_signal":
                logger.info("Message analyzed, classified as new signal by LLM.")
                # --- Validate New Signal Data ---
                if not llm_result.get("is_signal") is True:
                     logger.warning(f"LLM classified as new_signal but is_signal flag is not True. Result: {llm_result}")
                     return {'type': 'ignore', 'data': None} # Treat as ignore

                expected_keys = ["is_signal", "action", "entry_type", "entry_price",
                                 "stop_loss", "take_profits", "symbol"]
                if not all(key in llm_result for key in expected_keys):
                    missing_keys = [k for k in expected_keys if k not in llm_result]
                    logger.error(f"LLM new_signal result missing expected keys: {missing_keys}. Result: {llm_result}")
                    return {'type': 'ignore', 'data': None}

                # --- Post-processing, Validation, and Entry Price Strategy Application ---
                entry_range_strategy = 'midpoint'  # Default if not set below
                entry_price_raw = llm_result.get("entry_price")
                entry_price_final = entry_price_raw # Default to raw value
                action = llm_result.get("action")

                if isinstance(entry_price_raw, str) and '-' in entry_price_raw and entry_price_raw not in ["Market", "N/A"]:
                    log_prefix_analyzer = f"[SignalAnalyzer][MsgID: {context.get('message_history', [{'id': 'N/A'}])[-1].get('id', 'N/A') if context and context.get('message_history') else 'N/A'}]"
                    try:
                        low_str, high_str = entry_price_raw.split('-', 1)
                        low = float(low_str.strip())
                        high = float(high_str.strip())
                        if low > high: low, high = high, low

                        entry_range_strategy = self.config_service.get('Strategy', 'entry_range_strategy', fallback='midpoint').lower()
                        logger.info(f"{log_prefix_analyzer} Applying entry range strategy '{entry_range_strategy}' to range '{entry_price_raw}'.")

                        if entry_range_strategy == 'midpoint':
                            entry_price_final = round((low + high) / 2.0, self.symbol_digits)
                        elif entry_range_strategy in ['closest', 'farthest']:
                            tick = self.fetcher.get_symbol_tick(llm_result.get("symbol"))
                            if tick:
                                current_market_price = tick.ask if action == "BUY" else tick.bid
                                if entry_range_strategy == 'closest':
                                    entry_price_final = low if abs(low - current_market_price) <= abs(high - current_market_price) else high
                                else: # farthest
                                    entry_price_final = low if abs(low - current_market_price) > abs(high - current_market_price) else high
                            else:
                                logger.warning(f"{log_prefix_analyzer} Could not get current tick for {entry_range_strategy} strategy. Falling back to midpoint for range '{entry_price_raw}'.")
                                entry_price_final = round((low + high) / 2.0, self.symbol_digits)
                        # Keep entry_price_final as the raw string for 'distributed' strategy
                        elif entry_range_strategy != 'distributed':
                             logger.warning(f"{log_prefix_analyzer} Invalid entry_range_strategy '{entry_range_strategy}'. Falling back to midpoint for range '{entry_price_raw}'.")
                             entry_price_final = round((low + high) / 2.0, self.symbol_digits)

                        # Only log if we calculated a numeric price
                        if entry_range_strategy != 'distributed':
                            logger.info(f"{log_prefix_analyzer} Determined entry price: {entry_price_final}")

                    except (ValueError, TypeError) as e:
                        logger.error(f"{log_prefix_analyzer} Failed to parse entry range '{entry_price_raw}': {e}. Result: {llm_result}")
                        return {'type': 'ignore', 'data': None}

                # --- Create and Validate SignalData Object ---
                try:
                    sl_val = llm_result.get("stop_loss")
                    validated_sl = float(sl_val) if sl_val != "N/A" else "N/A"

                    tp_list = llm_result.get("take_profits", ["N/A"])
                    if not isinstance(tp_list, list):
                         logger.error(f"LLM new_signal result 'take_profits' is not a list: {tp_list}. Result: {llm_result}")
                         return {'type': 'ignore', 'data': None}
                    validated_tps = []
                    for tp in tp_list:
                        # Allow "N/A", None, or "OPEN" (case-insensitive) as non-numeric TPs
                        if tp == "N/A" or tp is None or (isinstance(tp, str) and tp.upper() == "OPEN"):
                            validated_tps.append("N/A") # Standardize non-numeric TPs to "N/A"
                        else:
                            try:
                                validated_tps.append(float(tp))
                            except (ValueError, TypeError):
                                logger.error(f"LLM new_signal result contains invalid numeric TP value '{tp}' in list (and not 'OPEN'). Result: {llm_result}")
                                return {'type': 'ignore', 'data': None} # Ignore if it's not numeric and not 'OPEN'/'N/A'

                    # Validate final entry price if it's supposed to be numeric
                    final_entry_price_for_obj = entry_price_final
                    if entry_range_strategy != 'distributed' and entry_price_final not in ["Market", "N/A"]:
                         final_entry_price_for_obj = float(entry_price_final) # Ensure float if not range/market/na

                    signal_data_obj = SignalData(
                        is_signal=True,
                        action=llm_result.get("action"),
                        entry_type=llm_result.get("entry_type"),
                        entry_price=final_entry_price_for_obj, # Use potentially converted float or raw string
                        stop_loss=validated_sl,
                        take_profits=validated_tps if validated_tps else ["N/A"],
                        symbol=llm_result.get("symbol"),
                        sentiment_score=float(llm_result["sentiment_score"]) if "sentiment_score" in llm_result and llm_result["sentiment_score"] is not None else None
                    )

                except (ValueError, TypeError) as e:
                     logger.error(f"LLM new_signal result contains invalid numeric data: {e}. Result: {llm_result}")
                     return {'type': 'ignore', 'data': None}

                logger.info(f"New signal identified and validated: Action={signal_data_obj.action}, Entry={signal_data_obj.entry_price}, SL={signal_data_obj.stop_loss}, TPs={signal_data_obj.take_profits}, Symbol={signal_data_obj.symbol}")
                return {'type': 'new_signal', 'data': signal_data_obj}

            # Indent the original logic under the 'else' for non-edit analysis
            else:
                logger.warning(f"LLM returned unknown message_type: {message_type}. Result: {llm_result}")
                return {'type': 'ignore', 'data': None}

    # This method seems redundant now as analyze handles updates with context
    # We might remove it later, but update signature for now.
    def analyze_update(self, message_text, image_data=None, context=None):
        """
        Analyzes a message (likely an edit or reply) for missing SL/TP info.
        DEPRECATED: Use analyze() instead.
        """
        logger.warning("analyze_update is deprecated. Use analyze() which handles all types.")
        # Call analyze with the appropriate prompt type (or let analyze decide)
        result = self.analyze(message_text, image_data, context)
        if result and result['type'] == 'update':
            return result['data'] # Return the UpdateData object
        elif result and result['type'] == 'new_signal':
             # If re-analysis yields a new signal, maybe log a warning?
             logger.warning("analyze_update called, but message re-analyzed as new_signal.")
             return None
        else: # Ignore or error
             return None


# Example usage (optional, for testing)
if __name__ == '__main__':
    # import configparser # No longer needed
    import os
    import sys
    import json # Need json for printing results
    from logger_setup import setup_logging
    from llm_interface import LLMInterface # Need LLMInterface for testing
    from config_service import ConfigService # Import service for testing
    # Need dummy fetcher for init
    from mt5_data_fetcher import MT5DataFetcher
    from mt5_connector import MT5Connector

    # Setup basic logging for test
    test_log_path = os.path.join(os.path.dirname(__file__), '..', 'logs', 'analyzer_test.log')
    setup_logging(log_file_path=test_log_path, log_level_str='DEBUG')

    # --- IMPORTANT: Ensure config/config.ini exists and has REAL Gemini API Key ---
    try:
        # Instantiate ConfigService directly for the test
        test_config_service = ConfigService(config_file='../config/config.ini') # Adjust path if needed
    except Exception as e:
        print(f"ERROR: Failed to load config/config.ini for testing: {e}")
        sys.exit(1)

    # Check if dummy values might still be present (optional check)
    if 'YOUR_' in test_config_service.get('Gemini', 'api_key', fallback=''):
         print("WARNING: Dummy Gemini API key might be present in config.ini. Analyzer test may fail.")

    # Instantiate dependencies with the test service instance
    llm_interface = LLMInterface(test_config_service)
    # Create dummy fetcher
    dummy_connector = MT5Connector(test_config_service) # Needs service but won't connect
    dummy_fetcher = MT5DataFetcher(dummy_connector)
    # Instantiate analyzer with the test service and dependencies
    analyzer = SignalAnalyzer(llm_interface, dummy_fetcher, test_config_service)

    if llm_interface.model:
        print("Testing Analyzer with a sample signal message...")
        test_message_signal = """
🧑‍💻XAUUSD Buy gold  Zone 3106 - 3108
🔹SL 3103
🔹TP 3112- 3125- open
"""
        result_signal = analyzer.analyze(test_message_signal)
        print("--- Signal Analysis Result ---")
        if result_signal and result_signal['data']:
            # Convert dataclass to dict for json printing if needed, or print directly
            # print(json.dumps(dataclasses.asdict(result_signal['data']), indent=2))
            print(result_signal)
        else:
            print("Analysis failed or not a signal.")

        print("\nTesting Analyzer with a sample non-signal message...")
        test_message_non_signal = "Gold is looking bullish today, might break 3150."
        result_non_signal = analyzer.analyze(test_message_non_signal)
        print("--- Non-Signal Analysis Result ---")
        if result_non_signal:
             print(result_non_signal) # Should show {'type': 'ignore', 'data': None}
        else:
            print("Analysis failed.") # Should not happen

        print("\nTesting Analyzer with a sample update message...")
        test_message_update = "SL for the last BUY moved to 3105"
        # Use analyze() for updates too now
        result_update = analyzer.analyze(test_message_update)
        print("--- Update Analysis Result ---")
        if result_update and result_update['type'] == 'update' and result_update['data']:
            # print(json.dumps(dataclasses.asdict(result_update['data']), indent=2))
             print(result_update)
        else:
            print("Update analysis failed or not classified as update.")

    else:
        print("LLM Interface could not be initialized. Cannot run analyzer tests.")

    print("\nTest finished.")