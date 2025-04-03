import google.generativeai as genai
import logging
import os
import time
import json # Import the missing json module
from PIL import Image # For potential image handling
import io

logger = logging.getLogger('TradeBot')

# Constants for retry logic
MAX_RETRIES = 3
RETRY_DELAY = 5 # seconds

class LLMInterface:
    """Handles communication with the Google Gemini API."""

    def __init__(self, config):
        """
        Initializes the LLM interface and configures the Gemini client.

        Args:
            config (configparser.ConfigParser): The application configuration object.
        """
        self.api_key = config.get('Gemini', 'api_key', fallback=None)
        self.model_name = config.get('Gemini', 'model_name', fallback='gemini-pro') # Default to text-only model

        if not self.api_key:
            logger.critical("Gemini API key not found in configuration. LLM features will be disabled.")
            self.client = None
            self.model = None
            # Potentially raise an error here if LLM is absolutely critical
            # raise ValueError("Missing Gemini API Key")
        else:
            try:
                genai.configure(api_key=self.api_key)
                # Check if the specified model supports vision for potential image handling
                # This is a basic check; more robust checking might involve listing models
                if 'vision' in self.model_name or 'gemini-1.5' in self.model_name: # Models like gemini-pro-vision or 1.5 support images
                    self.supports_vision = True
                    logger.info(f"Configuring Gemini with vision-capable model: {self.model_name}")
                else:
                    self.supports_vision = False
                    logger.info(f"Configuring Gemini with text-only model: {self.model_name}")

                # TODO: Add generation_config options if needed (temperature, top_p, etc.)
                self.model = genai.GenerativeModel(self.model_name)
                logger.info("Gemini client configured successfully.")
            except Exception as e:
                logger.critical(f"Failed to configure Gemini client: {e}", exc_info=True)
                self.client = None
                self.model = None
                # raise # Optional: re-raise the exception to halt startup

    def _prepare_prompt(self, message_text, context=None, prompt_type="analyze_signal"):
        """
        Prepares the prompt string to send to the LLM based on the type of analysis needed.
        This is where prompt engineering happens.
        """
        import json # Ensure json is imported

        # --- Prepare Context Strings (if available and enabled) ---
        context_str = "\n--- Additional Context ---\n"
        context_added = False

        if context:
            if context.get('current_price'):
                price_info = context['current_price']
                context_str += f"Current Price (XAUUSD): Bid={price_info['bid']}, Ask={price_info['ask']} (as of {price_info['time']})\n"
                context_added = True
            if context.get('active_trades'):
                context_str += "Active Bot Trades:\n"
                for trade_summary in context['active_trades']:
                    context_str += f"- {trade_summary}\n"
                context_added = True
            if context.get('message_history'):
                context_str += "Recent Message History (Oldest first):\n"
                # Iterate through history (which is now a list)
                for msg in context['message_history']:
                    sender = msg.get('sender_id', 'Unknown')
                    text = msg.get('text', '').replace('\n', ' ')[:80] # Truncate/clean
                    ts = msg.get('timestamp', '')
                    edit_flag = " (Edit)" if msg.get('is_edit') else ""
                    # Exclude the current message if it's in the history
                    if msg.get('text') != message_text:
                         context_str += f"- [{ts}] Sender {sender}{edit_flag}: {text}...\n"
                context_added = True

        if not context_added:
            context_str = "" # No context to add

        # --- Base Prompt ---
        base_instructions = f"""
You are an expert trading assistant analyzing messages from a Telegram channel about XAUUSD (Gold) trading.
Your goal is to identify actionable trading signals or updates related to existing trades, using the provided context. Ignore general chat, promotions, performance reports, and educational content unless it directly informs a specific, current trade signal or update.
here is the context (older messages, in square brackets):

[{context_str}]

--- Main Message to Analyze ---

Timestamp: Now (relative to context timestamps if provided)
Message Text (between square brackets):
[{message_text}]
---
"""

        if prompt_type == "analyze_signal":
            # Instructions for classifying message type and extracting details conditionally
            prompt = base_instructions + """
        Analyze the **Main Message to Analyze** above, using the **Additional Context** if provided, and determine the following:

        1.  **Message Type:** Classify the main message's primary purpose. Answer one of: `"new_signal"`, `"update"`, `"ignore"`.
            *   `"new_signal"`: Contains a direct, specific new trading signal (buy/sell XAUUSD).
            *   `"update"`: Provides an update (new SL/TP, move SL, set BE, close, partial close, cancel pending) that clearly relates to one of the numbered 'Active Bot Trades' listed in the context, or a very recent signal from history. Use the context to determine if it's an update.
            *   `"ignore"`: All other messages (general chat, analysis without a signal, questions, performance reports, status updates like 'TP hit' unless they clearly imply a close action for an active trade, etc.).

        2.  **Symbol:** Identify the trading symbol (e.g., "XAUUSD", "GOLD"). If none mentioned, assume "XAUUSD" if context implies it, otherwise `null`.

        3.  **Signal Details (ONLY if Message Type is "new_signal"):**
            *   `action`: "BUY" or "SELL".
            *   `entry_type`: "Pending" if a specific numeric price or price zone (e.g., "3106-3108") is mentioned for entry. Otherwise, "Market".
            *   `entry_price`: The specific numeric price, the "LOW-HIGH" string, or "Market" if no specific entry price is mentioned (implying immediate execution).
            *   `stop_loss`: Number or "N/A".
            *   `take_profits`: JSON list of numbers (e.g., `[3112, 3125]`) or `["N/A"]`. Extract all mentioned TP levels.
            *   `sentiment_score`: Number (-1.0 to +1.0).

        4.  **Update Details (ONLY if Message Type is "update"):**
            *   `target_trade_index`: The number (from the 'Active Bot Trades' list in the context) of the trade this update applies to. If the update doesn't clearly match a numbered trade, provide `null`.
            *   `update_type`: Classify the type of update based on the main message text and context. Answer one of: `"modify_sltp"`, `"move_sl"`, `"set_be"`, `"close_trade"`, `"partial_close"`, `"cancel_pending"`, `"unknown"`.
                - `"modify_sltp"`: Explicitly sets new SL and/or TP values. Extract *all* new TP levels mentioned.
                - `"move_sl"`: Explicitly moves SL to a *new specific value*.
                - `"set_be"`: Moves SL to Break Even (entry price). Check 'Active Bot Trades' context for entry price if needed.
                - `"close_trade"`: Instructs to close the entire trade/position now. Check 'Active Bot Trades' context.
                - `"partial_close"`: Instructs to close a portion of the trade. Extract volume or percentage. Check 'Active Bot Trades' context.
                - `"cancel_pending"`: Instructs to cancel a pending order. Check 'Active Bot Trades' context (pending orders might not be listed, but check history).
                - `"unknown"`: An update is mentioned but the type is unclear, or it doesn't clearly relate to an active trade.
            *   `new_stop_loss`: If `update_type` is `"move_sl"` or `"modify_sltp"`, provide the new SL number. Otherwise "N/A".
            *   `new_take_profits`: If `update_type` is `"modify_sltp"`, provide a JSON list of the new TP numbers (e.g., `[3115, 3120]`). Otherwise `["N/A"]`.
            *   `close_volume`: If `update_type` is `"partial_close"` and a specific lot size is mentioned (e.g., "close 0.02"), provide the number. Otherwise "N/A".
            *   `close_percentage`: If `update_type` is `"partial_close"` and a percentage is mentioned (e.g., "close 50%"), provide the number (e.g., 50). Otherwise "N/A".

        Provide the output ONLY in valid JSON format.

        **JSON Structure:**

        *   **Required fields for ALL types:** `message_type` (string), `symbol` (string or null).
        *   **Additional fields ONLY if `message_type` is `"new_signal"`:** `is_signal` (must be `true`), `action`, `entry_type`, `entry_price`, `stop_loss`, `take_profits` (list of numbers or ["N/A"]), `sentiment_score`.
        *   **Additional fields ONLY if `message_type` is `"update"`:** `target_trade_index` (integer or null), `update_type` (string), `new_stop_loss` (number or "N/A"), `new_take_profits` (list of numbers or ["N/A"]), `close_volume` (number or "N/A"), `close_percentage` (number or "N/A").

        **Example Output (new_signal):**
        ```json
        {
          "message_type": "new_signal", "symbol": "XAUUSD", "is_signal": true, "action": "BUY", "entry_type": "Pending",
          "entry_price": "3106-3108", "stop_loss": 3103, "take_profits": [3112, 3125], "sentiment_score": 0.8
        }
        ```
        **Example Output (update - move SL):**
        ```json
        {
          "message_type": "update", "symbol": "XAUUSD", "target_trade_index": 1, "update_type": "move_sl", "new_stop_loss": 3110.5, "new_take_profits": ["N/A"]
        }
        ```
        **Example Output (update - set BE):**
        ```json
        {
          "message_type": "update", "symbol": "XAUUSD", "target_trade_index": 2, "update_type": "set_be", "new_stop_loss": "N/A", "new_take_profits": ["N/A"], "close_volume": "N/A", "close_percentage": "N/A"
        }
        ```
        **Example Output (update - partial close %):**
        ```json
        {
          "message_type": "update", "symbol": "XAUUSD", "target_trade_index": 1, "update_type": "partial_close", "new_stop_loss": "N/A", "new_take_profits": ["N/A"], "close_volume": "N/A", "close_percentage": 50
        }
        ```
        **Example Output (update - partial close volume):**
        ```json
        {
          "message_type": "update", "symbol": "XAUUSD", "target_trade_index": 3, "update_type": "partial_close", "new_stop_loss": "N/A", "new_take_profits": ["N/A"], "close_volume": 0.01, "close_percentage": "N/A"
        }
        ```
        **Example Output (update - close):**
        ```json
        {
          "message_type": "update", "symbol": "XAUUSD", "target_trade_index": 1, "update_type": "close_trade", "new_stop_loss": "N/A", "new_take_profits": ["N/A"], "close_volume": "N/A", "close_percentage": "N/A"
        }
        ```
        **Example Output (ignore):**
        ```json
        {
          "message_type": "ignore", "symbol": null
        }
        ```
        """
        # Keep analyze_edit_or_reply prompt separate for now, might merge later if needed
        elif prompt_type == "analyze_edit_or_reply":
             # Instructions for analyzing edits/replies for missing info
             # This prompt type might become less necessary with the main prompt's context,
             # but we keep it for now. Add context instructions here too if needed.
             prompt = base_instructions + """
This message is potentially an edit of, or a reply to, a previous message.
Analyze the **Main Message to Analyze** above, using the **Additional Context** if provided.
Does the main message provide a missing Stop Loss (SL) or Take Profit (TP) for a previously mentioned XAUUSD signal (check history/active trades)?

1.  **Provides SL:** Does this message specify a Stop Loss price? Answer "true" or "false".
2.  **SL Price:** If Provides SL is true, what is the Stop Loss price? Provide only the number. If false, answer "N/A".
3.  **Provides TP:** Does this message specify a Take Profit price? Answer "true" or "false".
4.  **TP Prices:** If Provides TP is true, what are the Take Profit price(s)? Provide a JSON list of numbers (e.g., `[3115, 3120]`). If false, answer `["N/A"]`.

Provide the output ONLY in the following JSON format, with no other text before or after:
{
  "provides_sl": boolean,
  "sl_price": number | "N/A",
  "provides_tp": boolean,
  "tp_prices": list[number] | list["N/A"]
}
"""
        else:
            logger.warning(f"Unknown prompt type requested: {prompt_type}")
            # Fallback to a generic analysis prompt or return error
            prompt = base_instructions + "\nAnalyze the message for trading relevance."

        return prompt.strip()


    def analyze_message(self, message_text, image_data=None, context=None, prompt_type="analyze_signal"):
        """
        Sends message content (and optionally image) to the Gemini API for analysis.

        Args:
            message_text (str): The text content of the Telegram message.
            image_data (bytes, optional): The byte content of an image, if present. Defaults to None.
            context (dict, optional): Additional context (price, history, trades). Defaults to None.
            prompt_type (str): The type of analysis requested ("analyze_signal", "analyze_edit_or_reply").

        Returns:
            dict or None: A dictionary containing the structured analysis results from the LLM,
                          or None if the analysis fails or the client is not configured.
        """
        if not self.model:
            logger.error("Gemini client not initialized. Cannot analyze message.")
            return None

        prompt = self._prepare_prompt(message_text, context, prompt_type) # Pass context
        content_parts = [prompt]

        # Handle image data if the model supports vision and image data is provided
        if self.supports_vision and image_data:
            try:
                img = Image.open(io.BytesIO(image_data))
                # Prepend image part - order might matter depending on model/prompt
                content_parts.insert(0, img)
                logger.debug("Image data added to Gemini request.")
            except Exception as e:
                logger.error(f"Failed to process image data for Gemini: {e}", exc_info=True)
                # Decide whether to proceed without image or fail
                # Proceeding without image for now
                logger.warning("Proceeding with Gemini analysis using text only.")

        retries = 0
        while retries < MAX_RETRIES:
            try:
                logger.debug(f"Sending request to Gemini (Attempt {retries + 1}/{MAX_RETRIES})...")
                # Log the prepared prompt at DEBUG level
                log_prompt = prompt if len(prompt) < 500 else prompt[:500] + "..." # Avoid overly long logs
                logger.debug(f"Gemini Prompt ({prompt_type}):\n{log_prompt}")
                # Use generate_content for potentially mixed text/image input
                response = self.model.generate_content(content_parts)

                # Log raw response text for debugging
                logger.debug(f"Gemini raw response text: {response.text}")

                # Attempt to parse the response text as JSON
                try:
                    # Gemini might wrap JSON in ```json ... ```, try to extract it
                    json_response_str = response.text.strip()
                    if json_response_str.startswith("```json"):
                        json_response_str = json_response_str[7:]
                    if json_response_str.endswith("```"):
                        json_response_str = json_response_str[:-3]
                    json_response_str = json_response_str.strip()

                    result_dict = json.loads(json_response_str)
                    logger.info(f"Gemini analysis successful. Type: {prompt_type}")
                    # Log the successfully parsed dict at DEBUG level
                    logger.debug(f"Gemini Parsed JSON Result: {result_dict}")
                    return result_dict
                except json.JSONDecodeError as json_err:
                    logger.error(f"Failed to decode Gemini response as JSON: {json_err}")
                    logger.error(f"LLM Raw Response was: {response.text}")
                    # Don't retry on JSON decode error, it's likely a prompt/model issue
                    return None
                except Exception as parse_err:
                     logger.error(f"Unexpected error parsing Gemini JSON response: {parse_err}", exc_info=True)
                     return None


            except Exception as e:
                logger.error(f"Error calling Gemini API (Attempt {retries + 1}/{MAX_RETRIES}): {e}", exc_info=True)
                retries += 1
                if retries < MAX_RETRIES:
                    logger.info(f"Retrying Gemini API call in {RETRY_DELAY} seconds...")
                    time.sleep(RETRY_DELAY)
                else:
                    logger.critical("Max retries reached for Gemini API call. Analysis failed.")
                    return None
        return None # Should not be reached if loop logic is correct

