import MetaTrader5 as mt5
import logging
from datetime import datetime, timezone

logger = logging.getLogger('TradeBot')

class MT5DataFetcher:
    """Fetches market data and account information from the MT5 terminal."""

    def __init__(self, mt5_connector):
        """
        Initializes the data fetcher.

        Args:
            mt5_connector (MT5Connector): An instance of the MT5Connector class.
        """
        self.connector = mt5_connector

    def get_symbol_tick(self, symbol):
        """
        Fetches the latest tick data (bid/ask prices) for a given symbol.

        Args:
            symbol (str): The symbol to fetch tick data for (e.g., 'XAUUSD').

        Returns:
            mt5.Tick or None: The latest tick object if successful, None otherwise.
                              Tick object has attributes like time, bid, ask, last, volume.
        """
        if not self.connector.ensure_connection():
            logger.error(f"Cannot fetch tick for {symbol}, MT5 connection failed.")
            return None

        try:
            tick = mt5.symbol_info_tick(symbol)
            if tick:
                # Convert timestamp to datetime for logging clarity
                dt_time = datetime.fromtimestamp(tick.time, tz=timezone.utc)
                logger.debug(f"Tick for {symbol}: Time={dt_time}, Bid={tick.bid}, Ask={tick.ask}, Last={tick.last}")
                return tick
            else:
                logger.error(f"Failed to get tick for {symbol}: {mt5.last_error()}")
                return None
        except Exception as e:
            logger.error(f"Exception fetching tick for {symbol}: {e}", exc_info=True)
            return None

    def get_account_info(self):
        """
        Fetches current account information (balance, equity, margin, etc.).

        Returns:
            mt5.AccountInfo or None: Account information object if successful, None otherwise.
                                      Has attributes like login, balance, equity, margin, margin_free, etc.
        """
        if not self.connector.ensure_connection():
            logger.error("Cannot fetch account info, MT5 connection failed.")
            return None
    
        def get_symbol_tick_value(self, symbol):
            """
            Calculates the value of one tick (point) for one lot of the symbol in the account currency.
    
            Args:
                symbol (str): The symbol name.
    
            Returns:
                float or None: The value of one tick per lot, or None if calculation fails.
            """
            if not self.connector.ensure_connection():
                logger.error(f"Cannot get tick value for {symbol}, MT5 connection failed.")
                return None
    
            try:
                # Use calculate function for precision
                # Example: Calculate profit for 1 lot moving 1 point (tick_size)
                symbol_info = self.get_symbol_info(symbol)
                if not symbol_info:
                    return None
    
                tick_size = symbol_info.tick_size
                volume = 1.0 # Calculate for 1 lot
    
                # Determine price for calculation (use current ask)
                tick = self.get_symbol_tick(symbol)
                if not tick:
                    logger.warning(f"Could not get current tick for {symbol} to calculate tick value accurately. Using symbol_info.ask.")
                    price = symbol_info.ask
                else:
                    price = tick.ask
    
                # Calculate profit for a buy position moving 1 tick up
                profit = mt5.order_calc_profit(mt5.ORDER_TYPE_BUY, symbol, volume, price, price + tick_size)
    
                if profit is None:
                     logger.error(f"order_calc_profit failed for {symbol}. Error: {mt5.last_error()}")
                     return None
    
                logger.debug(f"Calculated tick value for {symbol} (1 lot, 1 tick): {profit}")
                return profit # This is the value of one tick (point) for one lot
    
            except Exception as e:
                logger.error(f"Exception calculating tick value for {symbol}: {e}", exc_info=True)
                return None

        try:
            account_info = mt5.account_info()
            if account_info:
                logger.debug(f"Account Info: Login={account_info.login}, Balance={account_info.balance}, Equity={account_info.equity}, Margin={account_info.margin}, FreeMargin={account_info.margin_free}")
                return account_info
            else:
                logger.error(f"Failed to get account info: {mt5.last_error()}")
                return None
        except Exception as e:
            logger.error(f"Exception fetching account info: {e}", exc_info=True)
            return None

    def get_symbol_info(self, symbol):
        """
        Fetches detailed information about a specific symbol.

        Args:
            symbol (str): The symbol to fetch information for.

        Returns:
            mt5.SymbolInfo or None: Symbol information object if successful, None otherwise.
                                     Has attributes like spread, digits, contract_size, trade_mode, etc.
        """
        if not self.connector.ensure_connection():
            logger.error(f"Cannot fetch symbol info for {symbol}, MT5 connection failed.")
            return None

        try:
            symbol_info = mt5.symbol_info(symbol)
            if symbol_info:
                logger.debug(f"Symbol Info for {symbol}: Spread={symbol_info.spread}, Digits={symbol_info.digits}, TradeMode={symbol_info.trade_mode}")
                # Check if symbol is tradable
                if symbol_info.trade_mode == mt5.SYMBOL_TRADE_MODE_DISABLED:
                     logger.warning(f"Symbol {symbol} is disabled for trading.")
                elif symbol_info.trade_mode == mt5.SYMBOL_TRADE_MODE_CLOSEONLY:
                     logger.warning(f"Symbol {symbol} is in close-only mode.")
                elif symbol_info.trade_mode == mt5.SYMBOL_TRADE_MODE_LONGONLY:
                     logger.warning(f"Symbol {symbol} is in long-only mode.")
                elif symbol_info.trade_mode == mt5.SYMBOL_TRADE_MODE_SHORTONLY:
                     logger.warning(f"Symbol {symbol} is in short-only mode.")

                return symbol_info
            else:
                logger.error(f"Failed to get symbol info for {symbol}: {mt5.last_error()}")
                return None
        except Exception as e:
            logger.error(f"Exception fetching symbol info for {symbol}: {e}", exc_info=True)
            return None

