import logging
import MetaTrader5 as mt5
from datetime import datetime, timezone
from abc import ABC, abstractmethod

# Import necessary components
from .state_manager import StateManager
from .mt5_executor import MT5Executor
from .telegram_sender import TelegramSender
from .duplicate_checker import DuplicateChecker
from .mt5_data_fetcher import MT5DataFetcher
from .tp_assignment import get_tp_assignment_strategy, ConfigValidator, ConfigValidationError # Removed SequenceMapper

logger = logging.getLogger('TradeBot')

# --- Base Strategy Class ---
class ExecutionStrategy(ABC):
    """Abstract base class for different trade execution strategies."""
    def __init__(self, *args, **kwargs):
        # Allow dependency injection for testing
        self.action = kwargs.get('action', None)
        self.trade_symbol = kwargs.get('trade_symbol', None)
        self.lot_size = kwargs.get('lot_size', None)
        self.exec_sl = kwargs.get('exec_sl', None)
        self.numeric_tps = kwargs.get('numeric_tps', None) # Note: Primarily used by old logic, new TP system uses signal_data
        self.message_id = kwargs.get('message_id', None)
        self.config_service = kwargs.get('config_service', None) or kwargs.get('config_service_instance', None)
        self.mt5_fetcher = kwargs.get('mt5_fetcher', None)
        self.mt5_executor = kwargs.get('mt5_executor', None)
        self.state_manager = kwargs.get('state_manager', None)
        self.telegram_sender = kwargs.get('telegram_sender', None)
        self.duplicate_checker = kwargs.get('duplicate_checker', None)
        self.log_prefix = kwargs.get('log_prefix', "")
        self.trade_calculator = kwargs.get('trade_calculator', None)
        self.debug_channel_id = getattr(self.telegram_sender, 'debug_target_channel_id', None) if self.telegram_sender else None
        # self.tp_strategy = "first_tp_full_close" # Obsolete: TP assignment handled by TPAssignment config
        # Common initializations (skip if missing for test)
        if self.mt5_fetcher and self.trade_symbol:
            self.symbol_info = self.mt5_fetcher.get_symbol_info(self.trade_symbol)
            self.min_lot = self.symbol_info.volume_min if self.symbol_info else 0.01
            self.lot_step = self.symbol_info.volume_step if self.symbol_info else 0.01
            self.digits = self.symbol_info.digits if self.symbol_info else 5
            self.base_split_lot = max(self.min_lot, 0.01)
        else:
            self.symbol_info = None
            self.min_lot = 0.01
            self.lot_step = 0.01
            self.digits = 5
            self.base_split_lot = 0.01


    @abstractmethod
    async def execute(self):
        """Executes the trade strategy."""
        pass

    def _store_trade_info(self, ticket, entry_price, volume, assigned_tp, is_pending=False, sequence_info=None, auto_tp_applied=False, take_profits_list_ref=None):
        """
        Helper to prepare trade info dict and store it via state manager.
        Now accepts take_profits_list_ref for single trade strategy.
        """
        open_time = datetime.now(timezone.utc)
        # Prepare the dictionary expected by StateManager.add_active_trade
        trade_info_data = {
            'ticket': ticket,
            'symbol': self.trade_symbol,
            'open_time': open_time,
            'entry_price': entry_price,
            'initial_sl': self.exec_sl,
            'original_volume': volume, # Volume of this specific trade/order
            # For multi-trade strategies, store only the assigned TP.
            # For single trade, store the original list passed for reference/reporting.
            'all_tps': take_profits_list_ref if take_profits_list_ref is not None else ([assigned_tp] if assigned_tp is not None else []),
            # 'tp_strategy': self.tp_strategy, # Obsolete
            'assigned_tp': assigned_tp, # The TP actually set on this order/position
            'is_pending': is_pending,
            'sequence_info': sequence_info,
            'original_msg_id': self.message_id,  # Link trade to original signal message ID
            # Other fields like tsl_active, auto_sl_pending_timestamp are initialized within TradeInfo dataclass
        }
        if self.state_manager:
            # Pass the dict and auto_tp_applied flag to state_manager
            self.state_manager.add_active_trade(trade_info_data, auto_tp_applied=auto_tp_applied)
            # Mark for AutoSL only if enabled, no SL provided, and not pending
            if self.config_service.getboolean('AutoSL', 'enable_auto_sl', fallback=False) and self.exec_sl is None and not is_pending: # Use service
                self.state_manager.mark_trade_for_auto_sl(ticket)
        else:
            logger.error(f"{self.log_prefix} Cannot store active trade info: StateManager not initialized.")


