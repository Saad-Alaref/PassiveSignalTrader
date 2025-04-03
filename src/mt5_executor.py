import MetaTrader5 as mt5
import logging
import time
import configparser
from .mt5_connector import MT5Connector # Use relative import

logger = logging.getLogger('TradeBot')

class MT5Executor:
    """Handles sending trade orders and modifications to the MT5 terminal."""

    def __init__(self, config: configparser.ConfigParser, connector: MT5Connector):
        """
        Initializes the MT5Executor.

        Args:
            config (configparser.ConfigParser): The application configuration.
            connector (MT5Connector): The MT5Connector instance.
        """
        self.config = config
        self.connector = connector
        self.symbol = config.get('MT5', 'symbol', fallback='XAUUSD')
        # Manually strip comments before converting int values
        retry_attempts_str = config.get('Retries', 'requote_retry_attempts', fallback='5').split('#')[0].strip()
        retry_delay_str = config.get('Retries', 'requote_retry_delay_seconds', fallback='4').split('#')[0].strip()
        slippage_str = config.get('Trading', 'max_slippage', fallback='10').split('#')[0].strip()

        self.requote_retries = int(retry_attempts_str)
        self.requote_delay = int(retry_delay_str)
        # Deviation/slippage for market orders (in points)
        self.deviation = int(slippage_str) # Default 10 points slippage

    def _send_order_with_retry(self, request):
        """
        Sends an order request to MT5, handling retries for requotes,
        off quotes, and attempting alternative filling modes (IOC/FOK).

        Args:
            request (dict): The trade request dictionary for mt5.order_send().
                            Must include 'type_filling'.

        Returns:
            tuple(mt5.OrderSendResult, float or None) or None:
                                         A tuple containing the result object and the actual
                                         execution price (for market orders), or None on failure.
                                         (successfully or with non-requote/fill error),
                                         None if max retries are reached or filling modes fail.
        """
        attempt = 0
        # Keep track of which filling modes we've tried for this request
        tried_filling_modes = set()
        if 'type_filling' not in request:
             logger.error("Request dictionary missing 'type_filling'. Cannot send order.")
             return None
        # Make a copy to avoid modifying the original dict passed in if retrying filling mode
        current_request = request.copy()
        tried_filling_modes.add(current_request['type_filling'])

        while attempt < self.requote_retries:
            attempt += 1
            current_filling_mode = current_request['type_filling'] # Log current mode being tried
            logger.info(f"Attempting to send order (Attempt {attempt}/{self.requote_retries}, Filling: {current_filling_mode}): {current_request}")

            if not self.connector.ensure_connection():
                logger.error("Cannot send order, MT5 connection failed.")
                return None # Connection failure is fatal

            try:
                result = mt5.order_send(current_request)

                if result is None:
                    logger.error(f"order_send failed, error code: {mt5.last_error()}")
                    # Don't retry on general failure
                    return None

                logger.info(f"order_send result: retcode={result.retcode}, comment='{result.comment}', request_id={result.request_id}, order={result.order}")

                # --- Success ---
                if result.retcode == mt5.TRADE_RETCODE_DONE:
                    logger.info(f"Order executed successfully. Order Ticket: {result.order}")
                    # Attempt to fetch actual execution price for market orders
                    actual_price = None
                    if request.get("action") == mt5.TRADE_ACTION_DEAL and result.deal:
                        # Sometimes the deal is directly in the result, use it if available
                        try:
                            # Small delay to allow deal processing on server? Might not be needed.
                            # time.sleep(0.1)
                            deals = mt5.history_deals_get(ticket=result.order)
                            if deals and len(deals) > 0:
                                # Find the deal corresponding to this order execution
                                # Usually the first deal if fetched immediately, but check ticket
                                for deal in deals:
                                     if deal.order == result.order and deal.entry == mt5.DEAL_ENTRY_IN: # Entry into market
                                          actual_price = deal.price
                                          logger.info(f"Fetched actual execution price from deal {deal.ticket}: {actual_price}")
                                          break
                            if actual_price is None:
                                logger.warning(f"Could not fetch specific deal price for order {result.order}, using result price {result.price} if available.")
                                actual_price = result.price # Fallback to price in result, might be 0.0
                        except Exception as deal_err:
                            logger.error(f"Error fetching deal details for order {result.order}: {deal_err}", exc_info=True)
                            actual_price = result.price # Fallback

                    return result, actual_price # Return result and price

                # --- Retryable Errors (Requote / Price Off) ---
                elif result.retcode in [mt5.TRADE_RETCODE_REQUOTE, mt5.TRADE_RETCODE_PRICE_OFF]:
                    error_type = "Requote" if result.retcode == mt5.TRADE_RETCODE_REQUOTE else "Off quotes"
                    logger.warning(f"{error_type} received (Attempt {attempt}). Prices: Bid={result.bid}, Ask={result.ask}. Retrying in {self.requote_delay}s...")
                    if attempt < self.requote_retries:
                        time.sleep(self.requote_delay)
                        continue # Continue loop to retry with same request params
                    else:
                        logger.error(f"Max {error_type} retries reached. Order failed.")
                        return None, None # Max retries exceeded

                # --- Invalid Filling Mode Error (Try alternative) ---
                elif result.retcode == mt5.TRADE_RETCODE_INVALID_FILL: # 10030
                    logger.warning(f"Invalid filling mode {current_filling_mode} (Attempt {attempt}). Comment: {result.comment}")
                    # Determine alternative filling mode
                    alternative_filling_mode = None
                    if current_filling_mode == mt5.ORDER_FILLING_IOC and mt5.ORDER_FILLING_FOK not in tried_filling_modes:
                        alternative_filling_mode = mt5.ORDER_FILLING_FOK
                    elif current_filling_mode == mt5.ORDER_FILLING_FOK and mt5.ORDER_FILLING_IOC not in tried_filling_modes:
                         alternative_filling_mode = mt5.ORDER_FILLING_IOC
                    # Add other modes like RETURN if relevant for the broker/exchange

                    if alternative_filling_mode:
                        logger.info(f"Attempting alternative filling mode: {alternative_filling_mode}")
                        current_request['type_filling'] = alternative_filling_mode # Modify the *copy*
                        tried_filling_modes.add(alternative_filling_mode)
                        # No sleep needed here, just retry immediately with new mode
                        # Make sure we don't exceed max attempts overall
                        if attempt < self.requote_retries:
                             # Decrement attempt so the *next* iteration uses the same attempt number but with the new filling mode
                             # This allows the full number of retries for requotes/price_off *after* finding a valid filling mode.
                             attempt -= 1
                             continue # Continue loop to retry with modified request
                        else:
                             logger.error("Max retries reached while attempting alternative filling mode.")
                             return result, None # Return the failure result
                    else:
                        logger.error(f"Invalid filling mode error, and alternative modes already tried or unavailable. Modes tried: {tried_filling_modes}. Order failed.")
                        return result, None # Return the failure result

                # --- Other Unrecoverable Errors ---
                else:
                    logger.error(f"Order failed with unrecoverable error: {result.comment} (retcode={result.retcode})")
                    return result, None # Return the result object indicating the specific error

            except Exception as e:
                logger.error(f"Exception during order_send (Attempt {attempt}): {e}", exc_info=True)
                # Don't retry on unexpected exceptions
                return None, None

        # If loop finishes without returning, it means max retries were hit after requotes/price_off
        logger.error("Max retries reached after requotes/price_off without success.")
        return None, None

    def execute_trade(self, action, symbol, order_type, volume, price=None, sl=None, tp=None, comment="TradeBot Signal"):
        """
        Constructs and sends a trade request (market or pending).

        Args:
            action (str): "BUY" or "SELL". (Note: This is informational, order_type dictates MT5 type)
            symbol (str): The symbol to trade (e.g., "XAUUSD").
            order_type (int): MT5 order type constant (e.g., mt5.ORDER_TYPE_BUY, mt5.ORDER_TYPE_BUY_LIMIT).
            volume (float): The lot size for the trade.
            price (float, optional): The entry price for pending orders. Defaults to None.
            sl (float, optional): Stop loss price. Defaults to None.
            tp (float, optional): Take profit price. Defaults to None.
            comment (str, optional): Order comment. Defaults to "TradeBot Signal".

        Returns:
            tuple(mt5.OrderSendResult, float or None) or None:
                                         The result tuple from _send_order_with_retry.
        """
        if not self.connector.ensure_connection():
            logger.error("Cannot execute trade, MT5 connection failed.")
            return None

        # The 'action' parameter ("BUY" or "SELL") determines the 'order_type' passed in.
        # The MT5 request uses 'type' for order type and 'action' for trade action (DEAL/PENDING etc).
        # The mt5_action variable defined here previously was incorrect and unused.

        # Validate symbol parameter
        if not symbol:
            logger.error("Symbol parameter is missing, cannot execute trade.")
            return None

        # Get symbol info for filling mode etc. using the provided symbol
        symbol_info = mt5.symbol_info(symbol)
        if not symbol_info:
            logger.error(f"Failed to get symbol info for {symbol}, cannot execute trade. Error: {mt5.last_error()}")
            return None

        # Basic request structure
        request = {
            "action": mt5.TRADE_ACTION_DEAL, # Default action
            "symbol": symbol, # Use the provided symbol parameter
            "volume": float(volume),
            "type": order_type,
            "magic": 234001, # Example magic number
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC, # Good till cancelled
            # Default to IOC, _send_order_with_retry will attempt FOK if needed
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        # Set action and price based on order type
        if order_type in [mt5.ORDER_TYPE_BUY, mt5.ORDER_TYPE_SELL]: # Market Order
            request["action"] = mt5.TRADE_ACTION_DEAL
            # Price is ignored by server for market orders
            request["deviation"] = self.deviation # Slippage for market orders
        elif order_type in [mt5.ORDER_TYPE_BUY_LIMIT, mt5.ORDER_TYPE_BUY_STOP, mt5.ORDER_TYPE_SELL_LIMIT, mt5.ORDER_TYPE_SELL_STOP]: # Pending Order
            request["action"] = mt5.TRADE_ACTION_PENDING
            if price is None:
                logger.error("Cannot place pending order without an entry price.")
                return None
            request["price"] = float(price)
        else:
            logger.error(f"Unsupported order type for execution: {order_type}")
            return None

        # Add SL and TP if provided (convert to float)
        if sl is not None:
            request["sl"] = float(sl)
        if tp is not None:
            request["tp"] = float(tp)

        # Zero SL/TP should be omitted or handled carefully depending on broker
        if request.get("sl") == 0.0: del request["sl"]
        if request.get("tp") == 0.0: del request["tp"]


        return self._send_order_with_retry(request)

    def modify_trade(self, ticket, sl=None, tp=None):
        """
        Modifies SL/TP of an existing open position OR a pending order.
        Determines if it's a position or order and sends the appropriate request.

        Args:
            ticket (int): The ticket number of the position or order.
            sl (float, optional): New stop loss price. If None or 0.0, SL is not modified.
            tp (float, optional): New take profit price. If None or 0.0, TP is not modified.

        Returns:
            bool: True if modification request was sent successfully and accepted, False otherwise.
        """
        if sl is None and tp is None:
            logger.warning(f"Modify trade called for ticket {ticket} with no SL or TP specified.")
            return False

        # Convert 0.0 to None as MT5 treats 0 as "no change" or "remove" depending on context
        new_sl = float(sl) if sl is not None and float(sl) != 0.0 else None
        new_tp = float(tp) if tp is not None and float(tp) != 0.0 else None

        if new_sl is None and new_tp is None:
             logger.info(f"Modify trade called for ticket {ticket}, but effective SL/TP are None/0. No modification needed.")
             return True # No change needed is considered success in this context

        if not self.connector.ensure_connection():
            logger.error(f"Cannot modify trade {ticket}, MT5 connection failed.")
            return False

        # --- Check if Position or Order Exists ---
        position_info = mt5.positions_get(ticket=ticket)
        order_info = None
        if not position_info or len(position_info) == 0:
            order_info = mt5.orders_get(ticket=ticket)

        if (not position_info or len(position_info) == 0) and (not order_info or len(order_info) == 0):
             logger.error(f"Cannot modify trade: Ticket {ticket} does not correspond to an open position or a pending order.")
             return False
        # --- End Existence Check ---

        logger.info(f"Attempting to modify ticket {ticket}: New SL={new_sl}, New TP={new_tp}")

        request = None
        is_position = False

        # Use the already fetched position_info
        # position = mt5.positions_get(ticket=ticket) # Redundant check
        if position_info and len(position_info) > 0:
            is_position = True
            pos = position_info[0] # Get the position tuple
            logger.debug(f"Ticket {ticket} identified as an open position.")
            request = {
                "action": mt5.TRADE_ACTION_SLTP, # Use SLTP action for positions
                "position": ticket, # Use 'position' key
                "symbol": pos.symbol,
                # For SLTP action, provide the new value. If a new value is None,
                # provide the *existing* value from the position to keep it unchanged.
                "sl": float(new_sl) if new_sl is not None else float(pos.sl),
                "tp": float(new_tp) if new_tp is not None else float(pos.tp),
            }
            # Ensure 0.0 is sent if the original or new value is effectively zero/None
            if request["sl"] is None: request["sl"] = 0.0
            if request["tp"] is None: request["tp"] = 0.0

        else:
            # Use the already fetched order_info
            # order = mt5.orders_get(ticket=ticket) # Redundant check
            if order_info and len(order_info) > 0:
                ord_info = order_info[0] # Get the order tuple
                logger.debug(f"Ticket {ticket} identified as a pending order.")
                # For pending orders, use TRADE_ACTION_MODIFY
                request = {
                    "action": mt5.TRADE_ACTION_MODIFY,
                    "order": ticket, # Use 'order' key
                    "symbol": ord_info.symbol,
                    "price": ord_info.price_open, # Must provide original price for MODIFY
                    # For MODIFY action, provide the *new* value or 0.0 to remove.
                    # If the new value is None, send the original value to keep it unchanged.
                    "sl": float(new_sl) if new_sl is not None else ord_info.sl,
                    "tp": float(new_tp) if new_tp is not None else ord_info.tp,
                    "type": ord_info.type, # Must provide original type
                    "type_time": ord_info.type_time,
                    "type_filling": ord_info.type_filling,
                }
                # Ensure SL/TP are floats, default to 0.0 if they were originally 0.0 and new value is None
                request["sl"] = float(request["sl"] if request["sl"] is not None else 0.0)
                request["tp"] = float(request["tp"] if request["tp"] is not None else 0.0)
                # Add stoplimit price if it's a stoplimit order
                if ord_info.type in (mt5.ORDER_TYPE_BUY_STOP_LIMIT, mt5.ORDER_TYPE_SELL_STOP_LIMIT):
                    request["stoplimit"] = ord_info.price_stoplimit
            # This else block should theoretically not be reached due to the existence check at the beginning
            # else:
            #     logger.error(f"Could not find position or order for ticket {ticket}. Cannot modify. (This should not happen after initial check)")
            #     return False

        # Send the prepared request
        try:
            action_name = "SLTP" if is_position else "MODIFY"
            logger.debug(f"Sending TRADE_ACTION_{action_name} request for ticket {ticket}: {request}")
            result = mt5.order_send(request)

            if result is None:
                logger.error(f"Modification order_send failed for ticket {ticket}, error code: {mt5.last_error()}")
                return False

            logger.info(f"Modification order_send result for ticket {ticket}: retcode={result.retcode}, comment='{result.comment}'")

            if result.retcode == mt5.TRADE_RETCODE_DONE:
                logger.info(f"Modification request for ticket {ticket} accepted.")
                return True
            else:
                logger.error(f"Modification request for ticket {ticket} failed: {result.comment} (retcode={result.retcode})")
                return False

        except Exception as e:
            logger.error(f"Exception during modification order_send for ticket {ticket}: {e}", exc_info=True)
            return False


    def close_position(self, ticket, volume=None, comment="TradeBot Close"):
        """
        Closes an open position partially or fully.

        Args:
            ticket (int): The ticket number of the position to close.
            volume (float, optional): The volume to close. If None, closes the entire position.
            comment (str, optional): Comment for the closing order.

        Returns:
            bool: True if the closing request was sent successfully and accepted, False otherwise.
        """
        if not self.connector.ensure_connection():
            logger.error(f"Cannot close position {ticket}, MT5 connection failed.")
            return False

        position_info = mt5.positions_get(ticket=ticket)
        if not position_info or len(position_info) == 0:
            logger.error(f"Cannot close position: Ticket {ticket} not found or not an open position.")
            return False

        pos = position_info[0]
        symbol_info = mt5.symbol_info(pos.symbol)
        if not symbol_info:
             logger.error(f"Could not get symbol info for {pos.symbol} to perform close action on ticket {ticket}.")
             return False
        min_volume = symbol_info.volume_min
        volume_step = symbol_info.volume_step
        current_pos_volume = pos.volume

        # Determine volume to close
        if volume is None: # Close full position if volume not specified
             close_volume = current_pos_volume
        else:
             close_volume = round(float(volume), 8) # Ensure float and round

        if close_volume <= 0:
             logger.error(f"Invalid close volume specified ({close_volume}) for ticket {ticket}.")
             return False

        # Check if requested close volume exceeds position volume
        if close_volume > current_pos_volume:
             logger.warning(f"Requested close volume ({close_volume}) > position volume ({current_pos_volume}) for ticket {ticket}. Closing full position.")
             close_volume = current_pos_volume

        # --- Partial Close Checks ---
        is_partial_close = close_volume < current_pos_volume

        if is_partial_close:
             # Check if position is already at minimum volume
             if current_pos_volume <= min_volume:
                  logger.warning(f"Cannot partially close position {ticket}: Position volume ({current_pos_volume}) is already at or below minimum ({min_volume}). Closing full position instead.")
                  close_volume = current_pos_volume # Force full close
                  is_partial_close = False # No longer a partial close
             else:
                  # Check if requested close volume is less than min volume allowed
                  if close_volume < min_volume:
                       logger.error(f"Cannot partially close position {ticket}: Requested close volume ({close_volume}) is less than minimum allowed ({min_volume}).")
                       return False # Abort, invalid volume

                  # Check if remaining volume would be less than minimum
                  remaining_volume = round(current_pos_volume - close_volume, 8)
                  if remaining_volume > 0 and remaining_volume < min_volume:
                       logger.warning(f"Cannot partially close position {ticket}: Remaining volume ({remaining_volume}) would be less than minimum ({min_volume}). Closing full position instead.")
                       close_volume = current_pos_volume # Force full close
                       is_partial_close = False # No longer a partial close

                  # Ensure close volume respects volume step (adjust down if needed)
                  # This logic might need refinement depending on how broker handles steps for closing orders
                  close_volume = round(int(close_volume / volume_step) * volume_step, 8)
                  if close_volume <= 0: # Recalculate remaining after step adjustment
                       logger.error(f"Cannot partially close position {ticket}: Adjusted close volume ({close_volume}) based on step ({volume_step}) is zero or less.")
                       return False
                  remaining_volume = round(current_pos_volume - close_volume, 8)
                  if remaining_volume < min_volume: # Check remaining again after step adjustment
                       logger.warning(f"Cannot partially close position {ticket}: Remaining volume ({remaining_volume}) after step adjustment would be less than minimum ({min_volume}). Closing full position instead.")
                       close_volume = current_pos_volume
                       is_partial_close = False

        # --- End Partial Close Checks ---

        # Determine the closing order type (opposite of the position type)
        close_order_type = mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY

        logger.info(f"Attempting to close {close_volume} lots of position {ticket} ({pos.symbol})")

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "position": ticket, # Specify the position ticket to close
            "symbol": pos.symbol,
            "volume": close_volume,
            "type": close_order_type,
            "price": mt5.symbol_info_tick(pos.symbol).ask if close_order_type == mt5.ORDER_TYPE_BUY else mt5.symbol_info_tick(pos.symbol).bid, # Current market price for closing
            "deviation": self.deviation,
            "magic": pos.magic, # Use the magic number of the original position
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC, # Try IOC first for closing
        }

        # Use the retry logic for closing as well - it returns (result, actual_price)
        result_tuple = self._send_order_with_retry(request)
        result, _ = result_tuple if result_tuple else (None, None) # Unpack, ignore price for close

        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            logger.info(f"Position {ticket} close request accepted (Volume: {close_volume}).")
            return True
        else:
            error_comment = getattr(result, 'comment', 'Unknown Error') if result else 'None Result'
            error_code = getattr(result, 'retcode', 'N/A') if result else 'N/A'
            logger.error(f"Failed to close position {ticket}: {error_comment} (retcode={error_code})")
            return False


    def modify_sl_to_breakeven(self, ticket, comment="TradeBot BE"):
        """
        Modifies the Stop Loss of an open position to its entry price (breakeven).

        Args:
            ticket (int): The ticket number of the position.
            comment (str, optional): Comment for the modification.

        Returns:
            bool: True if modification was successful, False otherwise.
        """
        if not self.connector.ensure_connection():
            logger.error(f"Cannot modify SL to BE for {ticket}, MT5 connection failed.")
            return False

        # Ensure the ticket corresponds to an open position
        position_info = mt5.positions_get(ticket=ticket)
        if not position_info or len(position_info) == 0:
            # Check if it's a pending order instead
            order_info = mt5.orders_get(ticket=ticket)
            if order_info and len(order_info) > 0:
                 logger.error(f"Cannot set BE: Ticket {ticket} is a pending order, not an open position.")
            else:
                 logger.error(f"Cannot set BE: Ticket {ticket} not found or not an open position.")
            return False

        pos = position_info[0]
        entry_price = pos.price_open
        current_sl = pos.sl

        # Check if SL is already at breakeven or better
        # (Consider adding a small buffer if needed, e.g., +1 pip for spread)
        if current_sl == entry_price:
             logger.info(f"SL for position {ticket} is already at breakeven ({entry_price}). No modification needed.")
             return True
        # Optional: Check if moving SL to BE would violate distance rules (though SLTP action might handle this)

        logger.info(f"Attempting to modify SL to Breakeven ({entry_price}) for position {ticket}")

        # Use the existing modify_trade logic, passing only the new SL
        # Important: modify_trade handles both positions and orders, so this is safe
        return self.modify_trade(ticket=ticket, sl=entry_price) # TP will be kept as is (None means no change)


    def delete_pending_order(self, ticket, comment="TradeBot Cancel"):
         """
         Deletes a pending order.

         Args:
             ticket (int): The ticket number of the pending order.
             comment (str, optional): Comment for the deletion request.

         Returns:
             bool: True if deletion request was sent successfully and accepted, False otherwise.
         """
         if not self.connector.ensure_connection():
             logger.error(f"Cannot delete pending order {ticket}, MT5 connection failed.")
             return False

         # Verify it's a pending order first
         order_info = mt5.orders_get(ticket=ticket)
         if not order_info or len(order_info) == 0:
             logger.error(f"Cannot delete order: Ticket {ticket} not found or is not a pending order.")
             # Check if it might be an open position instead
             position_info = mt5.positions_get(ticket=ticket)
             if position_info and len(position_info) > 0:
                  logger.warning(f"Attempted to delete ticket {ticket}, but it's an open position, not a pending order.")
             return False

         ord_info = order_info[0]
         # Double check it's actually a pending order type
         if ord_info.type not in [mt5.ORDER_TYPE_BUY_LIMIT, mt5.ORDER_TYPE_SELL_LIMIT,
                                  mt5.ORDER_TYPE_BUY_STOP, mt5.ORDER_TYPE_SELL_STOP,
                                  mt5.ORDER_TYPE_BUY_STOP_LIMIT, mt5.ORDER_TYPE_SELL_STOP_LIMIT]:
              logger.error(f"Ticket {ticket} exists but is not a deletable pending order type (Type: {ord_info.type}).")
              return False


         logger.info(f"Attempting to delete pending order {ticket} ({ord_info.symbol})")

         request = {
             "action": mt5.TRADE_ACTION_REMOVE, # Action for deleting pending orders
             "order": ticket, # Specify the order ticket
             "comment": comment,
         }

         # Send the delete request (retry logic might not be necessary for REMOVE, but using it is safe)
         # Note: _send_order_with_retry expects 'type_filling', which REMOVE doesn't use.
         # We need a direct order_send call here or adapt the retry logic. Let's use direct call for simplicity.
         try:
             logger.debug(f"Sending TRADE_ACTION_REMOVE request for order {ticket}: {request}")
             result = mt5.order_send(request)

             if result is None:
                 logger.error(f"Pending order deletion failed for ticket {ticket}, error code: {mt5.last_error()}")
                 return False

             logger.info(f"Pending order deletion result for ticket {ticket}: retcode={result.retcode}, comment='{result.comment}'")

             if result.retcode == mt5.TRADE_RETCODE_DONE:
                 logger.info(f"Pending order {ticket} delete request accepted.")
                 return True
             else:
                 logger.error(f"Failed to delete pending order {ticket}: {result.comment} (retcode={result.retcode})")
                 return False
         except Exception as e:
              logger.error(f"Exception during pending order deletion for ticket {ticket}: {e}", exc_info=True)
              return False


