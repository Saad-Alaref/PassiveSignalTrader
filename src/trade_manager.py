import logging
import MetaTrader5 as mt5
from datetime import datetime, timezone, timedelta
from .state_manager import StateManager # Use relative import
from .mt5_executor import MT5Executor # Use relative import
from .trade_calculator import TradeCalculator # Use relative import
from .telegram_sender import TelegramSender # Use relative import

logger = logging.getLogger('TradeBot')

class TradeManager:
    """
    Manages trade-related operations like applying AutoSL.
    """

    def __init__(self, config, state_manager: StateManager, mt5_executor: MT5Executor,
                 trade_calculator: TradeCalculator, telegram_sender: TelegramSender,
                 mt5_fetcher): # Added mt5_fetcher
        """
        Initializes the TradeManager.

        Args:
            config (configparser.ConfigParser): The application configuration.
            state_manager (StateManager): Instance for accessing trade state.
            mt5_executor (MT5Executor): Instance for modifying trades.
            trade_calculator (TradeCalculator): Instance for calculating SL/TP prices.
            telegram_sender (TelegramSender): Instance for sending notifications.
            mt5_fetcher (MT5DataFetcher): Instance for fetching market data.
        """
        self.config = config
        self.state_manager = state_manager
        self.mt5_executor = mt5_executor
        self.trade_calculator = trade_calculator
        self.telegram_sender = telegram_sender
        self.mt5_fetcher = mt5_fetcher # Store fetcher
        logger.info("TradeManager initialized.")

    async def check_and_apply_auto_sl(self):
        """Checks trades pending AutoSL and applies SL if conditions are met."""
        if not self.config.getboolean('AutoSL', 'enable_auto_sl', fallback=False):
            return # Feature disabled

        auto_sl_delay_sec = self.config.getint('AutoSL', 'auto_sl_delay_seconds', fallback=30)
        auto_sl_risk = self.config.getfloat('AutoSL', 'auto_sl_risk_usd', fallback=5.0)
        now_utc = datetime.now(timezone.utc)
        trades_to_remove_pending_flag = [] # Keep track of trades processed

        pending_trades = self.state_manager.get_trades_pending_auto_sl()
        logger.debug(f"Checking {len(pending_trades)} trades pending AutoSL...")

        for trade_info in pending_trades:
            # The timestamp should exist if it's in this list, but check defensively
            pending_timestamp = trade_info.get('auto_sl_pending_timestamp')
            if not pending_timestamp:
                logger.warning(f"Trade {trade_info.get('ticket')} in pending list but has no timestamp. Skipping.")
                trades_to_remove_pending_flag.append(trade_info.get('ticket')) # Remove flag if invalid state
                continue

            ticket = trade_info['ticket']
            log_prefix_auto_sl = f"[AutoSL Check][Ticket: {ticket}]"

            # Check if delay has passed
            if now_utc < pending_timestamp + timedelta(seconds=auto_sl_delay_sec):
                logger.debug(f"{log_prefix_auto_sl} AutoSL delay not yet passed.")
                continue # Delay not yet passed

            logger.info(f"{log_prefix_auto_sl} AutoSL delay passed. Checking trade status...")

            # Verify trade exists and still has no SL
            position = mt5.positions_get(ticket=ticket)
            order = None
            trade_type = None
            current_sl = None
            entry_price = trade_info.get('entry_price') # Use stored entry price
            volume = None
            symbol = trade_info.get('symbol')

            if position and len(position) > 0:
                pos_data = position[0]
                current_sl = pos_data.sl
                trade_type = pos_data.type # ORDER_TYPE_BUY or ORDER_TYPE_SELL
                volume = pos_data.volume
                # Use actual position open price if available and more reliable than stored
                if entry_price is None: entry_price = pos_data.price_open
                logger.debug(f"{log_prefix_auto_sl} Found open position. Current SL: {current_sl}")
            else:
                order = mt5.orders_get(ticket=ticket)
                if order and len(order) > 0:
                    ord_data = order[0]
                    if ord_data.state == mt5.ORDER_STATE_PLACED:
                         logger.debug(f"{log_prefix_auto_sl} Ticket is still a pending order. Skipping AutoSL.")
                         continue # Skip pending orders
                    else:
                         logger.warning(f"{log_prefix_auto_sl} Trade not found as open position, but order exists (State: {ord_data.state}). Assuming inactive for AutoSL.")
                         trades_to_remove_pending_flag.append(ticket)
                         continue
                else:
                    logger.warning(f"{log_prefix_auto_sl} Trade not found as open position or pending order. Removing from AutoSL check.")
                    trades_to_remove_pending_flag.append(ticket)
                    continue # Trade doesn't exist anymore

            # Check if SL was added manually or by another update
            if current_sl is not None and current_sl != 0.0:
                logger.info(f"{log_prefix_auto_sl} SL ({current_sl}) already exists. Removing from AutoSL check.")
                trades_to_remove_pending_flag.append(ticket)
                continue

            # --- Conditions met: Apply Auto SL ---
            logger.info(f"{log_prefix_auto_sl} Applying AutoSL (Risk: ${auto_sl_risk})...")

            if entry_price is None:
                 logger.error(f"{log_prefix_auto_sl} Cannot apply AutoSL: Entry price is unknown.")
                 trades_to_remove_pending_flag.append(ticket) # Avoid retrying
                 continue
            if volume is None or volume <= 0:
                 logger.error(f"{log_prefix_auto_sl} Cannot apply AutoSL: Invalid volume ({volume}).")
                 trades_to_remove_pending_flag.append(ticket) # Avoid retrying
                 continue
            if trade_type is None or trade_type not in [mt5.ORDER_TYPE_BUY, mt5.ORDER_TYPE_SELL]:
                 logger.error(f"{log_prefix_auto_sl} Cannot apply AutoSL: Invalid trade type ({trade_type}).")
                 trades_to_remove_pending_flag.append(ticket) # Avoid retrying
                 continue

            # Calculate SL price
            auto_sl_price = self.trade_calculator.calculate_auto_sl_price(
                symbol=symbol,
                order_type=trade_type,
                entry_price=entry_price,
                volume=volume,
                risk_usd=auto_sl_risk
            )

            if auto_sl_price is None:
                logger.error(f"{log_prefix_auto_sl} Failed to calculate AutoSL price.")
                trades_to_remove_pending_flag.append(ticket) # Remove flag on calculation failure for now
                continue

            # Apply the calculated SL
            modify_success = self.mt5_executor.modify_trade(ticket=ticket, sl=auto_sl_price)

            if modify_success:
                logger.info(f"{log_prefix_auto_sl} Successfully applied AutoSL: {auto_sl_price}")
                trades_to_remove_pending_flag.append(ticket) # Mark as done

                # Send notifications
                entry_price_str = f"@{entry_price}" if entry_price is not None else "Market"
                status_msg_auto_sl = f"🤖 <b>Auto StopLoss Applied</b>\n<b>Ticket:</b> <code>{ticket}</code> (Entry: {entry_price_str})\n<b>New SL:</b> <code>{auto_sl_price}</code> (Risk ≈ ${auto_sl_risk})"
                await self.telegram_sender.send_message(status_msg_auto_sl, parse_mode='html')
                debug_channel_id = getattr(self.telegram_sender, 'debug_target_channel_id', None)
                if debug_channel_id:
                     await self.telegram_sender.send_message(f"🤖 {log_prefix_auto_sl} Applied AutoSL: {auto_sl_price}", target_chat_id=debug_channel_id)
            else:
                logger.error(f"{log_prefix_auto_sl} Failed to apply AutoSL price {auto_sl_price} via modify_trade.")
                # Keep pending flag, maybe modification works next time?

        # Remove pending flags for processed trades
        if trades_to_remove_pending_flag:
            for ticket in trades_to_remove_pending_flag:
                self.state_manager.remove_auto_sl_pending_flag(ticket)

    async def check_and_handle_tp_hits(self):
        """Checks active trades for TP hits and handles based on configured strategy."""
        if not self.state_manager:
            logger.error("Cannot check TPs: StateManager not available.")
            return

        active_trades = self.state_manager.get_active_trades()
        if not active_trades:
            # logger.debug("No active trades to check for TP hits.") # Reduce log noise
            return

        # logger.debug(f"Checking {len(active_trades)} active trades for TP hits...") # Reduce log noise
        partial_close_perc = self.config.getint('Strategy', 'partial_close_percentage', fallback=50)
        tp_strategy = self.config.get('Strategy', 'tp_execution_strategy', fallback='first_tp_full_close').lower()

        # --- Skip check if strategy doesn't involve monitoring TPs after entry ---
        if tp_strategy == 'first_tp_full_close' or tp_strategy == 'last_tp_full_close':
            return

        # --- Only proceed for sequential_partial_close ---
        if tp_strategy != 'sequential_partial_close':
             logger.warning(f"TP Check: Unsupported tp_execution_strategy '{tp_strategy}' encountered in check loop.")
             return

        for trade_info in active_trades:
            ticket = trade_info.get('ticket')
            symbol = trade_info.get('symbol')
            all_tps = trade_info.get('all_tps', [])
            next_tp_index = trade_info.get('next_tp_index', 0) # Get the index of the TP we are monitoring
            original_volume = trade_info.get('original_volume') # Need original volume for percentage calc
            original_msg_id = trade_info.get('original_msg_id', 'N/A')

            log_prefix_tp = f"[TP Check][Ticket: {ticket}]"

            # Check if we have processed all TPs for this trade or if data is missing
            if next_tp_index >= len(all_tps) or original_volume is None:
                continue

            current_tp_target_str = all_tps[next_tp_index]
            if current_tp_target_str == "N/A":
                continue # No more valid TPs to monitor for this trade

            # --- Check for Current TP Hit ---
            current_tp_target = None
            try:
                current_tp_target = float(current_tp_target_str)
            except (ValueError, TypeError):
                logger.warning(f"{log_prefix_tp} Invalid TP value '{current_tp_target_str}' at index {next_tp_index}. Skipping TP check for this level.")
                trade_info['next_tp_index'] += 1 # Move to next index to avoid re-checking invalid TP
                continue

            # Get current market price
            tick = self.mt5_fetcher.get_symbol_tick(symbol)
            if not tick:
                logger.warning(f"{log_prefix_tp} Could not get current tick for {symbol}. Skipping TP check.")
                continue

            # Get current position details
            position = mt5.positions_get(ticket=ticket)
            if not position or len(position) == 0:
                # logger.debug(f"{log_prefix_tp} Position not found. Assuming closed or inactive.") # Reduce log noise
                continue

            pos_data = position[0]
            trade_type = pos_data.type
            current_pos_volume = pos_data.volume
            entry_price = pos_data.price_open

            tp_hit = False
            if trade_type == mt5.ORDER_TYPE_BUY and tick.bid >= current_tp_target:
                tp_hit = True
                logger.info(f"{log_prefix_tp} TP{next_tp_index + 1} ({current_tp_target}) hit for BUY position (Bid: {tick.bid}).")
            elif trade_type == mt5.ORDER_TYPE_SELL and tick.ask <= current_tp_target:
                tp_hit = True
                logger.info(f"{log_prefix_tp} TP{next_tp_index + 1} ({current_tp_target}) hit for SELL position (Ask: {tick.ask}).")

            if tp_hit:
                is_last_tp = (next_tp_index == len(all_tps) - 1) or (len(all_tps) > next_tp_index + 1 and all_tps[next_tp_index + 1] == "N/A")

                volume_to_close = 0.0
                action_desc = ""

                if is_last_tp:
                    volume_to_close = current_pos_volume # Close remaining volume
                    action_desc = f"Close Full at Final TP{next_tp_index + 1}"
                    logger.info(f"{log_prefix_tp} Final TP hit. Closing remaining {volume_to_close} lots...")
                else:
                    # Calculate partial volume based on ORIGINAL volume
                    volume_to_close = round(original_volume * (partial_close_perc / 100.0), 8)
                    action_desc = f"Partial Close at TP{next_tp_index + 1}"
                    logger.info(f"{log_prefix_tp} Initiating partial close ({partial_close_perc}% of original)...")

                # Execute close action (partial or full)
                # Executor handles volume checks (min/step/current) and might override to full close if needed
                close_success = self.mt5_executor.close_position(
                    ticket=ticket,
                    volume=volume_to_close,
                    comment=f"{action_desc} (SigID {original_msg_id})"
                )

                if close_success:
                    logger.info(f"{log_prefix_tp} {action_desc} successful for {volume_to_close} lots.")
                    trade_info['next_tp_index'] += 1 # Move to next TP index

                    next_tp_value_for_modify = None
                    next_tp_display = "<i>None</i>"

                    # If not the last TP, modify remaining position to next TP
                    if not is_last_tp:
                        next_tp_index_for_modify = trade_info['next_tp_index']
                        if next_tp_index_for_modify < len(all_tps) and all_tps[next_tp_index_for_modify] != "N/A":
                            try:
                                next_tp_value_for_modify = float(all_tps[next_tp_index_for_modify])
                                next_tp_display = f"<code>{next_tp_value_for_modify}</code>"
                                logger.info(f"{log_prefix_tp} Modifying remaining position TP to TP{next_tp_index_for_modify + 1}: {next_tp_value_for_modify}")
                            except (ValueError, TypeError):
                                logger.warning(f"{log_prefix_tp} Invalid next TP value '{all_tps[next_tp_index_for_modify]}'. Removing TP.")
                                next_tp_value_for_modify = 0.0 # Remove TP if next one is invalid
                        else:
                            logger.info(f"{log_prefix_tp} No further valid TPs defined. Removing TP from remaining position.")
                            next_tp_value_for_modify = 0.0 # Remove TP if no more valid TPs

                        # Modify the trade TP
                        modify_tp_success = self.mt5_executor.modify_trade(ticket=ticket, tp=next_tp_value_for_modify)
                        if not modify_tp_success:
                             logger.error(f"{log_prefix_tp} Failed to modify TP to {next_tp_value_for_modify} after partial close.")
                             # Continue anyway, close was successful

                    # Send Notification
                    entry_price_str = f"@{entry_price:.{self.trade_calculator.symbol_digits}f}"
                    status_msg = f"💰 <b>{action_desc}</b> <code>[MsgID: {original_msg_id}]</code>\n" \
                                 f"<b>Ticket:</b> <code>{ticket}</code> (Entry: {entry_price_str})\n" \
                                 f"<b>Closed:</b> <code>{volume_to_close}</code> lots at ≈<code>{current_tp_target}</code>\n" \
                                 f"<b>Next TP Target:</b> {next_tp_display}"
                    await self.telegram_sender.send_message(status_msg, parse_mode='html')
                    debug_channel_id = getattr(self.telegram_sender, 'debug_target_channel_id', None)
                    if debug_channel_id:
                         await self.telegram_sender.send_message(f"💰 {log_prefix_tp} {action_desc} {volume_to_close} lots at TP {current_tp_target}. Next TP: {next_tp_display}", target_chat_id=debug_channel_id)

                else:
                    logger.error(f"{log_prefix_tp} {action_desc} FAILED.")
                    # Don't increment next_tp_index, retry might happen next cycle if TP still hit


