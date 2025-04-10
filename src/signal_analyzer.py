import logging
import json
from .llm_interface import LLMInterface
from .mt5_data_fetcher import MT5DataFetcher
from .models import SignalData, UpdateData # Import dataclasses
from .config_service import config_service # Import service

logger = logging.getLogger('TradeBot')

class SignalAnalyzer:
    """
    Analyzes Telegram messages using an LLM to identify trading signals or updates.
    """

    def __init__(self, llm_interface: LLMInterface, data_fetcher: MT5DataFetcher, config_service_instance): # Inject service
        """
        Initializes the SignalAnalyzer.

        Args:
            llm_interface (LLMInterface): Instance for interacting with the LLM.
            data_fetcher (MT5DataFetcher): Instance for fetching market data.
            config_service_instance (ConfigService): The application config service.
        """
        self.llm = llm_interface
        self.fetcher = data_fetcher
        self.config_service = config_service_instance # Store service instance
        logger.info("SignalAnalyzer initialized.")

    def _validate_price(self, price_str, field_name="Price"):
        """Validates and converts a price string (SL/TP/Entry) to float or 'N/A'."""
        if price_str == "N/A" or price_str is None:
            return "N/A"
        try:
            return float(price_str)
        except (ValueError, TypeError):
            logger.warning(f"Invalid numeric {field_name} '{price_str}'. Treating as N/A.")
            return "N/A"

    def _validate_take_profits(self, tp_list_raw):
        """Validates and converts a list of TP values."""
        if not isinstance(tp_list_raw, list):
            tp_list_raw = [tp_list_raw] # Ensure list
        validated_tps = []
        for tp in tp_list_raw:
            if tp == "N/A" or tp is None or (isinstance(tp, str) and tp.upper() == "OPEN"):
                validated_tps.append("N/A")
            else:
                validated_tps.append(self._validate_price(tp, "Take Profit"))
        return validated_tps if validated_tps else ["N/A"] # Ensure not empty

    def _validate_numeric(self, value_raw, field_name):
        """Validates and converts generic numeric fields (volume, percentage)"""
        if value_raw == "N/A" or value_raw is None:
            return "N/A"
        try:
            return float(value_raw)
        except (ValueError, TypeError):
            logger.warning(f"Invalid numeric {field_name} '{value_raw}'. Treating as N/A.")
            return "N/A"

    def analyze(self, message_text, image_data=None, context=None, analysis_mode=None):
        """
        Analyzes a message using an LLM to classify it and extract details.

        Args:
            message_text (str): The text content of the message.
            image_data (bytes, optional): Image data associated with the message.
            context (dict, optional): Additional context (price, history, trades). Defaults to None.
            analysis_mode (str, optional): Hint for analysis type (e.g., 'extract_update_params' for edits). Defaults to None. (Currently unused).


        Returns:
            dict: A dictionary containing the analysis type and data (as a dataclass instance).
                  - {'type': 'new_signal', 'data': SignalData(...)}
                  - {'type': 'update', 'data': UpdateData(...)}
                  - {'type': 'ignore', 'data': None}
        """
        logger.info("Analyzing message for signal type (new/update/ignore)...")
        logger.debug(f"Message text: {message_text[:100]}...") # Log snippet

        # Always use the main analysis prompt. The LLM should understand the context of an edit/reply.
        prompt_type_to_use = "analyze_signal"

        # Ignore image_data, only analyze text
        llm_result = self.llm.analyze_message(
            message_text=message_text,
            image_data=None,
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
        message_type = llm_result.get('message_type', 'ignore') # Default to ignore if missing

        if message_type == 'new_signal':
            # --- Process New Signal ---
            try:
                # Validate required fields for a new signal
                action = llm_result.get('action')
                entry_type = llm_result.get('entry_type')
                entry_price_raw = llm_result.get('entry_price') # Keep raw for range processing
                stop_loss_raw = llm_result.get('stop_loss', "N/A")
                take_profits_raw = llm_result.get('take_profits', ["N/A"])
                symbol = llm_result.get('symbol') # Optional, might default later
                sentiment_score = llm_result.get('sentiment_score') # Optional

                if not action or action.upper() not in ["BUY", "SELL"]:
                    raise ValueError(f"Missing or invalid 'action': {action}")
                if not entry_type or entry_type not in ["Market", "Pending"]:
                     raise ValueError(f"Missing or invalid 'entry_type': {entry_type}")
                if entry_type == "Pending" and not entry_price_raw:
                     raise ValueError("Missing 'entry_price' for Pending order")

                # Validate numerics (SL/TP)
                validated_sl = self._validate_price(stop_loss_raw, "Stop Loss")
                validated_tps = self._validate_take_profits(take_profits_raw)

                # Validate sentiment score
                validated_sentiment = None
                if sentiment_score is not None:
                    try:
                        validated_sentiment = float(sentiment_score)
                        # Clamp score between -1.0 and 1.0
                        validated_sentiment = max(-1.0, min(1.0, validated_sentiment))
                    except (ValueError, TypeError):
                         logger.warning(f"Invalid sentiment score '{sentiment_score}'. Ignoring.")
                         validated_sentiment = None


                signal_data = SignalData(
                    is_signal=True,
                    action=action.upper(),
                    entry_type=entry_type,
                    entry_price=entry_price_raw, # Store raw price/range
                    stop_loss=validated_sl,
                    take_profits=validated_tps,
                    symbol=symbol, # Store symbol if provided
                    sentiment_score=validated_sentiment
                )
                logger.info(f"Message analyzed, classified as new signal by LLM.")
                logger.debug(f"Constructed SignalData: {signal_data}")
                return {'type': 'new_signal', 'data': signal_data}

            except Exception as e:
                logger.error(f"Error processing 'new_signal' data from LLM: {e}. LLM Result: {llm_result}", exc_info=True)
                return {'type': 'ignore', 'data': None} # Ignore if structuring fails

        elif message_type == 'update':
            # --- Process Update ---
            try:
                # Validate required fields for an update
                update_type = llm_result.get('update_type')
                if not update_type:
                    raise ValueError("Missing 'update_type' in LLM response")

                # Validate numerics (SL/TP/Volume/Percentage)
                new_sl_raw = llm_result.get('new_stop_loss', "N/A")
                new_tp_list_raw = llm_result.get('new_take_profits', ["N/A"])
                close_vol_raw = llm_result.get('close_volume', "N/A")
                close_perc_raw = llm_result.get('close_percentage', "N/A")

                validated_sl = self._validate_price(new_sl_raw, "New Stop Loss")
                validated_tps = self._validate_take_profits(new_tp_list_raw)
                validated_close_vol = self._validate_numeric(close_vol_raw, "Close Volume")
                validated_close_perc = self._validate_numeric(close_perc_raw, "Close Percentage")

                # Construct UpdateData object
                update_data_obj = UpdateData(
                    update_type=update_type,
                    symbol=llm_result.get('symbol'), # Optional hint
                    target_trade_index=llm_result.get('target_trade_index'), # Optional hint
                    new_entry_price=llm_result.get('new_entry_price', "N/A"), # Optional
                    new_stop_loss=validated_sl,
                    new_take_profits=validated_tps,
                    close_volume=validated_close_vol,
                    close_percentage=validated_close_perc
                )

                # Override: if message contains 'close' and no SL/TP specified, treat as close_trade
                # This logic remains relevant even when parsing standard 'update' types
                if (
                    update_data_obj.update_type == "modify_sltp" and
                    (update_data_obj.new_stop_loss == "N/A" or update_data_obj.new_stop_loss is None) and
                    (not update_data_obj.new_take_profits or update_data_obj.new_take_profits == ["N/A"]) and
                    "close" in message_text.lower()
                ):
                    logger.info("Overriding update_type to 'close_trade' based on message content and empty SL/TP.")
                    update_data_obj.update_type = "close_trade"

                # Basic validation for update_type enum
                if update_data_obj.update_type not in ["modify_sltp", "move_sl", "set_be", "close_trade", "cancel_pending", "unknown", "partial_close", "modify_entry"]: # Added modify_entry
                     logger.warning(f"LLM returned potentially invalid update_type: {update_data_obj.update_type}. Treating as unknown.")
                     update_data_obj.update_type = 'unknown'

                logger.info(f"Message analyzed, classified as update by LLM.")
                logger.debug(f"Constructed UpdateData: {update_data_obj}")
                return {'type': 'update', 'data': update_data_obj}

            except Exception as e:
                 logger.error(f"Error processing 'update' data from LLM: {e}. LLM Result: {llm_result}", exc_info=True)
                 return {'type': 'ignore', 'data': None} # Ignore if structuring fails

        else: # message_type == 'ignore' or unknown
            logger.info("Message analyzed, classified as ignore by LLM.")
            return {'type': 'ignore', 'data': None}

    # --- Deprecated analyze_update method ---
    # Kept for reference or potential future use, but currently bypassed by main analyze logic
    # We might remove it later, but update signature for now.
    def analyze_update(self, message_text, image_data=None, context=None):
        """
        DEPRECATED: Analyzes a message specifically for update parameters (SL/TP).
        Use the main analyze method with appropriate context/mode instead.
        """
        logger.warning("analyze_update method is deprecated. Using main analyze method.")
        # Call analyze with the appropriate prompt type (or let analyze decide)
        result = self.analyze(message_text, image_data, context)
        if result and result['type'] == 'update':
            return result['data'] # Return only the UpdateData object
        else:
            # Return a default 'unknown' update if main analysis didn't yield an update
            # Or consider returning None or raising an error
             logger.warning(f"Main analysis did not classify message as 'update'. Returning default unknown update. Analysis result: {result}")
             return UpdateData(update_type='unknown')