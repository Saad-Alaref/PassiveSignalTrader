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

logger = logging.getLogger('TradeBot')

# --- Base Strategy Class ---
class ExecutionStrategy(ABC):
    """Abstract base class for different trade execution strategies."""
    def __init__(self, action, trade_symbol, lot_size, exec_sl, numeric_tps,
                 message_id, config_service_instance, mt5_fetcher: MT5DataFetcher, mt5_executor: MT5Executor, # Use service instance
                 state_manager: StateManager, telegram_sender: TelegramSender,
                 duplicate_checker: DuplicateChecker, log_prefix: str):
        self.action = action
        self.trade_symbol = trade_symbol
        self.lot_size = lot_size
        self.exec_sl = exec_sl
        self.numeric_tps = numeric_tps
        self.message_id = message_id
        self.config_service = config_service_instance # Store service instance
        self.mt5_fetcher = mt5_fetcher
        self.mt5_executor = mt5_executor
        self.state_manager = state_manager
        self.telegram_sender = telegram_sender
        self.duplicate_checker = duplicate_checker
        self.log_prefix = log_prefix
        self.debug_channel_id = getattr(telegram_sender, 'debug_target_channel_id', None)
        self.tp_strategy = self.config_service.get('Strategy', 'tp_execution_strategy', fallback='first_tp_full_close').lower() # Use service

        # Common initializations
        self.symbol_info = self.mt5_fetcher.get_symbol_info(self.trade_symbol)
        self.min_lot = self.symbol_info.volume_min if self.symbol_info else 0.01
        self.lot_step = self.symbol_info.volume_step if self.symbol_info else 0.01
        self.digits = self.symbol_info.digits if self.symbol_info else 5
        self.base_split_lot = max(self.min_lot, 0.01)


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
            'original_msg_id': self.message_id,
            'entry_price': entry_price,
            'initial_sl': self.exec_sl,
            'original_volume': volume, # Volume of this specific trade/order
            # For multi-trade strategies, store only the assigned TP.
            # For single trade, store the original list passed for reference/reporting.
            'all_tps': take_profits_list_ref if take_profits_list_ref is not None else ([assigned_tp] if assigned_tp is not None else []),
            'tp_strategy': self.tp_strategy,
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
    """Handles the execution of distributed pending limit orders across a range."""
    def __init__(self, entry_price_raw, **kwargs):
        super().__init__(**kwargs)
        self.entry_price_raw = entry_price_raw # Keep the raw range string

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

        # Calculate price step
        price_step = 0.0
        current_entry_price_single = None # For the case total_trades_to_open == 1
        if total_trades_to_open == 1:
            logger.info(f"{self.log_prefix} Only one trade to open, placing at the edge based on action.")
            current_entry_price_single = high_price if self.action == "BUY" else low_price
            price_step = 0.0
        elif total_trades_to_open > 1:
            if high_price == low_price:
                logger.warning(f"{self.log_prefix} Entry range has zero width ({low_price}-{high_price}). Placing all orders at {low_price}.")
                price_step = 0.0
            else:
                price_step = (high_price - low_price) / (total_trades_to_open - 1)
                logger.info(f"{self.log_prefix} Calculated price step for {total_trades_to_open} trades: {price_step}")
        elif total_trades_to_open > 1:
            if high_price == low_price:
                 logger.warning(f"{self.log_prefix} Entry range has zero width ({low_price}-{high_price}). Placing all orders at {low_price}.")
                 price_step = 0.0
            else:
                 price_step = (high_price - low_price) / (total_trades_to_open - 1)
                 logger.info(f"{self.log_prefix} Calculated price step for {total_trades_to_open} trades: {price_step}")

        executed_tickets_info = []
        failed_trades = 0
        successful_trades = 0
        last_error = ""
        limit_order_type = mt5.ORDER_TYPE_BUY_LIMIT if self.action == "BUY" else mt5.ORDER_TYPE_SELL_LIMIT

        # --- Place Pending Limit Orders ---
        for i in range(total_trades_to_open):
            current_vol = self.base_split_lot if i < num_full_trades else remainder_lot
            # Calculate entry price for this order
            if total_trades_to_open == 1:
                 current_entry_price = current_entry_price_single
            elif limit_order_type == mt5.ORDER_TYPE_BUY_LIMIT:
                current_entry_price = high_price - (i * price_step)
            else: # SELL_LIMIT
                current_entry_price = low_price + (i * price_step)

            current_entry_price = round(current_entry_price, self.digits)

            # Determine TP
            tp_index = min(i, len(self.numeric_tps) - 1)
            current_exec_tp = self.numeric_tps[tp_index]
            trade_comment = f"TB SigID {self.message_id} Dist {i+1}/{total_trades_to_open}"

            logger.info(f"{self.log_prefix} Placing pending limit order {i+1}/{total_trades_to_open}: Type={limit_order_type}, Vol={current_vol}, Entry={current_entry_price}, TP={current_exec_tp}")

            trade_result_tuple = self.mt5_executor.execute_trade(
                action=self.action, symbol=self.trade_symbol, order_type=limit_order_type,
                volume=current_vol, price=current_entry_price, sl=self.exec_sl, tp=current_exec_tp,
                comment=trade_comment
            )
            trade_result, _ = trade_result_tuple if trade_result_tuple else (None, None)

            if trade_result and trade_result.retcode == mt5.TRADE_RETCODE_DONE:
                ticket = trade_result.order
                executed_tickets_info.append({'ticket': ticket, 'vol': current_vol, 'tp': current_exec_tp, 'entry': current_entry_price})
                successful_trades += 1
                logger.info(f"{self.log_prefix} Pending limit order {i+1} placed successfully. Ticket: {ticket}")
                self._store_trade_info(
                    ticket=ticket, entry_price=current_entry_price, volume=current_vol,
                    assigned_tp=current_exec_tp, is_pending=True,
                    sequence_info=f"Dist {i+1}/{total_trades_to_open}"
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
            status_message += f"<b>SL:</b> {'<code>'+str(self.exec_sl)+'</code>' if self.exec_sl else '<i>None</i>'}\n"
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
    """Handles the execution of multiple market/stop orders for sequential TPs."""
    def __init__(self, determined_order_type, exec_price, **kwargs):
        super().__init__(**kwargs)
        self.determined_order_type = determined_order_type
        self.exec_price = exec_price # Entry price for stop orders, None for market

    async def execute(self):
        logger.info(f"{self.log_prefix} Applying multi-trade sequential TP strategy (Market/Stop). Base Split Lot: {self.base_split_lot}")
        num_full_trades = int(self.lot_size // self.base_split_lot)
        remainder_lot_raw = self.lot_size % self.base_split_lot
        remainder_lot = round(remainder_lot_raw / self.lot_step) * self.lot_step if self.lot_step > 0 else remainder_lot_raw
        if remainder_lot < self.min_lot: remainder_lot = 0.0

        total_trades_to_open = num_full_trades + (1 if remainder_lot > 0 else 0)
        logger.info(f"{self.log_prefix} Calculated Trades: {num_full_trades} x {self.base_split_lot} lots, Remainder: {remainder_lot} lots. Total: {total_trades_to_open}")

        executed_tickets_info = []
        failed_trades = 0
        successful_trades = 0
        last_error = ""

        # --- Execute Full Lot Trades ---
        for i in range(num_full_trades):
            tp_index = min(i, len(self.numeric_tps) - 1)
            current_exec_tp = self.numeric_tps[tp_index]
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
                    assigned_tp=current_exec_tp, is_pending=False, # Market/Stop orders are not pending once executed
                    sequence_info=f"Seq {i+1}/{total_trades_to_open}"
                )
            else:
                failed_trades += 1
                error_comment = getattr(trade_result, 'comment', 'Unknown Error') if trade_result else 'None Result'
                last_error = f"{error_comment} (Code: {getattr(trade_result, 'retcode', 'N/A')})"
                logger.error(f"{self.log_prefix} Sub-trade {i+1} FAILED. Reason: {last_error}. Result: {trade_result_tuple}")

        # --- Execute Remainder Lot Trade ---
        if remainder_lot > 0:
            current_exec_tp = self.numeric_tps[-1]
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
                    sequence_info=f"Seq {total_trades_to_open}/{total_trades_to_open} (Rem)"
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
    def __init__(self, determined_order_type, exec_price, exec_tp, take_profits_list, auto_tp_applied, **kwargs):
        super().__init__(**kwargs)
        self.determined_order_type = determined_order_type
        self.exec_price = exec_price # Entry price for pending, None for market
        self.exec_tp = exec_tp # Single TP for this order
        self.take_profits_list = take_profits_list # Original list for reporting
        self.auto_tp_applied = auto_tp_applied

    async def execute(self):
        logger.info(f"{self.log_prefix} Executing as single trade. Vol={self.lot_size}, TP={self.exec_tp}")

        trade_result_tuple = self.mt5_executor.execute_trade(
            action=self.action, symbol=self.trade_symbol, order_type=self.determined_order_type,
            volume=self.lot_size, price=self.exec_price, sl=self.exec_sl, tp=self.exec_tp,
            comment=f"TB SigID {self.message_id}"
        )
        trade_result, actual_exec_price = trade_result_tuple if trade_result_tuple else (None, None)

        if trade_result and trade_result.retcode == mt5.TRADE_RETCODE_DONE:
            ticket = trade_result.order
            order_type_str_map = { mt5.ORDER_TYPE_BUY: "Market BUY", mt5.ORDER_TYPE_SELL: "Market SELL", mt5.ORDER_TYPE_BUY_LIMIT: "BUY LIMIT", mt5.ORDER_TYPE_SELL_LIMIT: "SELL LIMIT", mt5.ORDER_TYPE_BUY_STOP: "BUY STOP", mt5.ORDER_TYPE_SELL_STOP: "SELL STOP" }
            order_type_str = order_type_str_map.get(self.determined_order_type, f"Type {self.determined_order_type}")
            final_entry_price = actual_exec_price if actual_exec_price is not None else self.exec_price
            is_pending = self.determined_order_type not in [mt5.ORDER_TYPE_BUY, mt5.ORDER_TYPE_SELL]

            entry_str = f"<code>@{final_entry_price}</code>" if final_entry_price is not None else "<code>Market</code>"
            sl_str = f"<code>{self.exec_sl}</code>" if self.exec_sl is not None else "<i>None</i>"
            tp_list_str = ', '.join([f"<code>{tp}</code>" if tp != "N/A" else "<i>N/A</i>" for tp in self.take_profits_list])
            tp_str = f"<code>{self.exec_tp}</code>" if self.exec_tp is not None else "<i>None</i>"
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

            # Pass the original take_profits_list for reference in single trade case
            self._store_trade_info(
                ticket=ticket, entry_price=final_entry_price, volume=self.lot_size,
                assigned_tp=self.exec_tp, is_pending=is_pending,
                auto_tp_applied=self.auto_tp_applied,
                take_profits_list_ref=self.take_profits_list
            )
            # Store bot's execution message ID
            if sent_msg:
                try:
                    trade_obj = self.state_manager.get_trade_by_ticket(ticket)
                    if trade_obj:
                        trade_obj.bot_msg_id = sent_msg.id
                except:
                    pass
            self.duplicate_checker.add_processed_id(self.message_id)

        else: # Single Execution failed
            error_comment = getattr(trade_result, 'comment', 'Unknown Error') if trade_result else 'None Result (Check Logs)'
            error_code = getattr(trade_result, 'retcode', 'N/A') if trade_result else 'N/A'
            safe_comment = str(error_comment).replace('&', '&amp;').replace('<', '<').replace('>', '>')
            safe_code = str(error_code).replace('&', '&amp;').replace('<', '<').replace('>', '>')
            status_message = f"❌ <b>Trade Execution FAILED</b> <code>[MsgID: {self.message_id}]</code>\n<b>Reason:</b> {safe_comment} (Code: <code>{safe_code}</code>)"
            logger.error(f"{self.log_prefix} Trade execution failed. Full Result: {trade_result_tuple}")
            self.duplicate_checker.add_processed_id(self.message_id)
            await self.telegram_sender.send_message(status_message, parse_mode='html')
            if self.debug_channel_id:
                request_str = trade_result.request if trade_result else 'N/A'
                debug_msg_exec_fail = f"❌ {self.log_prefix} Trade Execution FAILED.\n<b>Reason:</b> {safe_comment} (Code: <code>{safe_code}</code>)\n<b>Request:</b> <pre>{request_str}</pre>"
                await self.telegram_sender.send_message(debug_msg_exec_fail, target_chat_id=self.debug_channel_id)


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