# Example usage (optional, for testing)
if __name__ == '__main__':
    import configparser
    import os
    import sys
    import json # Need json for parsing in the main function now
    from logger_setup import setup_logging

    # Setup basic logging for test
    test_log_path = os.path.join(os.path.dirname(__file__), '..', 'logs', 'llm_interface_test.log')
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
         print("WARNING: Dummy Gemini API key found in config. LLM test will fail.")
         print("Please edit config/config.example.ini with a real API key.")
         # sys.exit(1) # Optionally exit

    llm_interface = LLMInterface(config)

    if llm_interface.model:
        print("Testing with a sample signal message...")
        # Example from docs/Message Examples.md
        test_message_signal = """
🧑‍💻XAUUSD Buy gold  Zone 3106 - 3108
🔹SL 3103
🔹TP 3112- 3125- open
"""
        result_signal = llm_interface.analyze_message(test_message_signal, prompt_type="analyze_signal")
        print("--- Signal Analysis Result ---")
        if result_signal:
            print(json.dumps(result_signal, indent=2))
        else:
            print("Analysis failed.")

        print("\nTesting with a sample non-signal message...")
        test_message_non_signal = """
Gold Buy running 400 pips from yesterday’s absolute precision analysis:
 • Identified the strong liquidity zone at 3100
 • Pinpointed the exact entry at 3100
"""
        result_non_signal = llm_interface.analyze_message(test_message_non_signal, prompt_type="analyze_signal")
        print("--- Non-Signal Analysis Result ---")
        if result_non_signal:
            print(json.dumps(result_non_signal, indent=2))
        else:
            print("Analysis failed.")

        print("\nTesting with a sample edit message...")
        test_message_edit = "TP updated to 3115"
        result_edit = llm_interface.analyze_message(test_message_edit, prompt_type="analyze_edit_or_reply")
        print("--- Edit Analysis Result ---")
        if result_edit:
            print(json.dumps(result_edit, indent=2))
        else:
            print("Analysis failed.")

    else:
        print("LLM Interface could not be initialized. Check API key and configuration.")

    print("\nTest finished.")