# --- Concrete Strategy: Distributed Limits ---

class DistributedLimitsStrategy(ExecutionStrategy):
    """Handles the execution of distributed pending limit orders across a range, with modular TP assignment."""
    def __init__(self, entry_price_raw, tp_assignment_config=None, signal_data=None, **kwargs):
        super().__init__(**kwargs)
        self.entry_price_raw = entry_price_raw # Keep the raw range string
        self.tp_assignment_config = tp_assignment_config or {"mode": "none"}
        self.signal_data = signal_data or {}

    async def execute(self):
        logger.info(f"{self.log_prefix} Applying distributed pending limit order strategy.")
        low_price, high_price = parse_entry_range(self.entry_price_raw, self.log_prefix)

        if low_price is None or high_price is None:
            logger.error(f"{self.log_prefix} Invalid entry range format '{self.entry_price_raw}'. Aborting distributed strategy.")
            status_message = f"❌ <b>Trade Execution FAILED</b> <code>[MsgID: {self.message_id}]</code>\n<b>Reason:</b> Invalid entry range format for distributed strategy: '{self.entry_price_raw}'"
            await self.telegram_sender.send_message(status_message, parse_mode='html')
            self.duplicate_checker.add_processed_id(self.message_id)
            return

        num_full_trades = int(self.lot_size // self.base_split_lot)
        remainder_lot_raw = self.lot_size % self.base_split_lot
        remainder_lot = round(remainder_lot_raw / self.lot_step) * self.lot_step if self.lot_step > 0 else remainder_lot_raw
        if remainder_lot < self.min_lot: remainder_lot = 0.0

        total_trades_to_open = num_full_trades + (1 if remainder_lot > 0 else 0)
        if total_trades_to_open == 0:
             logger.error(f"{self.log_prefix} Calculated zero trades to open for distributed strategy. Lot Size: {self.lot_size}, Base Split: {self.base_split_lot}")
             status_message = f"❌ <b>Trade Execution FAILED</b> <code>[MsgID: {self.message_id}]</code>\n<b>Reason:</b> Calculated zero trades for distributed strategy."
             await self.telegram_sender.send_message(status_message, parse_mode='html')
             self.duplicate_checker.add_processed_id(self.message_id)
             return

        logger.info(f"{self.log_prefix} Calculated Trades: {num_full_trades} x {self.base_split_lot}, Remainder: {remainder_lot}. Total: {total_trades_to_open}. Range: {low_price}-{high_price}")

        # --- Modular TP Assignment ---
        try:
            ConfigValidator.validate_tp_assignment_config(self.tp_assignment_config)
            tp_strategy = get_tp_assignment_strategy(self.tp_assignment_config)
            trade_data = {"num_trades": total_trades_to_open}
            assigned_tps = tp_strategy.assign_tps(trade_data, self.signal_data)
        except ConfigValidationError as e:
            logger.error(f"{self.log_prefix} TP assignment config error: {e}")
            assigned_tps = [None] * total_trades_to_open
        except Exception as e:
            logger.error(f"{self.log_prefix} TP assignment error: {e}")
            assigned_tps = [None] * total_trades_to_open

        # assigned_tps is the final mapping
        mapped_tps = assigned_tps

        # For reporting and state, store the full mapped TP list
        all_tps_for_state = mapped_tps.copy()

        # Check if Entry Range Split Mode is enabled
        split_mode_enabled = False
        try:
            split_mode_enabled = self.config_service.getboolean('Strategy', 'entry_range_split_mode_enabled', fallback=False)
        except:
            pass

        split_points = []
        if split_mode_enabled and total_trades_to_open >= 2:
            logger.info(f"{self.log_prefix} Entry Range Split Mode enabled. Using custom split points.")
            # Example: 2 trades -> upper and middle/lower
            split_points = []
            if total_trades_to_open == 2:
                split_points = [high_price, (high_price + low_price) / 2]
            else:
                # For more than 2, distribute: upper, middle(s), lower
                split_points.append(high_price)
                for i in range(1, total_trades_to_open -1):
                    ratio = i / (total_trades_to_open -1)
                    split_points.append(high_price - ratio * (high_price - low_price))
                split_points.append(low_price)
        else:
            # Default distributed mode
            split_points = []
            if total_trades_to_open == 1:
                split_points = [high_price if self.action == "BUY" else low_price]
            else:
                for i in range(total_trades_to_open):
                    if self.action == "BUY":
                        split_points.append(high_price - i * (high_price - low_price) / (total_trades_to_open -1))
                    else:
                        split_points.append(low_price + i * (high_price - low_price) / (total_trades_to_open -1))

        executed_tickets_info = []
        failed_trades = 0
        successful_trades = 0
        last_error = ""
        limit_order_type = mt5.ORDER_TYPE_BUY_LIMIT if self.action == "BUY" else mt5.ORDER_TYPE_SELL_LIMIT

        # --- Place Pending Limit Orders ---
        for i in range(total_trades_to_open):
            current_vol = self.base_split_lot if i < num_full_trades else remainder_lot
            current_entry_price = round(split_points[i], self.digits)

            # Fetch current market prices
            try:
                tick = self.mt5_fetcher.get_symbol_tick(self.trade_symbol)
                if tick:
                    current_ask = tick.ask
                    current_bid = tick.bid
                    # Determine entry zone bounds
                    zone_low = min(split_points)
                    zone_high = max(split_points)
                    # Check if current Ask is inside the entry zone
                    if self.action == "BUY" and zone_low < current_ask < zone_high:
                        logger.warning(f"{self.log_prefix} Current Ask {current_ask} is INSIDE entry zone ({zone_low}-{zone_high}). Skipping pending order placement to avoid invalid prices.")
                        continue  # Skip this order
                    if self.action == "SELL" and zone_low < current_bid < zone_high:
                        logger.warning(f"{self.log_prefix} Current Bid {current_bid} is INSIDE entry zone ({zone_low}-{zone_high}). Skipping pending order placement to avoid invalid prices.")
                        continue  # Skip this order
                    # Else, determine order type dynamically
                    if self.action == "BUY":
                        if current_entry_price >= current_ask:
                            limit_order_type = mt5.ORDER_TYPE_BUY_STOP
                            logger.info(f"{self.log_prefix} Switching to BUY STOP for entry {current_entry_price} >= Ask {current_ask}")
                        else:
                            limit_order_type = mt5.ORDER_TYPE_BUY_LIMIT
                    elif self.action == "SELL":
                        if current_entry_price <= current_bid:
                            limit_order_type = mt5.ORDER_TYPE_SELL_STOP
                            logger.info(f"{self.log_prefix} Switching to SELL STOP for entry {current_entry_price} <= Bid {current_bid}")
                        else:
                            limit_order_type = mt5.ORDER_TYPE_SELL_LIMIT
                else:
                    logger.warning(f"{self.log_prefix} Could not get tick data to determine order type dynamically. Using default limit order type.")
            except Exception as e:
                logger.error(f"{self.log_prefix} Error determining order type dynamically: {e}")

            # Dynamically determine order type based on current market
            try:
                tick = self.mt5_fetcher.get_symbol_tick(self.trade_symbol)
                if tick:
                    current_ask = tick.ask
                    current_bid = tick.bid
                    if self.action == "BUY":
                        if current_entry_price >= current_ask:
                            limit_order_type = mt5.ORDER_TYPE_BUY_STOP
                            logger.info(f"{self.log_prefix} Switching to BUY STOP for entry {current_entry_price} >= Ask {current_ask}")
                        else:
                            limit_order_type = mt5.ORDER_TYPE_BUY_LIMIT
                    elif self.action == "SELL":
                        if current_entry_price <= current_bid:
                            limit_order_type = mt5.ORDER_TYPE_SELL_STOP
                            logger.info(f"{self.log_prefix} Switching to SELL STOP for entry {current_entry_price} <= Bid {current_bid}")
                        else:
                            limit_order_type = mt5.ORDER_TYPE_SELL_LIMIT
                else:
                    logger.warning(f"{self.log_prefix} Could not get tick data to determine order type dynamically. Using default limit order type.")
            except Exception as e:
                logger.error(f"{self.log_prefix} Error determining order type dynamically: {e}")

            # --- Adjust Entry Price for Spread ---
            adjusted_entry_price = current_entry_price  # Default to original
            try:
                tick = self.mt5_fetcher.get_symbol_tick(self.trade_symbol)
                logger.debug(f"{self.log_prefix} [DistLimit Entry Adjust] Fetched tick: {tick}") # DEBUG LOG
                if tick and tick.ask > 0 and tick.bid > 0: # Check for valid tick prices
                    spread = round(tick.ask - tick.bid, self.digits)
                    logger.debug(f"{self.log_prefix} [DistLimit Entry Adjust] Calculated spread: {spread}") # DEBUG LOG
                    # Use trade_calculator to adjust entry price with spread + offset
                    try:
                        adjusted_entry_price = self.trade_calculator.calculate_adjusted_entry_price(
                            symbol=self.trade_symbol,
                            original_price=current_entry_price,
                            direction=self.action,
                            spread=spread # Ensure spread is passed
                        )
                        logger.info(f"{self.log_prefix} Adjusted entry for spread+offset: Original={current_entry_price}, Spread={spread} -> Adjusted={adjusted_entry_price}")
                    except Exception as e:
                        logger.error(f"{self.log_prefix} Error in trade_calculator.calculate_adjusted_entry_price: {e}")
                        adjusted_entry_price = current_entry_price
                else:
                    logger.warning(f"{self.log_prefix} Could not get valid tick data to adjust entry price for spread and offset. Tick: {tick}. Using original price: {current_entry_price}")
                    adjusted_entry_price = current_entry_price # Ensure it's assigned
            except Exception as e:
                logger.error(f"{self.log_prefix} Error adjusting entry price for spread and offset: {e}")
                adjusted_entry_price = current_entry_price # Ensure it's assigned
            # --- End Entry Price Adjustment ---

            # Determine TP using the mapped TPs from the assignment strategy
            current_exec_tp = mapped_tps[i] if i < len(mapped_tps) else None

            # Removed obsolete Partial TP-Free Mode logic

            trade_comment = f"TB SigID {self.message_id} Dist {i+1}/{total_trades_to_open}"

            logger.info(f"{self.log_prefix} Placing pending limit order {i+1}/{total_trades_to_open}: Type={limit_order_type}, Vol={current_vol}, Entry={adjusted_entry_price} (Orig: {current_entry_price}), TP={current_exec_tp}")

            trade_result_tuple = self.mt5_executor.execute_trade(
                action=self.action, symbol=self.trade_symbol, order_type=limit_order_type,
                volume=current_vol, price=adjusted_entry_price, sl=self.exec_sl, tp=current_exec_tp,
                comment=trade_comment
            )
            trade_result, _ = trade_result_tuple if trade_result_tuple else (None, None)

            if trade_result and trade_result.retcode == mt5.TRADE_RETCODE_DONE:
                ticket = trade_result.order
                executed_tickets_info.append({'ticket': ticket, 'vol': current_vol, 'tp': current_exec_tp, 'entry': adjusted_entry_price})
                successful_trades += 1
                logger.info(f"{self.log_prefix} Pending limit order {i+1} placed successfully. Ticket: {ticket}")
                self._store_trade_info(
                    ticket=ticket, entry_price=adjusted_entry_price, volume=current_vol,
                    assigned_tp=current_exec_tp, is_pending=True,
                    sequence_info=f"Dist {i+1}/{total_trades_to_open}",
                    take_profits_list_ref=all_tps_for_state # Pass the full list for multi-trade
                )
            else:
                failed_trades += 1
                error_comment = getattr(trade_result, 'comment', 'Unknown Error') if trade_result else 'None Result'
                last_error = f"{error_comment} (Code: {getattr(trade_result, 'retcode', 'N/A')})"
                logger.error(f"{self.log_prefix} Pending limit order {i+1} FAILED. Reason: {last_error}. Result: {trade_result_tuple}")

        # --- Report Distributed Limit Order Result ---
        if successful_trades > 0:
            status_title = f"✅ Distributed Limits Placed ({successful_trades}/{total_trades_to_open} OK)" if failed_trades == 0 else f"⚠️ Distributed Limits Partially Placed ({successful_trades}/{total_trades_to_open} OK)"
            status_message = f"{status_title} <code>[MsgID: {self.message_id}]</code>\n"
            status_message += f"<b>Symbol:</b> <code>{self.trade_symbol}</code> | <b>Total Vol:</b> <code>{self.lot_size}</code>\n"
            status_message += f"<b>Range:</b> <code>{low_price}-{high_price}</code>\n"
            # Calculate adjusted SL for display
            adjusted_sl_display = '<i>None</i>'
            if self.exec_sl:
                try:
                    adjusted_sl_val = self.mt5_executor._adjust_sl_for_spread_offset(
                        self.exec_sl, limit_order_type, self.trade_symbol
                    )
                    adjusted_sl_display = f"<code>{adjusted_sl_val}</code>"
                except Exception as e:
                    logger.error(f"{self.log_prefix} Error calculating adjusted SL for display: {e}")
                    adjusted_sl_display = f"<code>{self.exec_sl}</code> (Error adjusting)"

            status_message += f"<b>Stop Loss:</b> {adjusted_sl_display}\n"
            status_message += "<b>Pending Orders Placed:</b>\n"
            for idx, trade in enumerate(executed_tickets_info):
                status_message += f"  <code>{idx+1}. Ticket: {trade['ticket']}, Vol: {trade['vol']}, Entry: {trade['entry']}, TP: {trade['tp']}</code>\n"
            if failed_trades > 0:
                status_message += f"<b>Failures:</b> {failed_trades} order(s) failed. Last Error: {last_error}\n"
            await self.telegram_sender.send_message(status_message, parse_mode='html')
            if self.debug_channel_id: await self.telegram_sender.send_message(f"{self.log_prefix} Distributed limit order summary:\n{status_message}", target_chat_id=self.debug_channel_id, parse_mode='html')
            self.duplicate_checker.add_processed_id(self.message_id)
        else:
            status_message = f"❌ <b>Distributed Limits FAILED</b> <code>[MsgID: {self.message_id}]</code>\n<b>Reason:</b> All {total_trades_to_open} pending orders failed. Last Error: {last_error}"
            logger.error(f"{self.log_prefix} All {total_trades_to_open} distributed pending orders failed placement.")
            self.duplicate_checker.add_processed_id(self.message_id)
            await self.telegram_sender.send_message(status_message, parse_mode='html')
            if self.debug_channel_id: await self.telegram_sender.send_message(f"❌ {self.log_prefix} All distributed pending orders failed.\n{status_message}", target_chat_id=self.debug_channel_id, parse_mode='html')


# --- Concrete Strategy: Multi Market/Stop Orders ---

class MultiMarketStopStrategy(ExecutionStrategy):
    """Handles the execution of multiple market/stop orders for sequential TPs, with modular TP assignment."""
    def __init__(self, determined_order_type, exec_price, tp_assignment_config=None, signal_data=None, **kwargs):
        super().__init__(**kwargs)
        self.determined_order_type = determined_order_type
        self.exec_price = exec_price # Entry price for stop orders, None for market
        self.tp_assignment_config = tp_assignment_config or {"mode": "none"}
        self.signal_data = signal_data or {}

    async def execute(self):
        logger.info(f"{self.log_prefix} Applying multi-trade sequential TP strategy (Market/Stop). Base Split Lot: {self.base_split_lot}")
        num_full_trades = int(self.lot_size // self.base_split_lot)
        remainder_lot_raw = self.lot_size % self.base_split_lot
        remainder_lot = round(remainder_lot_raw / self.lot_step) * self.lot_step if self.lot_step > 0 else remainder_lot_raw
        if remainder_lot < self.min_lot: remainder_lot = 0.0

        total_trades_to_open = num_full_trades + (1 if remainder_lot > 0 else 0)
        logger.info(f"{self.log_prefix} Calculated Trades: {num_full_trades} x {self.base_split_lot} lots, Remainder: {remainder_lot} lots. Total: {total_trades_to_open}")

        # --- Modular TP Assignment ---
        try:
            ConfigValidator.validate_tp_assignment_config(self.tp_assignment_config)
            tp_strategy = get_tp_assignment_strategy(self.tp_assignment_config)
            trade_data = {"num_trades": total_trades_to_open}
            assigned_tps = tp_strategy.assign_tps(trade_data, self.signal_data)
        except ConfigValidationError as e:
            logger.error(f"{self.log_prefix} TP assignment config error: {e}")
            assigned_tps = [None] * total_trades_to_open
        except Exception as e:
            logger.error(f"{self.log_prefix} TP assignment error: {e}")
            assigned_tps = [None] * total_trades_to_open

        # No sequence mapping; assigned_tps is the final mapping
        mapped_tps = assigned_tps

        all_tps_for_state = mapped_tps.copy()

        # Removed obsolete Partial TP-Free Mode check

        executed_tickets_info = []
        failed_trades = 0
        successful_trades = 0
        last_error = ""

        # --- Execute Full Lot Trades ---
        for i in range(num_full_trades):
            current_exec_tp = mapped_tps[i] if i < len(mapped_tps) else None

            # Removed obsolete Partial TP-Free Mode logic
            trade_comment = f"TB SigID {self.message_id} Seq {i+1}/{total_trades_to_open}"

            logger.info(f"{self.log_prefix} Executing trade {i+1}/{total_trades_to_open}: Vol={self.base_split_lot}, TP={current_exec_tp}")
            trade_result_tuple = self.mt5_executor.execute_trade(
                action=self.action, symbol=self.trade_symbol, order_type=self.determined_order_type,
                volume=self.base_split_lot, price=self.exec_price, sl=self.exec_sl, tp=current_exec_tp,
                comment=trade_comment
            )
            trade_result, actual_exec_price = trade_result_tuple if trade_result_tuple else (None, None)

            if trade_result and trade_result.retcode == mt5.TRADE_RETCODE_DONE:
                ticket = trade_result.order
                executed_tickets_info.append({'ticket': ticket, 'vol': self.base_split_lot, 'tp': current_exec_tp})
                successful_trades += 1
                logger.info(f"{self.log_prefix} Sub-trade {i+1} executed successfully. Ticket: {ticket}")
                final_entry_price = actual_exec_price if actual_exec_price is not None else self.exec_price
                self._store_trade_info(
                    ticket=ticket, entry_price=final_entry_price, volume=self.base_split_lot,
                    assigned_tp=current_exec_tp, is_pending=False,
                    sequence_info=f"Seq {i+1}/{total_trades_to_open}",
                    take_profits_list_ref=all_tps_for_state
                )
            else:
                failed_trades += 1
                error_comment = getattr(trade_result, 'comment', 'Unknown Error') if trade_result else 'None Result'
                last_error = f"{error_comment} (Code: {getattr(trade_result, 'retcode', 'N/A')})"
                logger.error(f"{self.log_prefix} Sub-trade {i+1} FAILED. Reason: {last_error}. Result: {trade_result_tuple}")

        # --- Execute Remainder Lot Trade ---
        if remainder_lot > 0:
            current_exec_tp = mapped_tps[-1] if mapped_tps else None
            # Removed obsolete Partial TP-Free Mode logic
            trade_comment = f"TB SigID {self.message_id} Seq {total_trades_to_open}/{total_trades_to_open} (Rem)"
            logger.info(f"{self.log_prefix} Executing remainder trade {total_trades_to_open}/{total_trades_to_open}: Vol={remainder_lot}, TP={current_exec_tp}")
            trade_result_tuple = self.mt5_executor.execute_trade(
                action=self.action, symbol=self.trade_symbol, order_type=self.determined_order_type,
                volume=remainder_lot, price=self.exec_price, sl=self.exec_sl, tp=current_exec_tp,
                comment=trade_comment
            )
            trade_result, actual_exec_price = trade_result_tuple if trade_result_tuple else (None, None)

            if trade_result and trade_result.retcode == mt5.TRADE_RETCODE_DONE:
                ticket = trade_result.order
                executed_tickets_info.append({'ticket': ticket, 'vol': remainder_lot, 'tp': current_exec_tp})
                successful_trades += 1
                logger.info(f"{self.log_prefix} Remainder sub-trade executed successfully. Ticket: {ticket}")
                final_entry_price = actual_exec_price if actual_exec_price is not None else self.exec_price
                self._store_trade_info(
                    ticket=ticket, entry_price=final_entry_price, volume=remainder_lot,
                    assigned_tp=current_exec_tp, is_pending=False,
                    sequence_info=f"Seq {total_trades_to_open}/{total_trades_to_open} (Rem)",
                    take_profits_list_ref=all_tps_for_state
                )
            else:
                failed_trades += 1
                error_comment = getattr(trade_result, 'comment', 'Unknown Error') if trade_result else 'None Result'
                last_error = f"{error_comment} (Code: {getattr(trade_result, 'retcode', 'N/A')})"
                logger.error(f"{self.log_prefix} Remainder sub-trade FAILED. Reason: {last_error}. Result: {trade_result_tuple}")

        # --- Report Multi-Trade Market/Stop Result ---
        if successful_trades > 0:
            status_title = f"✅ Multi-Trade Executed ({successful_trades}/{total_trades_to_open} OK)" if failed_trades == 0 else f"⚠️ Multi-Trade Partially Executed ({successful_trades}/{total_trades_to_open} OK)"
            status_message = f"{status_title} <code>[MsgID: {self.message_id}]</code>\n"
            status_message += f"<b>Symbol:</b> <code>{self.trade_symbol}</code> | <b>Total Vol:</b> <code>{self.lot_size}</code>\n"
            status_message += f"<b>SL:</b> {'<code>'+str(self.exec_sl)+'</code>' if self.exec_sl else '<i>None</i>'}\n"
            status_message += "<b>Trades Opened:</b>\n"
            for idx, trade in enumerate(executed_tickets_info):
                status_message += f"  <code>{idx+1}. Ticket: {trade['ticket']}, Vol: {trade['vol']}, TP: {trade['tp']}</code>\n"
            if failed_trades > 0:
                status_message += f"<b>Failures:</b> {failed_trades} trade(s) failed. Last Error: {last_error}\n"
            await self.telegram_sender.send_message(status_message, parse_mode='html')
            if self.debug_channel_id: await self.telegram_sender.send_message(f"{self.log_prefix} Multi-trade execution summary:\n{status_message}", target_chat_id=self.debug_channel_id, parse_mode='html')
            self.duplicate_checker.add_processed_id(self.message_id)
        else:
            status_message = f"❌ <b>Multi-Trade Execution FAILED</b> <code>[MsgID: {self.message_id}]</code>\n<b>Reason:</b> All {total_trades_to_open} sub-trades failed. Last Error: {last_error}"
            logger.error(f"{self.log_prefix} All {total_trades_to_open} sub-trades failed execution.")
            self.duplicate_checker.add_processed_id(self.message_id)
            await self.telegram_sender.send_message(status_message, parse_mode='html')
            if self.debug_channel_id: await self.telegram_sender.send_message(f"❌ {self.log_prefix} All sub-trades failed.\n{status_message}", target_chat_id=self.debug_channel_id, parse_mode='html')


# --- Concrete Strategy: Single Trade ---
class SingleTradeStrategy(ExecutionStrategy):
    """Handles the execution of a single trade order."""
    def __init__(self, determined_order_type, exec_price, exec_tp, take_profits_list, auto_tp_applied, tp_assignment_config=None, signal_data=None, **kwargs):
        super().__init__(**kwargs)
        self.determined_order_type = determined_order_type
        self.exec_price = exec_price # Entry price for pending, None for market
        self.exec_tp = exec_tp # Single TP for this order (legacy, will be replaced by modular logic)
        self.take_profits_list = take_profits_list # Original list for reporting (legacy)
        self.auto_tp_applied = auto_tp_applied
        self.tp_assignment_config = tp_assignment_config or {"mode": "none"}
        self.signal_data = signal_data or {}

    async def execute(self):
        logger.info(f"{self.log_prefix} Executing as single trade. Vol={self.lot_size}")

        # --- Modular TP Assignment ---
        try:
            ConfigValidator.validate_tp_assignment_config(self.tp_assignment_config)
            tp_strategy = get_tp_assignment_strategy(self.tp_assignment_config)
            trade_data = {"num_trades": 1}
            assigned_tps = tp_strategy.assign_tps(trade_data, self.signal_data)
        except ConfigValidationError as e:
            logger.error(f"{self.log_prefix} TP assignment config error: {e}")
            assigned_tps = [None]
        except Exception as e:
            logger.error(f"{self.log_prefix} TP assignment error: {e}")
            assigned_tps = [None]

        exec_tp = assigned_tps[0] if assigned_tps else None
        all_tps_for_state = assigned_tps.copy()

        # --- Adjust Entry Price for Spread and Offset (Pending Orders Only) ---
        price_to_execute = self.exec_price # Use original price if adjustment fails
        is_pending = self.determined_order_type not in [mt5.ORDER_TYPE_BUY, mt5.ORDER_TYPE_SELL]
        if self.exec_price is not None:
            try:
                tick = self.mt5_fetcher.get_symbol_tick(self.trade_symbol)
                logger.debug(f"{self.log_prefix} [SingleTrade Entry Adjust] Fetched tick: {tick}") # DEBUG LOG
                if tick and tick.ask > 0 and tick.bid > 0: # Check for valid tick prices
                    spread = round(tick.ask - tick.bid, self.digits)
                    logger.debug(f"{self.log_prefix} [SingleTrade Entry Adjust] Calculated spread: {spread}") # DEBUG LOG
                    # Use trade_calculator to adjust entry price with spread + offset
                    # For test compatibility, support both positional and keyword args
                    adjusted_entry = self.trade_calculator.calculate_adjusted_entry_price(
                        symbol=self.trade_symbol, # Pass symbol
                        original_price=self.exec_price,
                        direction=self.action,
                        spread=spread
                    )
                    if adjusted_entry is not None:
                        price_to_execute = adjusted_entry # Use adjusted price if calculation succeeds
                        logger.info(f"{self.log_prefix} Adjusted entry for spread/offset: Original={self.exec_price}, Spread={spread} -> Adjusted={price_to_execute}")
                    else:
                        logger.warning(f"{self.log_prefix} Failed to calculate adjusted entry price. Using original price: {self.exec_price}")
                else:
                    logger.warning(f"{self.log_prefix} Could not get valid tick data to adjust entry price. Using original price: {self.exec_price}. Tick: {tick}")
            except Exception as e:
                logger.error(f"{self.log_prefix} Error adjusting entry price: {e}. Using original price: {self.exec_price}")
        # --- End Entry Price Adjustment ---

        trade_result_tuple = self.mt5_executor.execute_trade(
            action=self.action, symbol=self.trade_symbol, order_type=self.determined_order_type,
            volume=self.lot_size, price=price_to_execute, sl=self.exec_sl, tp=exec_tp,
            comment=f"TB SigID {self.message_id}"
        )
        trade_result, actual_exec_price = trade_result_tuple if trade_result_tuple else (None, None)

        if trade_result and trade_result.retcode == mt5.TRADE_RETCODE_DONE:
            ticket = trade_result.order
            order_type_str_map = { mt5.ORDER_TYPE_BUY: "Market BUY", mt5.ORDER_TYPE_SELL: "Market SELL", mt5.ORDER_TYPE_BUY_LIMIT: "BUY LIMIT", mt5.ORDER_TYPE_SELL_LIMIT: "SELL LIMIT", mt5.ORDER_TYPE_BUY_STOP: "BUY STOP", mt5.ORDER_TYPE_SELL_STOP: "SELL STOP" }
            order_type_str = order_type_str_map.get(self.determined_order_type, f"Type {self.determined_order_type}")
            # Use adjusted price for pending, actual exec price for market
            final_entry_price = price_to_execute if is_pending else actual_exec_price
            is_pending = self.determined_order_type not in [mt5.ORDER_TYPE_BUY, mt5.ORDER_TYPE_SELL]

            entry_str = f"<code>@{final_entry_price}</code>" if final_entry_price is not None else "<code>Market</code>"
            sl_str = f"<code>{self.exec_sl}</code>" if self.exec_sl is not None else "<i>None</i>"
            tp_list_str = ', '.join([f"<code>{tp}</code>" if tp != "N/A" else "<i>N/A</i>" for tp in all_tps_for_state])
            tp_str = f"<code>{exec_tp}</code>" if exec_tp is not None else "<i>None</i>"
            auto_tp_label = " (Auto)" if self.auto_tp_applied else ""
            symbol_str = f"<code>{self.trade_symbol.replace('&', '&amp;').replace('<', '<').replace('>', '>')}</code>"
            lot_str = f"<code>{self.lot_size}</code>"
            ticket_str = f"<code>{ticket}</code>"
            type_str = f"<code>{order_type_str.replace('&', '&amp;').replace('<', '<').replace('>', '>')}</code>"

            status_message = f"""✅ <b>Trade Executed</b> <code>[MsgID: {self.message_id}]</code>

<b>Ticket:</b> {ticket_str}
<b>Type:</b> {type_str}
<b>Symbol:</b> {symbol_str}
<b>Volume:</b> {lot_str}
<b>Entry:</b> {entry_str}
<b>SL:</b> {sl_str} | <b>TP(s):</b> {tp_list_str} (Initial: {tp_str}{auto_tp_label})"""
            logger.info(f"{self.log_prefix} Trade executed successfully. Ticket: {ticket}")
            sent_msg = await self.telegram_sender.send_message(status_message, parse_mode='html')

            if self.debug_channel_id:
                debug_msg_exec_success = f"✅ {self.log_prefix} Trade Executed Successfully.\n<b>Ticket:</b> <code>{ticket}</code>\n<b>Type:</b> <code>{order_type_str}</code>\n<b>Symbol:</b> <code>{self.trade_symbol}</code>\n<b>Volume:</b> <code>{self.lot_size}</code>\n<b>Entry:</b> {entry_str}\n<b>SL:</b> {sl_str}\n<b>TP(s):</b> {tp_list_str} (Initial: {tp_str}{auto_tp_label})"
                await self.telegram_sender.send_message(debug_msg_exec_success, target_chat_id=self.debug_channel_id, parse_mode='html')

            # Pass the modular all_tps_for_state for reference in single trade case
            self._store_trade_info(
                ticket=ticket, entry_price=final_entry_price, volume=self.lot_size,
                assigned_tp=exec_tp, is_pending=is_pending,
                take_profits_list_ref=all_tps_for_state
            )



# --- Helper function to parse entry range ---
def parse_entry_range(range_str, log_prefix):
    """Parses an entry range string like '1.1000-1.1050'."""
    try:
        # Remove potential "Zone" prefix and whitespace
        range_str = range_str.lower().replace("zone", "").strip()
        low_str, high_str = range_str.split('-')
        low = float(low_str)
        high = float(high_str)
        if low > high: low, high = high, low # Ensure low <= high
        return low, high
    except Exception as e:
        logger.warning(f"{log_prefix} Failed to parse entry range '{range_str}': {e}")
        return None, None
