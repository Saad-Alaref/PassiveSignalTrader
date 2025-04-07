import logging
import json
import MetaTrader5 as mt5
import uuid # Add this import
from datetime import datetime, timezone # Ensure datetime is imported from datetime
from telethon import events

# Import necessary components (adjust as needed based on moved logic)
from .state_manager import StateManager # Use relative import
from .decision_logic import DecisionLogic # Use relative import
from .trade_calculator import TradeCalculator # Use relative import
from .mt5_executor import MT5Executor # Use relative import
from .telegram_sender import TelegramSender # Use relative import
from .signal_analyzer import SignalAnalyzer # Use relative import
from .duplicate_checker import DuplicateChecker # Use relative import
from .trade_execution_strategies import ( # Import new execution functions
    execute_distributed_limits,
    execute_multi_market_stop,
    execute_single_trade,
    parse_entry_range
)
from .update_commands import get_command # Import command factory

logger = logging.getLogger('TradeBot')

# --- Helper Function for Formatting Status Messages (Example) ---

def _format_html_message(title, message_id, details, success=True):
    """Helper to format common status messages in HTML."""
    icon = "✅" if success else ("❌" if title.endswith("FAILED") else ("⚠️" if title.startswith("Warning") or title.endswith("Aborted") else ("🚫" if title.endswith("REJECTED") else "ℹ️")))
    html = f"{icon} <b>{title}</b> <code>[MsgID: {message_id}]</code>\n"
    if isinstance(details, dict):
        for key, value in details.items():
            # Basic escaping for value
            safe_value = str(value).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            html += f"<b>{key}:</b> <code>{safe_value}</code>\n"
    elif isinstance(details, str):
         # Escape the reason string
         safe_details = details.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
         html += f"<b>Reason:</b> {safe_details}\n"

    return html.strip()


# --- Pre-Execution Checks Helper ---

async def _run_pre_execution_checks(
    lot_size, determined_order_type, signal_data, message_id,
    config_service_instance, mt5_fetcher, state_manager, telegram_sender,
    duplicate_checker, log_prefix
):
    """
    Performs pre-execution checks like Max Lot and Cooldown.

    Returns:
        bool: True if checks pass, False otherwise.
    """
    debug_channel_id = getattr(telegram_sender, 'debug_target_channel_id', None)

    # 1. Max Lot Check
    max_total_lots = config_service_instance.getfloat('Trading', 'max_total_open_lots', fallback=0.0)
    if max_total_lots > 0:
        current_volume = 0.0
        trade_symbol = signal_data.get('symbol', config_service_instance.get('MT5', 'symbol'))
        positions = mt5.positions_get(symbol=trade_symbol) # Assumes MT5 connection is active
        if positions is not None:
            current_volume = round(sum(pos.volume for pos in positions), 8)
        else:
            logger.error(f"{log_prefix} Failed to get current positions for max lot check: {mt5.last_error()}")
            status_message = f"⚠️ <b>Trade Aborted</b> <code>[MsgID: {message_id}]</code>\n<b>Reason:</b> Could not verify current positions for max lot check (Safety abort)"
            duplicate_checker.add_processed_id(message_id)
            await telegram_sender.send_message(status_message, parse_mode='html')
            if debug_channel_id:
                debug_msg_pos_fetch_err = f"❌ {log_prefix} Trade Aborted: Could not verify current positions for max lot check."
                await telegram_sender.send_message(debug_msg_pos_fetch_err, target_chat_id=debug_channel_id)
            return False # Check failed

        logger.debug(f"{log_prefix} Max Lot Check: Current Vol={current_volume}, New Vol={lot_size}, Max Allowed={max_total_lots}")
        if (current_volume + lot_size) > max_total_lots:
            logger.warning(f"{log_prefix} Trade volume ({lot_size}) would exceed max total open lots ({max_total_lots}). Current: {current_volume}. Trade Aborted.")
            status_message = f"⚠️ <b>Trade Aborted</b> <code>[MsgID: {message_id}]</code>\n<b>Reason:</b> Volume <code>{lot_size}</code> exceeds max total lots (<code>{max_total_lots}</code>). Current: <code>{current_volume}</code>"
            duplicate_checker.add_processed_id(message_id)
            await telegram_sender.send_message(status_message, parse_mode='html')
            if debug_channel_id:
                debug_msg_max_lot_exceeded = f"❌ {log_prefix} Trade Aborted: Volume {lot_size} exceeds max total lots ({max_total_lots}). Current: {current_volume}."
                await telegram_sender.send_message(debug_msg_max_lot_exceeded, target_chat_id=debug_channel_id)
            return False # Check failed
        else:
             logger.info(f"{log_prefix} Max lot check passed.")
    else:
        logger.debug(f"{log_prefix} Max total open lots check disabled (max_total_open_lots <= 0).")

    # 2. Cooldown Check (Market Orders Only)
    is_market_order = determined_order_type in [mt5.ORDER_TYPE_BUY, mt5.ORDER_TYPE_SELL]
    cooldown_enabled = config_service_instance.getboolean('Trading', 'enable_market_order_cooldown', fallback=True)
    cooldown_seconds = config_service_instance.getint('Trading', 'market_order_cooldown_seconds', fallback=60)

    if is_market_order and cooldown_enabled and state_manager.is_market_cooldown_active(cooldown_seconds):
        logger.warning(f"{log_prefix} Market order cooldown active. Trade Aborted.")
        status_message = f"⏳ <b>Trade Aborted</b> <code>[MsgID: {message_id}]</code>\n<b>Reason:</b> Market order cooldown active ({cooldown_seconds}s)."
        duplicate_checker.add_processed_id(message_id) # Mark aborted as processed
        await telegram_sender.send_message(status_message, parse_mode='html')
        if debug_channel_id:
            debug_msg_cooldown = f"⏳ {log_prefix} Trade Aborted: Market order cooldown active."
            await telegram_sender.send_message(debug_msg_cooldown, target_chat_id=debug_channel_id)
        return False # Check failed

    # All checks passed
    return True


