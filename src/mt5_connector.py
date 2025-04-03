import MetaTrader5 as mt5
import sys
import logging
import time
import threading
import configparser # Import missing module

logger = logging.getLogger('TradeBot')

class MT5Connector:
    """Handles connection and initialization with the MetaTrader 5 terminal."""

    def __init__(self, config):
        """
        Initializes the connector with configuration.

        Args:
            config (configparser.ConfigParser): The application configuration object.
        """
        self.config = config
        self.is_initialized = False
        self.connection_lock = threading.Lock() # Ensure thread safety for connection attempts

    def connect(self):
        """
        Establishes and initializes the connection to the MT5 terminal.

        Reads credentials and path from the config. Attempts to initialize MT5.

        Returns:
            bool: True if connection and initialization are successful, False otherwise.
        """
        with self.connection_lock:
            if self.is_initialized:
                logger.debug("MT5 connection already initialized.")
                return True

            try:
                account = int(self.config.get('MT5', 'account'))
                password = self.config.get('MT5', 'password')
                server = self.config.get('MT5', 'server')
                path = self.config.get('MT5', 'path')
            except (ValueError, configparser.NoSectionError, configparser.NoOptionError) as e:
                logger.critical(f"MT5 configuration error: {e}", exc_info=True)
                return False

            logger.info(f"Attempting to initialize MT5 terminal at path: {path}")
            if not mt5.initialize(path=path, login=account, password=password, server=server, timeout=10000): # 10 second timeout
                logger.error(f"MT5 initialize() failed, error code: {mt5.last_error()}")
                mt5.shutdown() # Ensure cleanup even if init fails
                self.is_initialized = False
                return False

            logger.info("MT5 initialized successfully.")

            # Verify terminal connection status
            terminal_info = mt5.terminal_info()
            if not terminal_info:
                logger.error(f"Failed to get terminal_info(), error code: {mt5.last_error()}")
                mt5.shutdown()
                self.is_initialized = False
                return False

            if not terminal_info.connected:
                logger.error("MT5 terminal is not connected to the trade server.")
                mt5.shutdown()
                self.is_initialized = False
                return False

            if not terminal_info.trade_allowed:
                 logger.error("Algo trading is not enabled in the MT5 terminal. Cannot proceed with trading.")
                 # Make this fatal as trading is the core function
                 mt5.shutdown()
                 self.is_initialized = False
                 return False

            logger.info(f"MT5 Terminal connected: {terminal_info.connected}, Algo Trading Allowed: {terminal_info.trade_allowed}")
            logger.info(f"Connected to account {account} on server {server}")
            self.is_initialized = True
            return True

    def disconnect(self):
        """Shuts down the connection to the MT5 terminal."""
        with self.connection_lock:
            if self.is_initialized:
                logger.info("Shutting down MT5 connection.")
                mt5.shutdown()
                self.is_initialized = False
            else:
                logger.debug("MT5 connection already shut down.")

    def ensure_connection(self):
        """Checks connection and attempts to reconnect if necessary."""
        if not self.is_initialized or not self.is_connected():
            logger.warning("MT5 connection lost or not initialized. Attempting to reconnect...")
            self.disconnect() # Ensure clean state before reconnecting
            time.sleep(2) # Brief pause before retry
            return self.connect()
        return True

    def is_connected(self):
        """Checks if the MT5 terminal is currently connected."""
        if not self.is_initialized:
            return False
        try:
            # Use a lightweight check like terminal_info
            info = mt5.terminal_info()
            if info and info.connected:
                return True
            else:
                logger.warning(f"MT5 terminal_info check failed or shows disconnected. Error: {mt5.last_error()}")
                return False
        except Exception as e:
            logger.error(f"Exception during MT5 connection check: {e}", exc_info=True)
            return False

# Example usage (optional, for testing)
if __name__ == '__main__':
    import configparser
    import os
    from logger_setup import setup_logging

    # Setup basic logging for test
    test_log_path = os.path.join(os.path.dirname(__file__), '..', 'logs', 'mt5_connector_test.log')
    setup_logging(log_file_path=test_log_path, log_level_str='DEBUG')

    # Load dummy config (replace with actual config for real test)
    example_config_path = os.path.join(os.path.dirname(__file__), '..', 'config', 'config.example.ini')
    if not os.path.exists(example_config_path):
        print(f"ERROR: config.example.ini not found at {example_config_path}. Cannot run test.")
        sys.exit(1)

    config = configparser.ConfigParser()
    config.read(example_config_path)
    # --- IMPORTANT: Fill in REAL MT5 details in config.example.ini for this test to work ---
    # config['MT5']['account'] = 'YOUR_REAL_ACCOUNT'
    # config['MT5']['password'] = 'YOUR_REAL_PASSWORD'
    # config['MT5']['server'] = 'YOUR_REAL_SERVER'
    # config['MT5']['path'] = r'C:\...' # Your actual path

    if not config.get('MT5', 'account') or 'YOUR_' in config.get('MT5', 'account'):
         print("WARNING: Dummy MT5 credentials found in config. Connection test will likely fail.")
         print("Please edit config/config.example.ini with real credentials to test connection.")
         # sys.exit(1) # Optionally exit if real creds are mandatory for test

    connector = MT5Connector(config)

    print("Attempting initial connection...")
    if connector.connect():
        print("Connection successful!")
        print(f"Is connected check: {connector.is_connected()}")

        print("\nAttempting to get account info...")
        account_info = mt5.account_info()
        if account_info:
            print(f"Account Info: Login={account_info.login}, Balance={account_info.balance}, Equity={account_info.equity}")
        else:
            print(f"Failed to get account info: {mt5.last_error()}")

        print("\nDisconnecting...")
        connector.disconnect()
        print(f"Is connected check after disconnect: {connector.is_connected()}")

        print("\nAttempting ensure_connection (should reconnect)...")
        if connector.ensure_connection():
            print("Reconnection successful via ensure_connection!")
            print(f"Is connected check: {connector.is_connected()}")
            connector.disconnect() # Clean up
        else:
            print("Reconnection failed via ensure_connection.")

    else:
        print("Initial connection failed.")

    print("\nTest finished.")