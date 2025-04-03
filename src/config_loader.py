import configparser
import logging
import os
import sys

logger = logging.getLogger('TradeBot')

DEFAULT_CONFIG = {
    'Telegram': {
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
        'max_total_open_lots': '0.1' # Default max total lots
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
        'duplicate_cache_size': '10000' # Max number of message IDs to remember
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
        'auto_sl_risk_usd': '5.0'
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
        'Gemini': ['api_key']
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
        'Trading': [('fixed_lot_size', float), ('default_lot_size', float), ('max_total_open_lots', float)],
        'Retries': [('requote_retry_attempts', int), ('requote_retry_delay_seconds', int)],
        'Misc': [('duplicate_cache_size', int)], # Validate cache size
        'LLMContext': [('enable_price_context', bool), ('enable_trade_context', bool), ('enable_history_context', bool), ('history_message_count', int)], # Validate new context settings
        'AutoSL': [('enable_auto_sl', bool), ('auto_sl_delay_seconds', int), ('auto_sl_risk_usd', float)], # Validate AutoSL settings
        'Strategy': [('partial_close_percentage', int)] # Validate Strategy settings
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