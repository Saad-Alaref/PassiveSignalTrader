import logging
import configparser
from .mt5_data_fetcher import MT5DataFetcher # Use relative import
import MetaTrader5 as mt5
logger = logging.getLogger('TradeBot')

class TradeCalculator:
    """
    Calculates trade parameters, primarily the lot size (volume).
    Currently implements fixed lot size based on configuration.
    """

    def __init__(self, config: configparser.ConfigParser, data_fetcher: MT5DataFetcher):
        """
        Initializes the TradeCalculator.

        Args:
            config (configparser.ConfigParser): The application configuration.
            data_fetcher (MT5DataFetcher): Instance for fetching market/account data
                                           (used for future calculation methods).
        """
        self.config = config
        self.fetcher = data_fetcher # Store fetcher for future use
        self.lot_size_method = config.get('Trading', 'lot_size_method', fallback='fixed').lower()
        # Manually strip comments before converting float values
        fixed_lot_size_str = config.get('Trading', 'fixed_lot_size', fallback='0.01').split('#')[0].strip()
        default_lot_size_str = config.get('Trading', 'default_lot_size', fallback='0.01').split('#')[0].strip()

        self.fixed_lot_size = float(fixed_lot_size_str)
        self.default_lot_size = float(default_lot_size_str)

        logger.info(f"TradeCalculator initialized. Method='{self.lot_size_method}', FixedSize={self.fixed_lot_size}, DefaultSize={self.default_lot_size}")

        # Validate fixed lot size is positive
        if self.fixed_lot_size <= 0:
            logger.warning(f"Configured fixed_lot_size ({self.fixed_lot_size}) is not positive. Using default_lot_size ({self.default_lot_size}) as fallback for fixed method.")
            self.fixed_lot_size = self.default_lot_size
        if self.default_lot_size <= 0:
             logger.error("Default lot size is not positive. Setting to 0.01 as emergency fallback.")
             self.default_lot_size = 0.01 # Emergency fallback


    def calculate_lot_size(self, signal_data: dict):
        """
        Calculates the lot size for the trade based on the configured method.

        Args:
            signal_data (dict): The dictionary containing signal details (might be needed
                                for future risk-based calculations, e.g., SL price).

        Returns:
            float: The calculated lot size, or the default lot size if calculation fails.
        """
        logger.debug(f"Calculating lot size using method: {self.lot_size_method}")
        calculated_lot = self.default_lot_size # Start with default as fallback

        # --- Determine Base Lot Size ---
        if self.lot_size_method == 'fixed':
            calculated_lot = self.fixed_lot_size
            logger.info(f"Base lot size (fixed): {calculated_lot}")

        # --- Future Implementation Examples ---
        # elif self.lot_size_method == 'risk_percent_equity':
        #     # ... (calculation logic) ...
        #     calculated_lot = ...
        #     logger.info(f"Base lot size (risk %): {calculated_lot}")

        else:
            logger.warning(f"Unsupported lot_size_method '{self.lot_size_method}'. Using default: {self.default_lot_size}")
            calculated_lot = self.default_lot_size

        # --- Adjust Lot Size based on Broker Constraints ---
        try:
            symbol_info = self.fetcher.get_symbol_info(self.config.get('MT5', 'symbol'))
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


    def calculate_auto_sl_price(self, symbol: str, order_type: int, entry_price: float, volume: float, risk_usd: float):
        """
        Calculates the Stop Loss price based on a fixed USD risk amount.

        Args:
            symbol (str): The trading symbol.
            order_type (int): mt5.ORDER_TYPE_BUY or mt5.ORDER_TYPE_SELL.
            entry_price (float): The entry price of the trade.
            volume (float): The volume (lot size) of the trade.
            risk_usd (float): The maximum desired risk in account currency (USD).

        Returns:
            float or None: The calculated SL price, or None if calculation fails.
        """
        if not symbol or not entry_price or not volume or volume <= 0 or not risk_usd or risk_usd <= 0:
            logger.error("Invalid parameters for calculate_auto_sl_price.")
            return None

        symbol_info = self.fetcher.get_symbol_info(symbol)
        if not symbol_info:
            logger.error(f"Cannot calculate Auto SL: Failed to get symbol info for {symbol}.")
            return None

        # --- Calculate Tick Value ---
        tick = self.fetcher.get_symbol_tick(symbol)
        if not tick:
            logger.error(f"Cannot calculate Auto SL: Failed to get current tick for {symbol}.")
            return None

        # Use order_calc_profit to find the value of one tick move for 1 lot
        tick_value_per_lot = mt5.order_calc_profit(order_type, symbol, 1.0, tick.ask, tick.ask + symbol_info.point)
        if order_type == mt5.ORDER_TYPE_SELL: # For sell, calculate profit of price going down one tick
             tick_value_per_lot = mt5.order_calc_profit(order_type, symbol, 1.0, tick.bid, tick.bid - symbol_info.point)

        # order_calc_profit returns the profit/loss. We need the absolute value representing the tick value.
        if tick_value_per_lot is None:
             logger.error(f"Cannot calculate Auto SL: order_calc_profit failed for {symbol}. Error: {mt5.last_error()}")
             return None
        tick_value_per_lot = abs(tick_value_per_lot) # Ensure positive value

        if tick_value_per_lot == 0:
            logger.error(f"Cannot calculate Auto SL: Calculated tick value is zero for {symbol}.")
            return None
        # --- End Calculate Tick Value ---

        point = symbol_info.point
        digits = symbol_info.digits

        # Calculate value per point for the specific trade volume
        value_per_point = tick_value_per_lot * volume

        if value_per_point == 0:
             logger.error(f"Cannot calculate Auto SL: Value per point is zero for volume {volume}.")
             return None

        # Calculate required SL distance in points
        sl_distance_points = abs(risk_usd / value_per_point)

        # Calculate SL distance in price terms
        sl_distance_price = sl_distance_points * point

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

        logger.info(f"Calculated Auto SL for {symbol} {order_type}: Entry={entry_price}, Risk=${risk_usd}, Volume={volume} -> SL Distance={sl_distance_price:.{digits}f} ({sl_distance_points:.2f} points) -> SL Price={sl_price_rounded}")
        return sl_price_rounded