# --- New Signal Processing ---

async def process_new_signal(signal_data: SignalData, message_id, state_manager: StateManager, # Expect SignalData object
                             decision_logic: DecisionLogic, trade_calculator: TradeCalculator,
                             mt5_executor: MT5Executor, telegram_sender: TelegramSender,
                             duplicate_checker: DuplicateChecker, config_service_instance, log_prefix,
                             mt5_fetcher):
    """Processes a validated 'new_signal' analysis result."""
    debug_channel_id = getattr(telegram_sender, 'debug_target_channel_id', None)

    try:
        if not signal_data:
             logger.error(f"{log_prefix} New signal type but no data found. Ignoring.")
             duplicate_checker.add_processed_id(message_id) # Mark as processed
             if debug_channel_id:
                 debug_msg_no_data = f"❓ {log_prefix} New signal type but no data found. Ignored."
                 await telegram_sender.send_message(debug_msg_no_data, target_chat_id=debug_channel_id)
             return

        # 1. Make Decision
        # Pass the dataclass object to decision logic
        is_approved, reason, determined_order_type = decision_logic.decide(signal_data)
        if debug_channel_id:
            decision_str = "APPROVED" if is_approved else "REJECTED"
            order_type_str_dbg = f" (Order Type: {determined_order_type})" if determined_order_type else ""
            debug_msg_decision = f"🤔 {log_prefix} Decision Logic:\n<b>Result:</b> {decision_str}\n<b>Reason:</b> {reason}{order_type_str_dbg}"
            await telegram_sender.send_message(debug_msg_decision, target_chat_id=debug_channel_id)

        if not is_approved:
            logger.info(f"{log_prefix} Trade rejected. Reason: {reason}")
            safe_reason = str(reason).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            status_message = f"🚫 <b>Trade REJECTED</b> <code>[MsgID: {message_id}]</code>\n<b>Reason:</b> {safe_reason}"
            duplicate_checker.add_processed_id(message_id) # Mark rejected as processed
            await telegram_sender.send_message(status_message, parse_mode='html')
            return

        # 2. Calculate Lot Size
        logger.info(f"{log_prefix} Trade decision: APPROVED. Reason: {reason}")
        # Pass the dataclass object to calculator
        lot_size = trade_calculator.calculate_lot_size(signal_data)
        if debug_channel_id:
            debug_msg_lots = f"🔢 {log_prefix} Lot Size Calculation:\n<b>Result:</b> <code>{lot_size}</code>"
            await telegram_sender.send_message(debug_msg_lots, target_chat_id=debug_channel_id)

        if lot_size <= 0:
            logger.error(f"{log_prefix} Invalid Lot Size ({lot_size}). Trade Aborted.")
            status_message = f"⚠️ <b>Trade Aborted</b> <code>[MsgID: {message_id}]</code>\n<b>Reason:</b> Invalid Lot Size (<code>{lot_size}</code>)"
            duplicate_checker.add_processed_id(message_id) # Mark aborted as processed
            await telegram_sender.send_message(status_message, parse_mode='html')
            if debug_channel_id:
                debug_msg_invalid_lot = f"❌ {log_prefix} Trade Aborted: Invalid Lot Size ({lot_size})."
                await telegram_sender.send_message(debug_msg_invalid_lot, target_chat_id=debug_channel_id)
            return

        # 3. Pre-Execution Checks (Max Lot, Cooldown)
        checks_passed = await _run_pre_execution_checks(
            lot_size=lot_size, determined_order_type=determined_order_type,
            signal_data=signal_data, message_id=message_id,
            config_service_instance=config_service_instance, mt5_fetcher=mt5_fetcher,
            state_manager=state_manager, telegram_sender=telegram_sender,
            duplicate_checker=duplicate_checker, log_prefix=log_prefix
        )
        if not checks_passed:
            logger.info(f"{log_prefix} Pre-execution checks failed. Aborting trade.")
            # Specific reason logged and message sent within the check function
            return # Stop processing

        # 4. Prepare Execution Parameters (Access attributes from dataclass)
        action = signal_data.action
        entry_price_raw = signal_data.entry_price # Can be number, "Market", "N/A", or "LOW-HIGH" string
        sl_price = signal_data.stop_loss
        take_profits_list = signal_data.take_profits # Get the list of TPs
        trade_symbol = signal_data.symbol

        # --- Determine Execution Price (Now handled by SignalAnalyzer) ---
        # SignalAnalyzer now returns the final numeric price in 'entry_price'
        # based on the configured strategy, or "Market"/"N/A".
        exec_price = None
        if entry_price_raw not in ["Market", "N/A"]:
             try:
                 exec_price = float(entry_price_raw)
             except (ValueError, TypeError):
                  logger.error(f"{log_prefix} Invalid numeric entry price '{entry_price_raw}' received from SignalAnalyzer. Aborting trade.")
                  return # Abort if price is invalid after analysis

        # --- Determine SL/TP for Execution based on Strategy ---
        exec_sl = None if sl_price == "N/A" else float(sl_price)
        exec_tp = None # Initialize TP for the order
        tp_strategy = config_service_instance.get('Strategy', 'tp_execution_strategy', fallback='first_tp_full_close').lower() # Use service
        valid_tps = [tp for tp in take_profits_list if tp != "N/A"] # Filter out "N/A"

        if valid_tps:
            try:
                numeric_tps = [float(tp) for tp in valid_tps]
                # Sort TPs: Ascending for BUY (closest first), Descending for SELL (closest first)
                numeric_tps.sort(reverse=(action == "SELL"))

                if tp_strategy == 'first_tp_full_close' or tp_strategy == 'sequential_partial_close':
                    # First element is now always the closest TP regardless of action
                    exec_tp = numeric_tps[0]
                    logger.info(f"{log_prefix} Using closest TP {exec_tp} for initial order based on strategy '{tp_strategy}'.")
                elif tp_strategy == 'last_tp_full_close':
                    # Last element is now always the farthest TP regardless of action
                    exec_tp = numeric_tps[-1]
                    logger.info(f"{log_prefix} Using farthest TP {exec_tp} for initial order based on strategy '{tp_strategy}'.")
                else:
                     logger.warning(f"{log_prefix} Unknown tp_execution_strategy '{tp_strategy}'. Using closest TP {numeric_tps[0]} as fallback.")
                     exec_tp = numeric_tps[0] # Fallback to closest TP

            except (ValueError, TypeError) as e:
                 logger.error(f"{log_prefix} Error processing TP list {valid_tps}: {e}. Setting initial TP to None.")
                 exec_tp = None
        else:
             logger.info(f"{log_prefix} No valid numeric TPs provided in the signal.")
             exec_tp = None

        # --- Apply AutoTP if enabled and no TP found ---
        auto_tp_applied = False
        logger.debug(f"{log_prefix} Checking AutoTP conditions. Initial exec_tp: {exec_tp}") # Log initial TP state
        if exec_tp is None: # Only apply if no TP was found from signal
            enable_auto_tp = config_service_instance.getboolean('AutoTP', 'enable_auto_tp', fallback=False) # Use service
            logger.debug(f"{log_prefix} AutoTP enabled in config: {enable_auto_tp}") # Log config state
            if enable_auto_tp:
                logger.info(f"{log_prefix} No TP found in signal and AutoTP enabled. Attempting to calculate AutoTP...")
                auto_tp_distance = config_service_instance.getfloat('AutoTP', 'auto_tp_price_distance', fallback=10.0) # Use service

                # Determine entry price for calculation
                calc_entry_price = None
                if determined_order_type in [mt5.ORDER_TYPE_BUY, mt5.ORDER_TYPE_SELL]: # Market Order
                    # Fetch current price for market order calculation
                    tick = mt5_fetcher.get_symbol_tick(trade_symbol)
                    if tick:
                        calc_entry_price = tick.ask if determined_order_type == mt5.ORDER_TYPE_BUY else tick.bid
                    else:
                        logger.error(f"{log_prefix} Cannot calculate AutoTP for market order: Failed to get current tick for {trade_symbol}.")
                else: # Pending Order
                    calc_entry_price = exec_price # Use the calculated pending entry price

                if calc_entry_price is not None:
                    auto_tp_price = trade_calculator.calculate_tp_from_distance(
                        symbol=trade_symbol,
                        order_type=determined_order_type,
                        entry_price=calc_entry_price,
                        tp_price_distance=auto_tp_distance
                    )
                    if auto_tp_price is not None:
                        exec_tp = auto_tp_price # Use the calculated AutoTP
                        auto_tp_applied = True
                        logger.info(f"{log_prefix} Calculated and applied AutoTP: {exec_tp} (Distance: {auto_tp_distance})")
                        # Update take_profits_list for status message and state storage
                        take_profits_list = [exec_tp]
                    else:
                        logger.error(f"{log_prefix} Failed to calculate AutoTP price. AutoTP will not be applied.") # Log calc failure
                else:
                     logger.error(f"{log_prefix} Cannot calculate AutoTP: Could not determine entry price for calculation. AutoTP will not be applied.") # Log entry price failure
            else:
                 logger.debug(f"{log_prefix} AutoTP is disabled in config. Skipping.") # Log disabled state
        else:
             logger.debug(f"{log_prefix} Signal already has a TP ({exec_tp}). Skipping AutoTP.") # Log existing TP state

        logger.debug(f"{log_prefix} Final exec_tp before sending order: {exec_tp}") # Log final TP value
        # --- End AutoTP ---

        # --- Adjust Entry Price for Spread on Pending Stop Orders ---
        if determined_order_type in [mt5.ORDER_TYPE_BUY_STOP, mt5.ORDER_TYPE_SELL_STOP] and exec_price is not None:
            logger.info(f"{log_prefix} Adjusting entry price for spread on {determined_order_type} order.")
            tick = mt5_fetcher.get_symbol_tick(trade_symbol)
            if tick and tick.bid > 0 and tick.ask > 0: # Ensure valid tick data
                spread = round(tick.ask - tick.bid, 8) # Calculate spread and round
                symbol_info = mt5_fetcher.get_symbol_info(trade_symbol)
                point = symbol_info.point if symbol_info else 0.00001 # Default point size

                # Ensure spread is positive and reasonable (e.g., less than 100 pips for safety)
                if spread < 0:
                     logger.warning(f"{log_prefix} Negative spread detected ({spread}). Skipping spread adjustment.")
                # Example safety check: 100 pips (adjust multiplier as needed, e.g., 1000 for 5-digit brokers)
                elif symbol_info and spread > (100 * (10**symbol_info.digits) * point):
                     logger.warning(f"{log_prefix} Unusually large spread detected ({spread}). Skipping spread adjustment.")
                else:
                    original_price = exec_price
                    if determined_order_type == mt5.ORDER_TYPE_BUY_STOP:
                        exec_price += spread
                        logger.info(f"{log_prefix} Applied spread adjustment for BUY_STOP. Original: {original_price}, Spread: {spread}, New: {exec_price}")
                    elif determined_order_type == mt5.ORDER_TYPE_SELL_STOP:
                        exec_price -= spread
                        logger.info(f"{log_prefix} Applied spread adjustment for SELL_STOP. Original: {original_price}, Spread: {spread}, New: {exec_price}")

                    # Round the adjusted price to the symbol's digits
                    if symbol_info:
                        exec_price = round(exec_price, symbol_info.digits)
                        logger.debug(f"{log_prefix} Rounded adjusted price to {symbol_info.digits} digits: {exec_price}")
                    else:
                         logger.warning(f"{log_prefix} Could not get symbol info for rounding adjusted price.")

            else:
                logger.warning(f"{log_prefix} Could not get valid tick data for {trade_symbol} to adjust entry price for spread. Using original price: {exec_price}")
        # --- End Spread Adjustment ---
        # 5. Market Order Confirmation Check (Cooldown check moved to pre-execution helper)
        is_market_order = determined_order_type in [mt5.ORDER_TYPE_BUY, mt5.ORDER_TYPE_SELL]

        # --- NEW: Check if Market Order requires confirmation ---
        if is_market_order:
            # --- Confirmation Logic ---
            logger.info(f"{log_prefix} Market order detected. Preparing confirmation request.")
            confirmation_id = str(uuid.uuid4()) # Generate unique ID
            confirmation_timeout_minutes = config_service_instance.getint('Trading', 'market_confirmation_timeout_minutes', fallback=3)

            # Prepare parameters needed for execution if confirmed
            trade_params_for_confirmation = {
                'action': action,
                'symbol': trade_symbol,
                'order_type': determined_order_type, # Should be BUY or SELL
                'volume': lot_size,
                'price': None, # Market order uses None for price
                'sl': exec_sl,
                'tp': exec_tp,
                'comment': f"TB SigID {message_id} ConfID {confirmation_id[:8]}", # Include conf ID part
                'original_signal_msg_id': message_id, # Add original signal message ID
                'auto_tp_applied': auto_tp_applied # Add AutoTP status
            }

            # Format the message for the user
            action_str = "BUY" if action == "BUY" else "SELL"
            symbol_str_safe = trade_symbol.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            sl_str_conf = f"<code>{exec_sl}</code>" if exec_sl is not None else "<i>None</i>"
            tp_str_conf = f"<code>{exec_tp}</code>" if exec_tp is not None else "<i>None</i>"
            confirmation_text = f"""❓ <b>Confirm Market Trade?</b> <code>[MsgID: {message_id}]</code>

<b>Action:</b> <code>{action_str}</code>
<b>Symbol:</b> <code>{symbol_str_safe}</code>
<b>Volume:</b> <code>{lot_size}</code>
<b>SL:</b> {sl_str_conf} | <b>TP:</b> {tp_str_conf}

<i>This confirmation expires in {confirmation_timeout_minutes} minutes.</i>"""

            # Send the message with buttons
            sent_conf_message = await telegram_sender.send_confirmation_message(
                confirmation_id=confirmation_id,
                trade_details=trade_params_for_confirmation, # Pass details for logging/context
                message_text=confirmation_text
            )

            if sent_conf_message:
                logger.info(f"{log_prefix} Confirmation message sent (ConfID: {confirmation_id}, MsgID: {sent_conf_message.id}). Awaiting user response.")
                # Store pending confirmation details in StateManager
                state_manager.add_pending_confirmation(
                    confirmation_id=confirmation_id,
                    trade_details=trade_params_for_confirmation,
                    message_id=sent_conf_message.id,
                    timestamp=datetime.now(timezone.utc)
                )
                # No separate status update needed, the confirmation message itself serves this purpose.

            else:
                logger.error(f"{log_prefix} Failed to send confirmation message for market order.")
                # Send failure status
                status_message_fail = f"❌ <b>Confirmation FAILED</b> <code>[MsgID: {message_id}]</code>\nCould not send confirmation message. Trade aborted."
                await telegram_sender.send_message(status_message_fail, parse_mode='html')

            # Mark original signal message as processed (handled via confirmation)
            duplicate_checker.add_processed_id(message_id)
            # IMPORTANT: Return here to prevent falling through to execution
            return

        else:
            # --- Existing Execution Logic (Moved here) ---
            # 6. Execute Trade (Pending Orders)
            logger.info(f"{log_prefix} Pending order detected. Proceeding with direct execution.")
        # 6. Determine Execution Strategy & Execute
        symbol_info = mt5_fetcher.get_symbol_info(trade_symbol)
        min_lot = symbol_info.volume_min if symbol_info else 0.01
        base_split_lot = max(min_lot, 0.01)
        entry_range_strategy = config_service_instance.get('Strategy', 'entry_range_strategy', fallback='closest').lower() # Use service
        is_entry_range = isinstance(entry_price_raw, str) and '-' in entry_price_raw

        # Ensure numeric_tps is available if needed by strategies
        # This variable should have been populated earlier in the TP processing block
        if 'numeric_tps' not in locals(): numeric_tps = [] # Initialize if somehow missing

        # Check conditions for distributed limits
        use_distributed_limits = (
            tp_strategy == 'sequential_partial_close' and
            entry_range_strategy == 'distributed' and
            is_entry_range and
            lot_size >= base_split_lot * 2 and
            numeric_tps # Ensure numeric_tps were successfully parsed earlier
        )

        # Check conditions for multi-trade market/stop
        use_multi_trade_market_stop = (
            not use_distributed_limits and # Only if not using distributed limits
            tp_strategy == 'sequential_partial_close' and
            lot_size >= base_split_lot * 2 and
            numeric_tps and
            determined_order_type in [mt5.ORDER_TYPE_BUY, mt5.ORDER_TYPE_SELL, mt5.ORDER_TYPE_BUY_STOP, mt5.ORDER_TYPE_SELL_STOP]
        )

        # --- Instantiate and Execute Strategy ---
        strategy_instance = None
        common_args = {
            "action": action, "trade_symbol": trade_symbol, "lot_size": lot_size,
            "exec_sl": exec_sl, "numeric_tps": numeric_tps, "message_id": message_id,
            "config_service_instance": config_service_instance, "mt5_fetcher": mt5_fetcher, "mt5_executor": mt5_executor, # Pass service instance
            "state_manager": state_manager, "telegram_sender": telegram_sender,
            "duplicate_checker": duplicate_checker, "log_prefix": log_prefix
        }

        try:
            if use_distributed_limits:
                strategy_instance = DistributedLimitsStrategy(
                    entry_price_raw=entry_price_raw, **common_args
                )
            elif use_multi_trade_market_stop:
                 strategy_instance = MultiMarketStopStrategy(
                    determined_order_type=determined_order_type, exec_price=exec_price,
                    **common_args
                )
            else:
                # Default to single trade execution
                strategy_instance = SingleTradeStrategy(
                    determined_order_type=determined_order_type, exec_price=exec_price,
                    exec_tp=exec_tp, take_profits_list=take_profits_list,
                    auto_tp_applied=auto_tp_applied, **common_args
                )

            # Execute the chosen strategy
            if strategy_instance:
                await strategy_instance.execute()
            else:
                # This case should ideally not be reached if logic is sound
                logger.error(f"{log_prefix} Failed to instantiate a valid execution strategy.")
                raise ValueError("Could not determine execution strategy")

        except Exception as strategy_exec_err:
             logger.error(f"{log_prefix} Error during trade strategy execution: {strategy_exec_err}", exc_info=True)
             # Send generic failure message if strategy execution itself fails
             status_message = f"❌ <b>Trade Execution FAILED</b> <code>[MsgID: {message_id}]</code>\n<b>Reason:</b> Internal error during strategy execution. Check logs."
             await telegram_sender.send_message(status_message, parse_mode='html')
             duplicate_checker.add_processed_id(message_id) # Mark as processed on error
             if debug_channel_id:
                 await telegram_sender.send_message(f"🆘 {log_prefix} Error during strategy execution: {strategy_exec_err}", target_chat_id=debug_channel_id)

    except Exception as exec_err:
         logger.error(f"{log_prefix} Error during signal execution: {exec_err}", exc_info=True)
         duplicate_checker.add_processed_id(message_id) # Mark as processed on error
         if debug_channel_id:
             debug_msg_exec_err = f"🆘 {log_prefix} Error during signal execution:\n<pre>{exec_err}</pre>"
             await telegram_sender.send_message(debug_msg_exec_err, target_chat_id=debug_channel_id)
         # Optionally send an error status message to main channel
         # status_message_err = f"🆘 <b>Error executing signal</b> <code>[MsgID: {message_id}]</code>. Check logs."
         # await telegram_sender.send_message(status_message_err, parse_mode='html')


