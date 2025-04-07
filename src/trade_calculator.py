import logging
# import configparser # No longer needed directly
from .config_service import config_service # Import the service
from .mt5_data_fetcher import MT5DataFetcher # Use relative import
import MetaTrader5 as mt5
logger = logging.getLogger('TradeBot')

class TradeCalculator:
    """
    Calculates trade parameters, primarily the lot size (volume).
    Currently implements fixed lot size based on configuration.
    """

    def __init__(self, config_service_instance, data_fetcher: MT5DataFetcher): # Inject service
        """
        Initializes the TradeCalculator.

        Args:
            config_service_instance (ConfigService): The application config service.
            data_fetcher (MT5DataFetcher): Instance for fetching market/account data.
        """
        self.config_service = config_service_instance # Store service instance
        self.fetcher = data_fetcher
        logger.info("TradeCalculator initialized.")
        # Lot size parameters will be read dynamically in calculate_lot_size


    def calculate_lot_size(self, signal_data: dict):
        """
        Calculates the lot size for the trade based on the configured method.

        Args:
            signal_data (dict): The dictionary containing signal details (might be needed
                                for future risk-based calculations, e.g., SL price).

        Returns:
            float: The calculated lot size, or the default lot size if calculation fails.
        """
        # --- Read config values dynamically ---
        lot_size_method = self.config_service.get('Trading', 'lot_size_method', fallback='fixed').lower() # Use service
        fixed_lot_size = self.config_service.getfloat('Trading', 'fixed_lot_size', fallback=0.01) # Use service
        default_lot_size = self.config_service.getfloat('Trading', 'default_lot_size', fallback=0.01) # Use service

        # Validate default lot size (emergency fallback)
        if default_lot_size <= 0:
             logger.error("Default lot size is not positive. Using 0.01 as emergency fallback.")
             default_lot_size = 0.01

        # Validate fixed lot size (use default if invalid)
        if fixed_lot_size <= 0:
            logger.warning(f"Configured fixed_lot_size ({fixed_lot_size}) is not positive. Using default_lot_size ({default_lot_size}) as fallback for fixed method.")
            fixed_lot_size = default_lot_size
        # --- End Read config values ---

        logger.debug(f"Calculating lot size using method: {lot_size_method}")
        calculated_lot = default_lot_size # Start with default as fallback

        # --- Determine Base Lot Size ---
        if lot_size_method == 'fixed':
            calculated_lot = fixed_lot_size
            logger.info(f"Base lot size (fixed): {calculated_lot}")

        # --- Future Implementation Examples ---
        # elif lot_size_method == 'risk_percent_equity':
        #     # ... (calculation logic) ...
        #     calculated_lot = ...
        #     logger.info(f"Base lot size (risk %): {calculated_lot}")

        else:
            logger.warning(f"Unsupported lot_size_method '{lot_size_method}'. Using default: {default_lot_size}")
            calculated_lot = default_lot_size

        # --- Adjust Lot Size based on Broker Constraints ---
        try:
            # Read symbol dynamically
            mt5_symbol = self.config_service.get('MT5', 'symbol', fallback='XAUUSD') # Use service
            symbol_info = self.fetcher.get_symbol_info(mt5_symbol)
            if symbol_info:
                volume_min = symbol_info.volume_min
                volume_max = symbol_info.volume_max
                volume_step = symbol_info.volume_step

                logger.debug(f"Symbol constraints: Min={volume_min}, Max={volume_max}, Step={volume_step}")

                # Clamp between min and max
                adjusted_lot = max(volume_min, min(volume_max, calculated_lot))

                # Normalize according to step
                if volume_step > 0:
                     # Calculate how many steps fit into the adjusted lot
                     steps = round(adjusted_lot / volume_step)
                     normalized_lot = steps * volume_step
                     # Ensure normalization doesn't violate min/max due to rounding
                     final_lot = max(volume_min, min(volume_max, normalized_lot))
                else:
                     # Should not happen, but handle defensively
                     final_lot = adjusted_lot

                # Round to avoid potential floating point issues (e.g., 8 decimal places)
                final_lot = round(final_lot, 8)

                if final_lot != calculated_lot:
                    logger.info(f"Adjusted lot size from {calculated_lot} to {final_lot} based on symbol constraints.")
                else:
                    logger.info(f"Calculated lot size {final_lot} meets symbol constraints.")

                return final_lot
            else:
                logger.error("Could not get symbol info to validate lot size. Using unvalidated calculated lot.")
                return round(calculated_lot, 8) # Return rounded unvalidated lot

        except Exception as e:
            logger.error(f"Error adjusting lot size: {e}. Using unvalidated calculated lot.", exc_info=True)
            return round(calculated_lot, 8) # Return rounded unvalidated lot

        # --- Future Implementation Examples ---
        # elif self.lot_size_method == 'risk_percent_equity':
        #     try:
        #         # Placeholder for risk % calculation logic
        #         # Requires account equity, SL distance, symbol info (value per point)
        #         # equity = self.fetcher.get_account_info().equity
        #         # stop_loss_price = float(signal_data.get('stop_loss'))
        #         # entry_price = ... # Need entry price too
        #         # symbol_info = self.fetcher.get_symbol_info(...)
        #         # risk_percent = self.config.getfloat('Trading', 'risk_percent_per_trade')
        #         # calculated_lot = self._calculate_risk_percent_lot(...)
        #         # logger.info(f"Calculated lot size based on risk %: {calculated_lot}")
        #         # return calculated_lot
        #         logger.warning("Risk percent equity lot size calculation not yet implemented.")
        #         pass # Fall through to default
        #     except Exception as e:
        #         logger.error(f"Error calculating risk percent equity lot size: {e}", exc_info=True)
        #         # Fall through to default

        # This part is now handled within the main logic block above

    # --- Helper for future methods ---
    # def _calculate_risk_percent_lot(self, equity, risk_percent, sl_price, entry_price, symbol_info):
    #     # Implementation details for risk % calculation
    #     # ... calculate SL distance in points ...
    #     # ... get value per point per lot ...
    #     # ... apply formula ...
    #     # ... handle potential division by zero, normalize lot size ...
    #     return calculated_lot


    def calculate_sl_from_distance(self, symbol: str, order_type: int, entry_price: float, sl_price_distance: float):
        """
        Calculates the Stop Loss price based on a fixed price distance from entry.

        Args:
            symbol (str): The trading symbol.
            order_type (int): mt5.ORDER_TYPE_BUY or mt5.ORDER_TYPE_SELL.
            entry_price (float): The entry price of the trade.
            sl_price_distance (float): The desired SL distance in price units (e.g., 5.0 for $5 price move).

        Returns:
            float or None: The calculated SL price, or None if calculation fails.
        """
        log_prefix = f"[CalcSLFromDist][{symbol}]" # Add log prefix
        logger.debug(f"{log_prefix} Inputs: entry={entry_price}, distance={sl_price_distance}, order_type={order_type}")
        if not symbol or not entry_price or sl_price_distance <= 0:
            logger.error("Invalid parameters for calculate_sl_from_distance.")
            return None

        symbol_info = self.fetcher.get_symbol_info(symbol)
        if not symbol_info:
            logger.error(f"{log_prefix} Cannot calculate SL: Failed to get symbol info for {symbol}.")
            return None

        digits = symbol_info.digits
        # The sl_price_distance is already the value we need
        sl_distance_price = abs(sl_price_distance) # Ensure positive distance
        logger.debug(f"{log_prefix} Using direct price distance: {sl_distance_price}")

        # Determine SL price based on order type
        sl_price = None
        if order_type == mt5.ORDER_TYPE_BUY:
            sl_price = entry_price - sl_distance_price
        elif order_type == mt5.ORDER_TYPE_SELL:
            sl_price = entry_price + sl_distance_price
        else:
            logger.error(f"Cannot calculate Auto SL: Invalid order type {order_type}.")
            return None

        # Round the final SL price to the symbol's digits
        sl_price_rounded = round(sl_price, digits)

        logger.info(f"{log_prefix} Calculated SL for {symbol} {order_type}: Entry={entry_price}, Distance={sl_distance_price:.{digits}f} -> SL Price={sl_price_rounded}")
        return sl_price_rounded

    def calculate_tp_from_distance(self, symbol: str, order_type: int, entry_price: float, tp_price_distance: float):
        """
        Calculates the Take Profit price based on a fixed price distance from entry.

        Args:
            symbol (str): The trading symbol.
            order_type (int): mt5.ORDER_TYPE_BUY or mt5.ORDER_TYPE_SELL.
            entry_price (float): The entry price of the trade.
            tp_price_distance (float): The desired TP distance in price units (e.g., 10.0 for $10 price move).

        Returns:
            float or None: The calculated TP price, or None if calculation fails.
        """
        log_prefix = f"[CalcTPFromDist][{symbol}]" # Add log prefix
        logger.debug(f"{log_prefix} Inputs: entry={entry_price}, distance={tp_price_distance}, order_type={order_type}")
        if not symbol or not entry_price or tp_price_distance <= 0:
            logger.error("Invalid parameters for calculate_tp_from_distance.")
            return None

        symbol_info = self.fetcher.get_symbol_info(symbol)
        if not symbol_info:
            logger.error(f"{log_prefix} Cannot calculate TP: Failed to get symbol info for {symbol}.")
            return None

        digits = symbol_info.digits
        # The tp_price_distance is already the value we need
        tp_distance_price = abs(tp_price_distance) # Ensure positive distance
        logger.debug(f"{log_prefix} Using direct price distance: {tp_distance_price}")

        # Determine TP price based on order type
        tp_price = None
        if order_type == mt5.ORDER_TYPE_BUY:
            tp_price = entry_price + tp_distance_price
        elif order_type == mt5.ORDER_TYPE_SELL:
            tp_price = entry_price - tp_distance_price
        else:
            logger.error(f"Cannot calculate Auto TP: Invalid order type {order_type}.")
            return None

        # Round the final TP price to the symbol's digits
        tp_price_rounded = round(tp_price, digits)

        logger.info(f"{log_prefix} Calculated TP for {symbol} {order_type}: Entry={entry_price}, Distance={tp_distance_price:.{digits}f} -> TP Price={tp_price_rounded}")
        return tp_price_rounded

    def calculate_trailing_sl_price(self, symbol: str, order_type: int, current_price: float, trail_distance_price: float):
        """
        Calculates the Trailing Stop Loss price based on the current market price
        and a fixed price distance.

        Args:
            symbol (str): The trading symbol.
            order_type (int): mt5.ORDER_TYPE_BUY or mt5.ORDER_TYPE_SELL.
            current_price (float): The current relevant market price (Bid for BUY, Ask for SELL).
            trail_distance_price (float): The desired distance behind the current price, in price units.

        Returns:
            float or None: The calculated Trailing SL price, or None if calculation fails.
        """
        log_prefix = f"[CalcTrailSL][{symbol}]" # Add log prefix
        logger.debug(f"{log_prefix} Inputs: current_price={current_price}, distance={trail_distance_price}, order_type={order_type}")
        if not symbol or not current_price or trail_distance_price <= 0:
            logger.error("Invalid parameters for calculate_trailing_sl_price.")
            return None

        symbol_info = self.fetcher.get_symbol_info(symbol)
        if not symbol_info:
            logger.error(f"{log_prefix} Cannot calculate Trailing SL: Failed to get symbol info for {symbol}.")
            return None

        digits = symbol_info.digits
        # The trail_distance_price is already the value we need
        sl_distance_price = abs(trail_distance_price) # Ensure positive distance
        logger.debug(f"{log_prefix} Using direct price distance: {sl_distance_price}")

        # Determine SL price based on order type and *current* price
        sl_price = None
        if order_type == mt5.ORDER_TYPE_BUY:
            # For a BUY, SL trails below the current BID price
            sl_price = current_price - sl_distance_price
        elif order_type == mt5.ORDER_TYPE_SELL:
            # For a SELL, SL trails above the current ASK price
            sl_price = current_price + sl_distance_price
        else:
            logger.error(f"Cannot calculate Trailing SL: Invalid order type {order_type}.")
            return None

        # Round the final SL price to the symbol's digits
        sl_price_rounded = round(sl_price, digits)

        logger.info(f"{log_prefix} Calculated Trailing SL for {symbol} {order_type}: CurrentPrice={current_price}, Distance={sl_distance_price:.{digits}f} -> SL Price={sl_price_rounded}")
        return sl_price_rounded


