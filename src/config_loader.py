import configparser
import logging
import os
import sys

logger = logging.getLogger('TradeBot')

DEFAULT_CONFIG = {
    'Telegram': {
        'api_id': '', # Added for env var override
        'api_hash': '', # Added for env var override
        'bot_token': '', # Required for bot authentication
        'channel_id': '', # Always required (main channel)
        'debug_channel_id': '' # Optional: Channel ID for sending debug messages
    },
    'MT5': {
        'account': '',
        'password': '',
        'server': '',
        'path': r'C:\Program Files\MetaTrader 5\terminal64.exe',
        'symbol': 'XAUUSD'
    },
    'Gemini': {
        'api_key': '',
        'model_name': 'gemini-pro' # Default model
    },
    'Trading': {
        'lot_size_method': 'fixed',
        'fixed_lot_size': '0.01',
        'default_lot_size': '0.01',
        'max_total_open_lots': '0.1', # Default max total lots
        'enable_market_order_cooldown': 'true', # Enable cooldown after market execution
        'market_order_cooldown_seconds': '60', # Cooldown duration in seconds
        'base_lot_size_for_usd_targets': '0.01' # Lot size assumed for USD targets (AutoBE, TSL, etc.)
    },
    'DecisionLogic': {
        'sentiment_weight': '0.5',
        'price_action_weight': '0.5',
        'approval_threshold': '0.6',
        'use_sentiment_analysis': 'true' # Add new setting, default to true
        # 'max_entry_deviation_pips': '20' # Example if added later
    },
    'Logging': {
        'log_file': 'logs/bot.log',
        'log_level': 'INFO'
    },
    'Retries': {
        'requote_retry_attempts': '5',
        'requote_retry_delay_seconds': '4'
    },
    'Misc': {
        'duplicate_cache_size': '10000', # Max number of message IDs to remember
        'periodic_check_interval_seconds': '60' # Interval for checking trades (AutoSL, AutoBE, etc.)
    },
    'LLMContext': {
        'enable_price_context': 'true', # Send current price to LLM
        'enable_trade_context': 'true', # Send active trade list to LLM
        'enable_history_context': 'true', # Send recent message history to LLM
        'history_message_count': '5' # Number of recent messages to include in history context
    },
    'Strategy': {
        'entry_range_strategy': 'midpoint', # Options: midpoint, closest, farthest
        'tp_execution_strategy': 'first_tp_full_close', # Options: first_tp_full_close, last_tp_full_close, sequential_partial_close
        'partial_close_percentage': '50' # Integer percentage (1-99) used for sequential_partial_close
    },
    'AutoSL': {
        'enable_auto_sl': 'false',
        'auto_sl_delay_seconds': '60',
        'auto_sl_price_distance': '5.0' # SL distance in price units (e.g., 5.0 for a $5 price move on XAUUSD)
    },
    'AutoBE': { # Added missing AutoBE section
        'enable_auto_be': 'false',
        'auto_be_profit_usd': '3.0'
    },
    'AutoTP': { # Added AutoTP section
        'enable_auto_tp': 'false',
        'auto_tp_price_distance': '10.0' # TP distance in price units (e.g., 10.0 for a $10 price move on XAUUSD)
    },
    'TrailingStop': { # Added TrailingStop section
        'enable_trailing_stop': 'false',
        'activation_profit_usd': '10.0', # Profit needed to activate TSL (relative to base lot size)
        'trail_distance_price': '5.0' # How far SL trails behind price (in price units)
    },
    'LLMPrompts': {
        'base_instructions': """
You are an expert trading assistant analyzing messages from a Telegram channel about XAUUSD (Gold) trading. The channel is run by a trading coach.
Your goal is to identify actionable trading signals or updates related to existing trades, using the provided context. Ignore general chat, promotions, performance reports, and educational content unless it directly informs a specific, current trade signal or update.
Know that the coach may send messages regarding his trade signals, to boast about success, or share winnings and running pips, and they will contain words like (gold, buy, sell, pips) and so on, but still, they may not be trading signals.
Also, the coach may ask if you are ready to (buy or sell) with them, this is merely a support message, not a trading signal.
You need to be quite sure that the intention of the message is a command to execute a trade, or to modify a previously sent trade signal.
here is the additional context, so you understand the conversation better (older messages, in square brackets):

[{context_str}]

--- Main Message to Analyze ---

Timestamp: Now (relative to context timestamps if provided)
Message Text (between square brackets):

[{message_text}]

---
""",
        'analyze_signal_instructions': """
Analyze the **Main Message to Analyze** above, using the **Additional Context**, and determine the following:

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
    *   `close_percentage`: If `update_type` is `"partial_close"` and a percentage is mentioned (e.g., "close 50%%"), provide the number (e.g., 50). Otherwise "N/A".

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
**Example Output (update - partial close %%):**
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
""",
        'analyze_edit_or_reply_instructions': """
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
    }
}

def load_config(config_path='config/config.ini'):
    """
    Loads configuration from the specified INI file.

    Args:
        config_path (str): Path to the configuration file.

    Returns:
        configparser.ConfigParser: The loaded configuration object.
                                    Returns None if the file doesn't exist
                                    or cannot be parsed.
    """
    if not os.path.exists(config_path):
        logger.critical(f"Configuration file not found: {config_path}")
        print(f"CRITICAL: Configuration file '{config_path}' not found.", file=sys.stderr)
        print("Please copy 'config/config.example.ini' to 'config/config.ini' and fill in your details.", file=sys.stderr)
        return None

    config = configparser.ConfigParser()
    # Read the default values first
    config.read_dict(DEFAULT_CONFIG)

    try:
        # Read the user's config file, overriding defaults
        read_files = config.read(config_path, encoding='utf-8')
        if not read_files:
             logger.error(f"Config file found but could not be read: {config_path}")
             return None # Or raise an error? For now return None

        logger.info(f"Configuration loaded successfully from: {config_path}")

        # --- Apply Environment Variable Overrides ---
        logger.info("Checking for environment variable overrides...")
        overrides_applied = 0
        for section, keys in DEFAULT_CONFIG.items():
            for key in keys:
                # Construct environment variable name (e.g., TELEGRAM_API_ID, MT5_ACCOUNT)
                env_var_name = f"{section.upper()}_{key.upper()}"
                env_value = os.environ.get(env_var_name)
                if env_value is not None:
                    try:
                        # Ensure section exists before setting (should always exist due to defaults)
                        if not config.has_section(section):
                            config.add_section(section)
                        config.set(section, key, env_value)
                        logger.info(f"Overrode config '[{section}] {key}' with environment variable '{env_var_name}'.")
                        overrides_applied += 1
                    except Exception as e:
                         # Log error but continue trying other variables
                         logger.error(f"Error applying override from env var '{env_var_name}' for '[{section}] {key}': {e}")
        if overrides_applied > 0:
             logger.info(f"Applied {overrides_applied} configuration overrides from environment variables.")
        else:
             logger.info("No environment variable overrides found.")
        # --- End Environment Variable Overrides ---

        # Validate the final configuration (including overrides)
        validate_config(config)
        return config

    except configparser.Error as e:
        logger.critical(f"Error parsing configuration file {config_path}: {e}", exc_info=True)
        print(f"CRITICAL: Error parsing configuration file '{config_path}': {e}", file=sys.stderr)
        return None
    except Exception as e:
        logger.critical(f"An unexpected error occurred loading config {config_path}: {e}", exc_info=True)
        print(f"CRITICAL: An unexpected error occurred loading config '{config_path}': {e}", file=sys.stderr)
        return None


def validate_config(config):
    """Performs basic validation checks on the loaded config."""
    required_always = {
        'Telegram': ['channel_id', 'bot_token', 'api_id', 'api_hash'], # Main channel is required
        'MT5': ['account', 'password', 'server', 'path', 'symbol'],
        'Gemini': ['api_key'],
        'LLMPrompts': ['base_instructions', 'analyze_signal_instructions', 'analyze_edit_or_reply_instructions'],
        'AutoBE': ['enable_auto_be', 'auto_be_profit_usd'],
        'AutoTP': ['enable_auto_tp', 'auto_tp_price_distance'], # Renamed AutoTP validation
        'TrailingStop': ['enable_trailing_stop', 'activation_profit_usd', 'trail_distance_price'], # Renamed TrailingStop validation
        # Removed redundant 'Trading' list here, numeric_checks is more specific
    }
    missing = []

    # Check always required fields first
    for section, keys in required_always.items():
        if section not in config:
            missing.append(f"Section [{section}]")
            continue
        for key in keys:
            if not config.get(section, key):
                missing.append(f"Key '{key}' in section [{section}]")

    # No specific auth check needed here anymore, as required_always covers all fields.
    # The warning about both being present is removed as api_id/hash are always needed for init.

    if missing:
        # Use set to remove potential duplicates before joining
        unique_missing = sorted(list(set(missing)))
        error_msg = f"Configuration validation failed. Missing or empty required values/auth: {', '.join(unique_missing)}"
        logger.critical(error_msg)
        raise ValueError(error_msg) # Raise error to prevent startup with invalid config

    # Validate numeric types where necessary (add more as needed)
    numeric_checks = {
        'Telegram': [('api_id', int)], # Ensure api_id is integer
        'MT5': [('account', int)], # Ensure MT5 account is integer
        'DecisionLogic': [('sentiment_weight', float), ('price_action_weight', float), ('approval_threshold', float), ('use_sentiment_analysis', bool)], # Add bool validation
        'Trading': [('fixed_lot_size', float), ('default_lot_size', float), ('max_total_open_lots', float), ('enable_market_order_cooldown', bool), ('market_order_cooldown_seconds', int), ('base_lot_size_for_usd_targets', float)], # Added base_lot_size validation
        'Retries': [('requote_retry_attempts', int), ('requote_retry_delay_seconds', int)],
        'Misc': [('duplicate_cache_size', int), ('periodic_check_interval_seconds', int)], # Validate cache size and interval
        'LLMContext': [('enable_price_context', bool), ('enable_trade_context', bool), ('enable_history_context', bool), ('history_message_count', int)], # Validate new context settings
        'AutoSL': [('enable_auto_sl', bool), ('auto_sl_delay_seconds', int), ('auto_sl_price_distance', float)], # Renamed AutoSL validation
        'Strategy': [('partial_close_percentage', int)],
        'AutoBE': [('enable_auto_be', bool), ('auto_be_profit_usd', float)],
        'AutoTP': [('enable_auto_tp', bool), ('auto_tp_price_distance', float)], # Renamed AutoTP validation
        'TrailingStop': [('enable_trailing_stop', bool), ('activation_profit_usd', float), ('trail_distance_price', float)] # Renamed TrailingStop validation
    }
    invalid_numerics = []
    for section, checks in numeric_checks.items():
        if section in config:
            for key, type_converter in checks:
                value_str = config.get(section, key, fallback=None)
                if value_str is not None:
                    # Attempt to strip inline comments before conversion
                    value_str_cleaned = value_str.split('#')[0].strip()
                    try:
                        type_converter(value_str_cleaned)
                    except ValueError:
                        invalid_numerics.append(f"Key '{key}' in section [{section}] ('{value_str}' -> '{value_str_cleaned}') is not a valid {type_converter.__name__}")
                # else: # Key might be optional depending on logic, handled by initial check if needed
                #     pass

    if invalid_numerics:
        error_msg = f"Configuration validation failed. Invalid numeric values: {'; '.join(invalid_numerics)}"
        logger.critical(error_msg)
        raise ValueError(error_msg)

    # Validate specific string options
    allowed_entry_strategies = ['midpoint', 'closest', 'farthest']
    allowed_tp_strategies = ['first_tp_full_close', 'last_tp_full_close', 'sequential_partial_close']
    invalid_options = []

    entry_strategy = config.get('Strategy', 'entry_range_strategy', fallback='midpoint').lower()
    if entry_strategy not in allowed_entry_strategies:
        invalid_options.append(f"Key 'entry_range_strategy' in section [Strategy] ('{entry_strategy}') must be one of: {', '.join(allowed_entry_strategies)}")

    tp_strategy = config.get('Strategy', 'tp_execution_strategy', fallback='first_tp_full_close').lower() # Updated key name and default
    if tp_strategy not in allowed_tp_strategies:
        invalid_options.append(f"Key 'tp_execution_strategy' in section [Strategy] ('{tp_strategy}') must be one of: {', '.join(allowed_tp_strategies)}")

    # Validate partial close percentage range
    # Validate partial close percentage range only if the relevant strategy is selected
    if tp_strategy == 'sequential_partial_close':
        try:
            partial_perc_str = config.get('Strategy', 'partial_close_percentage', fallback='50').split('#')[0].strip()
            partial_perc = int(partial_perc_str)
            if not (1 <= partial_perc <= 99):
                 invalid_options.append(f"Key 'partial_close_percentage' in section [Strategy] ('{partial_perc}') must be between 1 and 99 when tp_execution_strategy is 'sequential_partial_close'")
        except ValueError:
             # This case is already handled by numeric_checks, but double-check doesn't hurt
             pass # Error already added to invalid_numerics

    if invalid_options:
        error_msg = f"Configuration validation failed. Invalid option values: {'; '.join(invalid_options)}"
        logger.critical(error_msg)
        raise ValueError(error_msg)


    logger.info("Configuration validation passed.")


# Example usage (optional, for testing)
if __name__ == '__main__':
    # Assumes config.example.ini exists in ../config relative to this script
    example_config_path = os.path.join(os.path.dirname(__file__), '..', 'config', 'config.example.ini')
    # Create dummy config if example doesn't exist for testing
    if not os.path.exists(example_config_path):
         print(f"Creating dummy config at {example_config_path} for testing.")
         dummy_config = configparser.ConfigParser()
         dummy_config.read_dict(DEFAULT_CONFIG)
         # Add minimal required values for validation pass
         dummy_config['Telegram']['api_id'] = '123'
         dummy_config['Telegram']['api_hash'] = 'abc'
         dummy_config['Telegram']['channel_id'] = 'test'
         dummy_config['MT5']['account'] = '12345'
         dummy_config['MT5']['password'] = 'pass'
         dummy_config['MT5']['server'] = 'server'
         dummy_config['Gemini']['api_key'] = 'key'
         os.makedirs(os.path.dirname(example_config_path), exist_ok=True)
         with open(example_config_path, 'w') as configfile:
             dummy_config.write(configfile)


    # Setup basic logging for test
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    logger.info(f"Attempting to load config from: {example_config_path}")
    config = load_config(example_config_path)

    if config:
        print("\nConfig loaded successfully:")
        for section in config.sections():
            print(f"[{section}]")
            for key, value in config.items(section):
                # Mask sensitive info for printing
                if 'key' in key.lower() or 'hash' in key.lower() or 'password' in key.lower():
                    value = '********'
                print(f"  {key} = {value}")

        # Example access
        print(f"\nAccessing specific value: MT5 Account = {config.get('MT5', 'account', fallback='Not Found')}")
        print(f"Accessing specific value: Gemini API Key = ********") # Masked
        print(f"Accessing specific value: Log Level = {config.get('Logging', 'log_level', fallback='Not Found')}")
        print(f"Accessing specific value: Fixed Lot Size = {config.getfloat('Trading', 'fixed_lot_size', fallback=0.0)}") # Example getfloat
    else:
        print("\nFailed to load configuration.")