# --- Update Processing ---

async def process_update(analysis_result: dict, event, state_manager: StateManager, # analysis_result is still dict {'type': 'update', 'data': UpdateData}
                         signal_analyzer: SignalAnalyzer, mt5_executor: MT5Executor,
                         telegram_sender: TelegramSender, duplicate_checker: DuplicateChecker,
                         config_service_instance, log_prefix, llm_context, image_data):
    """Processes a potential update message (from analysis or edit/reply)."""
    debug_channel_id = getattr(telegram_sender, 'debug_target_channel_id', None)
    message_id = event.id
    message_text = getattr(event, 'text', '')
    is_edit = isinstance(event, events.MessageEdited.Event) # Assuming events is imported
    reply_to_msg_id = getattr(event, 'reply_to_msg_id', None)

    try:
        logger.debug(f"{log_prefix} Entering process_update. Analysis result type: {analysis_result.get('type') if analysis_result else 'N/A'}, Is Edit: {is_edit}, ReplyTo: {reply_to_msg_id}")
        target_trade_info = None
        is_update_attempt = False
        update_data = None # Holds the dict with update details

        # Case A: New message classified as 'update' by initial analysis
        if analysis_result and analysis_result.get('type') == 'update':
            is_update_attempt = True
            update_symbol_hint = analysis_result.get('symbol')
            logger.info(f"{log_prefix} Processing new message classified as update. Symbol hint: {update_symbol_hint}")

            # Find target trade based on hint or latest overall
            active_trades_list = state_manager.get_active_trades() if state_manager else []
            relevant_trades = []
            if update_symbol_hint:
                relevant_trades = [t for t in active_trades_list if t['symbol'] == update_symbol_hint]
            else:
                relevant_trades = active_trades_list

            if relevant_trades:
                relevant_trades.sort(key=lambda x: x.get('open_time', datetime.min.replace(tzinfo=timezone.utc)), reverse=True) # Handle missing open_time
                target_trade_info = relevant_trades[0] # Target is the latest relevant trade
                logger.info(f"{log_prefix} Identified latest trade (Ticket: {target_trade_info.get('ticket')}, OrigMsgID: {target_trade_info.get('original_msg_id')}) as potential target for 'update' type message.")
                update_data_obj = analysis_result.get('data') # Use data (which is UpdateData obj) from initial analysis
                logger.debug(f"{log_prefix} Using update_data from initial analysis: {update_data}")
            else:
                logger.info(f"{log_prefix} No active trades found matching criteria for update.")
                status_message = f"⚠️ <b>Update Ignored</b> <code>[MsgID: {message_id}]</code> - No matching active trade found."
                await telegram_sender.send_message(status_message, parse_mode='html')
                duplicate_checker.add_processed_id(message_id)
                if debug_channel_id:
                    debug_msg_update_ignored = f"⚠️ {log_prefix} Update Ignored - No matching active trade found (Symbol Hint: {update_symbol_hint})."
                    await telegram_sender.send_message(debug_msg_update_ignored, target_chat_id=debug_channel_id)
                return # Stop processing this update

        # Case B: Edit or Reply to a previous message
        elif is_edit or reply_to_msg_id:
            is_update_attempt = True
            original_msg_id = message_id if is_edit else reply_to_msg_id
            logger.info(f"{log_prefix} Processing edit/reply targeting original MsgID: {original_msg_id}")

            # Find the trade associated with the original message ID
            target_trade_info = state_manager.get_trade_by_original_msg_id(original_msg_id) if state_manager else None

            if target_trade_info:
                logger.info(f"{log_prefix} Found tracked trade (Ticket: {target_trade_info.get('ticket')}, OrigMsgID: {target_trade_info.get('original_msg_id')}) linked to original message {original_msg_id}.")
                # Analyze the *edit/reply* text to get update details
                logger.info(f"{log_prefix} Re-analyzing edit/reply text for update details...")
                update_analysis_result = signal_analyzer.analyze(message_text, image_data, llm_context) # Re-analyze edit/reply text
                logger.debug(f"{log_prefix} Re-analysis result for edit/reply: {update_analysis_result}")

                if update_analysis_result and update_analysis_result.get('type') == 'update' and update_analysis_result.get('data'):
                    update_data_obj = update_analysis_result.get('data') # Get the UpdateData object
                    logger.debug(f"{log_prefix} Using update_data from edit/reply re-analysis: {update_data}")
                    # Allow LLM to override target based on index if provided in update analysis
                    target_index_edit = update_data_obj.target_trade_index # Access attribute
                    if target_index_edit is not None:
                         try:
                             list_index_edit = int(target_index_edit) - 1
                             active_trades_list = state_manager.get_active_trades() if state_manager else []
                             if 0 <= list_index_edit < len(active_trades_list):
                                 target_trade_info = active_trades_list[list_index_edit] # Override target
                                 logger.info(f"{log_prefix} Overrode target trade for edit/reply using LLM index {target_index_edit} -> Ticket: {target_trade_info.get('ticket')}")
                             else:
                                 logger.warning(f"{log_prefix} LLM provided invalid target_trade_index for edit/reply: {target_index_edit}.")
                         except (ValueError, TypeError):
                              logger.warning(f"{log_prefix} LLM provided non-integer target_trade_index for edit/reply: {target_index_edit}.")
                else:
                    logger.warning(f"{log_prefix} Edit/reply text re-analysis did not yield valid 'update' data. Type: {update_analysis_result.get('type') if update_analysis_result else 'None'}")
                    # If analysis fails, update_data remains None, and no action will be taken below.
            else:
                logger.info(f"{log_prefix} Edit/reply received, but no active trade tracked for original MsgID {original_msg_id}.")
                # No action needed if no related trade found

        # --- Apply the Update using Command Pattern ---
        logger.debug(f"{log_prefix} Checking if update should be applied: is_update_attempt={is_update_attempt}, target_trade_info_exists={target_trade_info is not None}, update_data_obj_exists={update_data_obj is not None}")
        if is_update_attempt and target_trade_info and update_data_obj: # Check for update_data_obj
            update_type = update_data_obj.update_type # Access attribute
            logger.info(f"{log_prefix} Identified update type '{update_type}' for ticket {target_trade_info['ticket']}")

            CommandClass = get_command(update_type)
            if CommandClass:
                command_instance = CommandClass(
                    update_data=update_data_obj, # Pass the UpdateData object
                    target_trade_info=target_trade_info,
                    mt5_executor=mt5_executor,
                    state_manager=state_manager,
                    telegram_sender=telegram_sender,
                    config_service_instance=config_service_instance, # Pass service
                    message_id=message_id,
                    log_prefix=log_prefix
                )
                try:
                    await command_instance.execute()
                except Exception as cmd_exec_err:
                    logger.error(f"{log_prefix} Error executing update command {CommandClass.__name__}: {cmd_exec_err}", exc_info=True)
                    # Send generic failure message
                    status_message_err = f"❌ <b>Update FAILED</b> <code>[MsgID: {message_id}]</code> (Ticket: <code>{target_trade_info['ticket']}</code>). Internal error during update execution. Check logs."
                    await telegram_sender.send_message(status_message_err, parse_mode='html')
                    if debug_channel_id:
                         await telegram_sender.send_message(f"🆘 {log_prefix} Error executing update command: {cmd_exec_err}", target_chat_id=debug_channel_id)

            else:
                # This case should ideally not happen if get_command defaults to UnknownUpdateCommand
                logger.error(f"{log_prefix} Could not find command class for update type '{update_type}'.")
                status_message_err = f"❓ <b>Update Unclear</b> <code>[MsgID: {message_id}]</code> (Ticket: <code>{target_trade_info['ticket']}</code>). Internal error: Unknown update type '{update_type}'."
                await telegram_sender.send_message(status_message_err, parse_mode='html')


            # Mark original message as processed if update came from new message analysis
            # (This assumes the command execution handles its own success/failure reporting)
            # Mark original message (from initial analysis) as processed
            if analysis_result and analysis_result.get('type') == 'update':
                 duplicate_checker.add_processed_id(message_id)

        elif is_update_attempt and not target_trade_info:
             logger.debug(f"{log_prefix} Update attempt detected but no target trade info found. No action taken.")
             # This case was handled inside the specific update type logic (new message or edit/reply)
             pass # No action needed if no target trade was found

    except Exception as update_err:
         logger.error(f"{log_prefix} Error during update processing: {update_err}", exc_info=True)
         # Mark as processed to avoid retrying on error if it was a new message update attempt
         if analysis_result and analysis_result.get('type') == 'update':
              duplicate_checker.add_processed_id(message_id)
         if debug_channel_id:
             debug_msg_update_err = f"🆘 {log_prefix} Error during update processing:\n<pre>{update_err}</pre>"
             await telegram_sender.send_message(debug_msg_update_err, target_chat_id=debug_channel_id)
         # Optionally send an error status message to main channel
         # status_message_err = f"🆘 <b>Error processing update</b> <code>[MsgID: {message_id}]</code>. Check logs."
         # await telegram_sender.send_message(status_message_err, parse_mode='html')