# Example usage (optional, for testing)
if __name__ == '__main__':
    # import configparser # No longer needed
    import os
    import sys
    from logger_setup import setup_logging
    from config_service import ConfigService # Import service for testing
    # Need dummy fetcher/connector for init
    from mt5_connector import MT5Connector
    from mt5_data_fetcher import MT5DataFetcher

    # Setup basic logging for test
    test_log_path = os.path.join(os.path.dirname(__file__), '..', 'logs', 'calculator_test.log')
    setup_logging(log_file_path=test_log_path, log_level_str='DEBUG')

    # --- IMPORTANT: Ensure config/config.ini exists for this test ---
    try:
        # Instantiate ConfigService directly for the test
        test_config_service = ConfigService(config_file='../config/config.ini') # Adjust path if needed
    except Exception as e:
        print(f"ERROR: Failed to load config/config.ini for testing: {e}")
        sys.exit(1)


    # Create dummy fetcher (doesn't need real connection for these tests)
    # Connector needs service, but won't connect
    dummy_connector = MT5Connector(test_config_service)
    dummy_fetcher = MT5DataFetcher(dummy_connector)

    # Instantiate calculator with the test service
    calculator = TradeCalculator(test_config_service, dummy_fetcher)

    print("Testing Trade Calculator...")

    # Test with fixed lot size (default in example config)
    # print(f"\nTesting method: {calculator.lot_size_method}") # Method is read inside function now
    dummy_signal = {"stop_loss": "3100"} # Pass dummy signal data
    lot_size = calculator.calculate_lot_size(dummy_signal)
    print(f"Calculated Lot Size: {lot_size}")
    expected_fixed = test_config_service.getfloat('Trading', 'fixed_lot_size', fallback=0.01) # Use service
    print(f"Expected: {expected_fixed}")
    assert lot_size == expected_fixed

    # Test fallback if method is unknown
    print("\nTesting fallback for unknown method...")
    # To test fallback, we'd need to modify the dummy config file or create a new service instance
    # For simplicity, we assume the config service correctly reads the method
    # calculator_fallback = TradeCalculator(test_config_service, dummy_fetcher) # Re-init not needed if service is used internally
    lot_size_fallback = calculator_fallback.calculate_lot_size(dummy_signal)
    print(f"Calculated Lot Size: {lot_size_fallback}")
    expected_default = test_config_service.getfloat('Trading', 'default_lot_size', fallback=0.01) # Use service
    print(f"Expected (default): {expected_default}")
    assert lot_size_fallback == expected_default

    print("\nTest finished.")