# Example usage (optional, for testing)
if __name__ == '__main__':
    import configparser
    import os
    import sys
    from logger_setup import setup_logging
    # Need dummy fetcher/connector for init, but calculation doesn't use them yet
    from mt5_connector import MT5Connector
    from mt5_data_fetcher import MT5DataFetcher

    # Setup basic logging for test
    test_log_path = os.path.join(os.path.dirname(__file__), '..', 'logs', 'calculator_test.log')
    setup_logging(log_file_path=test_log_path, log_level_str='DEBUG')

    # Load dummy config
    example_config_path = os.path.join(os.path.dirname(__file__), '..', 'config', 'config.example.ini')
    if not os.path.exists(example_config_path):
        print(f"ERROR: config.example.ini not found at {example_config_path}. Cannot run test.")
        sys.exit(1)

    config = configparser.ConfigParser()
    config.read(example_config_path)

    # Create dummy fetcher (doesn't need real connection for fixed lot size)
    dummy_connector = MT5Connector(config) # Needs config but won't connect
    dummy_fetcher = MT5DataFetcher(dummy_connector)

    calculator = TradeCalculator(config, dummy_fetcher)

    print("Testing Trade Calculator...")

    # Test with fixed lot size (default in example config)
    print(f"\nTesting method: {calculator.lot_size_method}")
    dummy_signal = {"stop_loss": "3100"} # Pass dummy signal data
    lot_size = calculator.calculate_lot_size(dummy_signal)
    print(f"Calculated Lot Size: {lot_size}")
    expected_fixed = config.getfloat('Trading', 'fixed_lot_size', fallback=0.01)
    print(f"Expected: {expected_fixed}")
    assert lot_size == expected_fixed

    # Test fallback if method is unknown
    print("\nTesting fallback for unknown method...")
    config['Trading']['lot_size_method'] = 'unknown_method'
    calculator_fallback = TradeCalculator(config, dummy_fetcher)
    lot_size_fallback = calculator_fallback.calculate_lot_size(dummy_signal)
    print(f"Calculated Lot Size: {lot_size_fallback}")
    expected_default = config.getfloat('Trading', 'default_lot_size', fallback=0.01)
    print(f"Expected (default): {expected_default}")
    assert lot_size_fallback == expected_default

    print("\nTest finished.")