# Example usage (optional, for testing)
if __name__ == '__main__':
    import configparser
    import os
    import sys
    from logger_setup import setup_logging
    from mt5_connector import MT5Connector

    # Setup basic logging for test
    test_log_path = os.path.join(os.path.dirname(__file__), '..', 'logs', 'mt5_fetcher_test.log')
    setup_logging(log_file_path=test_log_path, log_level_str='DEBUG')

    # Load dummy config (replace with actual config for real test)
    example_config_path = os.path.join(os.path.dirname(__file__), '..', 'config', 'config.example.ini')
    if not os.path.exists(example_config_path):
        print(f"ERROR: config.example.ini not found at {example_config_path}. Cannot run test.")
        sys.exit(1)

    config = configparser.ConfigParser()
    config.read(example_config_path)
    # --- IMPORTANT: Fill in REAL MT5 details in config.example.ini for this test to work ---

    if not config.get('MT5', 'account') or 'YOUR_' in config.get('MT5', 'account'):
         print("WARNING: Dummy MT5 credentials found in config. Connection test will likely fail.")
         # sys.exit(1) # Optionally exit if real creds are mandatory for test

    connector = MT5Connector(config)
    fetcher = MT5DataFetcher(connector)

    print("Attempting connection for fetcher test...")
    if connector.connect():
        print("Connection successful!")
        symbol_to_test = config.get('MT5', 'symbol', fallback='XAUUSD')

        print(f"\nFetching tick for {symbol_to_test}...")
        tick = fetcher.get_symbol_tick(symbol_to_test)
        if tick:
            print(f"  Success: Ask={tick.ask}, Bid={tick.bid}")
        else:
            print("  Failed to get tick.")

        print("\nFetching account info...")
        acc_info = fetcher.get_account_info()
        if acc_info:
            print(f"  Success: Balance={acc_info.balance}, Equity={acc_info.equity}")
        else:
            print("  Failed to get account info.")

        print(f"\nFetching symbol info for {symbol_to_test}...")
        sym_info = fetcher.get_symbol_info(symbol_to_test)
        if sym_info:
            print(f"  Success: Spread={sym_info.spread}, Digits={sym_info.digits}, Contract Size={sym_info.contract_size}")
        else:
            print("  Failed to get symbol info.")

        connector.disconnect()
    else:
        print("Connection failed. Cannot run fetcher tests.")

    print("\nTest finished.")