# Example usage (optional, for testing)
if __name__ == '__main__':
    import configparser
    import os
    import sys
    from logger_setup import setup_logging
    from mt5_connector import MT5Connector

    # Setup basic logging for test
    test_log_path = os.path.join(os.path.dirname(__file__), '..', 'logs', 'mt5_executor_test.log')
    setup_logging(log_file_path=test_log_path, log_level_str='DEBUG')

    # Load dummy config
    example_config_path = os.path.join(os.path.dirname(__file__), '..', 'config', 'config.example.ini')
    if not os.path.exists(example_config_path):
        print(f"ERROR: config.example.ini not found at {example_config_path}. Cannot run test.")
        sys.exit(1)

    config = configparser.ConfigParser()
    config.read(example_config_path)
    # --- IMPORTANT: Fill in REAL MT5 details in config.example.ini for this test to work ---

    if 'YOUR_' in config.get('MT5', 'account', fallback=''):
         print("WARNING: Dummy MT5 credentials found in config. Executor test needs a live connection.")
         # sys.exit(1) # Optionally exit

    connector = MT5Connector(config)
    executor = MT5Executor(config, connector)

    print("Connecting MT5 for Executor test...")
    if not connector.connect():
        print("MT5 Connection Failed. Cannot run executor tests.")
        sys.exit(1)
    print("MT5 Connected.")

    symbol = config.get('MT5', 'symbol')
    volume = 0.01 # Use minimum volume for testing

    # --- Test Cases ---
    print("\n--- Testing Trade Execution ---")

    # Get current prices for realistic test orders
    tick = mt5.symbol_info_tick(symbol)
    if not tick:
        print(f"Could not get tick for {symbol}. Aborting tests.")
        connector.disconnect()
        sys.exit(1)

    current_ask = tick.ask
    current_bid = tick.bid
    point = mt5.symbol_info(symbol).point
    sl_distance = 100 * point # Example SL distance 100 points
    tp_distance = 200 * point # Example TP distance 200 points

    # 1. Market Buy Order
    print(f"\nTest 1: Market BUY {volume} lots of {symbol}...")
    buy_sl = round(current_bid - sl_distance, 5) # SL below bid for BUY
    buy_tp = round(current_bid + tp_distance, 5) # TP above bid for BUY
    market_buy_result = executor.execute_trade("BUY", mt5.ORDER_TYPE_BUY, volume, sl=buy_sl, tp=buy_tp)
    if market_buy_result and market_buy_result.retcode == mt5.TRADE_RETCODE_DONE:
        print(f"  Market BUY Success! Order Ticket: {market_buy_result.order}")
        # Test Modification - Add TP only
        time.sleep(2) # Allow order processing
        print(f"  Attempting to modify ticket {market_buy_result.order} - setting new TP...")
        mod_tp = round(buy_tp + 50 * point, 5)
        mod_success = executor.modify_trade(market_buy_result.order, tp=mod_tp)
        print(f"  Modification attempt result: {mod_success}")
    else:
        print(f"  Market BUY Failed. Result: {market_buy_result}")

    # 2. Pending Sell Limit Order (Price above current Bid)
    print(f"\nTest 2: Pending SELL LIMIT {volume} lots of {symbol}...")
    limit_price = round(current_bid + 50 * point, 5) # Price above current bid
    sell_sl = round(limit_price + sl_distance, 5) # SL above entry for SELL
    sell_tp = round(limit_price - tp_distance, 5) # TP below entry for SELL
    pending_sell_result = executor.execute_trade("SELL", mt5.ORDER_TYPE_SELL_LIMIT, volume, price=limit_price, sl=sell_sl, tp=sell_tp)
    if pending_sell_result and pending_sell_result.retcode == mt5.TRADE_RETCODE_DONE:
        print(f"  Pending SELL LIMIT Success! Order Ticket: {pending_sell_result.order}")
        # Test Modification - Add SL only
        time.sleep(2)
        print(f"  Attempting to modify ticket {pending_sell_result.order} - setting new SL...")
        mod_sl = round(sell_sl + 20 * point, 5)
        mod_success = executor.modify_trade(pending_sell_result.order, sl=mod_sl)
        print(f"  Modification attempt result: {mod_success}")
    else:
        print(f"  Pending SELL LIMIT Failed. Result: {pending_sell_result}")

    # Note: These orders are placed on the account used in config!
    # Be sure to use a demo account for testing.
    print("\n*** IMPORTANT: Check your MT5 terminal (Demo Account!) for test orders/positions. ***")

    # --- Cleanup ---
    print("\nDisconnecting MT5...")
    connector.disconnect()
    print("Test finished.")