# Example usage (optional, for testing within this file)
if __name__ == '__main__':
    import configparser
    import asyncio
    from logger_setup import setup_logging
    from state_manager import StateManager
    # Need dummy versions of other classes for testing
    class DummyExecutor:
        async def modify_trade(self, ticket, sl=None, tp=None):
            print(f"[DummyExecutor] Modify called for {ticket} with SL={sl}, TP={tp}")
            return True # Simulate success
    class DummyCalculator:
        def calculate_auto_sl_price(self, symbol, order_type, entry_price, volume, risk_usd):
            print(f"[DummyCalculator] Calculate SL called for {symbol}, type={order_type}, entry={entry_price}, vol={volume}, risk={risk_usd}")
            if order_type == mt5.ORDER_TYPE_BUY: return round(entry_price - 0.00050, 5) # Simulate calc
            if order_type == mt5.ORDER_TYPE_SELL: return round(entry_price + 0.00050, 5)
            return None
    class DummySender:
        debug_target_channel_id = -100123456 # Dummy ID
        async def send_message(self, text, parse_mode=None, target_chat_id=None):
            target = target_chat_id if target_chat_id else "Main Channel"
            print(f"[DummySender] Send to {target}: {text}")

    # Setup basic logging for test
    setup_logging(log_level_str='DEBUG')

    # Dummy config
    config = configparser.ConfigParser()
    config['AutoSL'] = {'enable_auto_sl': 'true', 'auto_sl_delay_seconds': '1', 'auto_sl_risk_usd': '5.0'}
    config['LLMContext'] = {'history_message_count': '3'} # For StateManager init
    config['MT5'] = {'symbol': 'EURUSD'} # For StateManager init

    # Dummy MT5 functions needed for testing
    class DummyPosition:
        def __init__(self, ticket, sl, type, volume, price_open):
            self.ticket = ticket
            self.sl = sl
            self.type = type
            self.volume = volume
            self.price_open = price_open
    def dummy_positions_get(ticket=None):
        if ticket == 1001: return [DummyPosition(1001, 0.0, mt5.ORDER_TYPE_BUY, 0.01, 1.10000)]
        if ticket == 1002: return [DummyPosition(1002, 1.10500, mt5.ORDER_TYPE_SELL, 0.02, 1.10000)] # Already has SL
        return []
    def dummy_orders_get(ticket=None): return [] # No pending orders in this test
    mt5.positions_get = dummy_positions_get
    mt5.orders_get = dummy_orders_get

    # Initialize components
    state = StateManager(config)
    executor = DummyExecutor()
    calculator = DummyCalculator()
    sender = DummySender()
    manager = TradeManager(config, state, executor, calculator, sender)

    # Add test trades to state
    trade1_time = datetime.now(timezone.utc) - timedelta(seconds=5) # Ensure delay has passed
    state.add_active_trade({'ticket': 1001, 'symbol': 'EURUSD', 'open_time': trade1_time, 'entry_price': 1.10000})
    state.add_active_trade({'ticket': 1002, 'symbol': 'EURUSD', 'open_time': trade1_time, 'entry_price': 1.10000})
    state.add_active_trade({'ticket': 1003, 'symbol': 'EURUSD', 'open_time': datetime.now(timezone.utc)}) # Delay not passed yet

    # Mark trades for AutoSL
    state.mark_trade_for_auto_sl(1001)
    state.mark_trade_for_auto_sl(1002) # Will be skipped as it has SL
    state.mark_trade_for_auto_sl(1003) # Will be skipped due to delay

    async def run_test():
        print("\n--- Running AutoSL Check ---")
        await manager.check_and_apply_auto_sl()
        print("--- AutoSL Check Finished ---")

        # Verify flags
        trade1_after = state.get_trade_by_ticket(1001)
        trade2_after = state.get_trade_by_ticket(1002)
        trade3_after = state.get_trade_by_ticket(1003)

        assert 'auto_sl_pending_timestamp' not in trade1_after # Should have been processed and removed
        assert 'auto_sl_pending_timestamp' not in trade2_after # Should have been removed (existing SL)
        assert 'auto_sl_pending_timestamp' in trade3_after # Should still be pending (delay)

    asyncio.run(run_test())
    print("\nTradeManager tests finished.")