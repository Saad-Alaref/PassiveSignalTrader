import MetaTrader5 as mt5
import sys
import logging
import time
import threading
# import configparser # No longer needed directly
from .config_service import config_service # Import the service

logger = logging.getLogger('TradeBot')

class MT5Connector:
    """Handles connection and initialization with the MetaTrader 5 terminal."""

    def __init__(self, config_service_instance): # Inject service
        """
        Initializes the connector with the configuration service.

        Args:
            config_service_instance (ConfigService): The application config service.
        """
        self.config_service = config_service_instance # Store service instance
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
                # Use config_service to get MT5 details
                account = self.config_service.getint('MT5', 'account')
                password = self.config_service.get('MT5', 'password')
                server = self.config_service.get('MT5', 'server')
                path = self.config_service.get('MT5', 'path')
            except (ValueError, TypeError, configparser.Error) as e: # Catch broader config errors
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

# Example usage (optional, for testing using ConfigService)
if __name__ == '__main__':
    # import configparser # No longer needed directly
    import os
    from logger_setup import setup_logging
    from config_service import ConfigService # Import the service class for testing

    # Setup basic logging for test
    test_log_path = os.path.join(os.path.dirname(__file__), '..', 'logs', 'mt5_connector_test.log')
    setup_logging(log_file_path=test_log_path, log_level_str='DEBUG')

    # --- IMPORTANT: Ensure config/config.ini exists and has REAL MT5 details for this test ---
    try:
        # Instantiate ConfigService directly for the test
        test_config_service = ConfigService(config_file='../config/config.ini') # Adjust path if needed
    except Exception as e:
        print(f"ERROR: Failed to load config/config.ini for testing: {e}")
        sys.exit(1)

    # Check if dummy values might still be present (optional check)
    if 'YOUR_' in test_config_service.get('MT5', 'account', fallback=''):
         print("WARNING: Dummy MT5 credentials might be present in config.ini. Connection test may fail.")
         print("Please ensure config/config.ini has real credentials.")

    # Instantiate connector with the test service instance
    connector = MT5Connector(test_config_service)

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