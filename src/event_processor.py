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


# --- New Signal Processing ---

async def process_new_signal(signal_data, message_id, state_manager: StateManager,
                             decision_logic: DecisionLogic, trade_calculator: TradeCalculator,
                             mt5_executor: MT5Executor, telegram_sender: TelegramSender,
                             duplicate_checker: DuplicateChecker, config, log_prefix,
                             mt5_fetcher): # Added mt5_fetcher
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

        # 3. Max Lot Check
        max_total_lots = config.getfloat('Trading', 'max_total_open_lots', fallback=0.0)
        if max_total_lots > 0:
            current_volume = 0.0
            trade_symbol = signal_data.get('symbol', config.get('MT5', 'symbol'))
            positions = mt5.positions_get(symbol=trade_symbol)
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
                return

            logger.debug(f"{log_prefix} Max Lot Check: Current Vol={current_volume}, New Vol={lot_size}, Max Allowed={max_total_lots}")
            if (current_volume + lot_size) > max_total_lots:
                logger.warning(f"{log_prefix} Trade volume ({lot_size}) would exceed max total open lots ({max_total_lots}). Current: {current_volume}. Trade Aborted.")
                status_message = f"⚠️ <b>Trade Aborted</b> <code>[MsgID: {message_id}]</code>\n<b>Reason:</b> Volume <code>{lot_size}</code> exceeds max total lots (<code>{max_total_lots}</code>). Current: <code>{current_volume}</code>"
                duplicate_checker.add_processed_id(message_id)
                await telegram_sender.send_message(status_message, parse_mode='html')
                if debug_channel_id:
                    debug_msg_max_lot_exceeded = f"❌ {log_prefix} Trade Aborted: Volume {lot_size} exceeds max total lots ({max_total_lots}). Current: {current_volume}."
                    await telegram_sender.send_message(debug_msg_max_lot_exceeded, target_chat_id=debug_channel_id)
                return
            else:
                 logger.info(f"{log_prefix} Max lot check passed.")
        else:
            logger.debug(f"{log_prefix} Max total open lots check disabled (max_total_open_lots <= 0).")

        # 4. Prepare Execution Parameters
        action = signal_data.get("action")
        entry_price_raw = signal_data.get("entry_price") # Can be number, "Market", "N/A", or "LOW-HIGH" string
        sl_price = signal_data.get("stop_loss")
        take_profits_list = signal_data.get("take_profits", ["N/A"]) # Get the list of TPs
        trade_symbol = signal_data.get("symbol")

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
        tp_strategy = config.get('Strategy', 'tp_execution_strategy', fallback='first_tp_full_close').lower()
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
            enable_auto_tp = config.getboolean('AutoTP', 'enable_auto_tp', fallback=False)
            logger.debug(f"{log_prefix} AutoTP enabled in config: {enable_auto_tp}") # Log config state
            if enable_auto_tp:
                logger.info(f"{log_prefix} No TP found in signal and AutoTP enabled. Attempting to calculate AutoTP...")
                auto_tp_distance = config.getfloat('AutoTP', 'auto_tp_price_distance', fallback=10.0)

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
        # 5. Cooldown Check (Market Orders Only)
        is_market_order = determined_order_type in [mt5.ORDER_TYPE_BUY, mt5.ORDER_TYPE_SELL]
        cooldown_enabled = config.getboolean('Trading', 'enable_market_order_cooldown', fallback=True)
        cooldown_seconds = config.getint('Trading', 'market_order_cooldown_seconds', fallback=60)

        if is_market_order and cooldown_enabled and state_manager.is_market_cooldown_active(cooldown_seconds):
            logger.warning(f"{log_prefix} Market order cooldown active. Trade Aborted.")
            status_message = f"⏳ <b>Trade Aborted</b> <code>[MsgID: {message_id}]</code>\n<b>Reason:</b> Market order cooldown active ({cooldown_seconds}s)."
            duplicate_checker.add_processed_id(message_id) # Mark aborted as processed
            await telegram_sender.send_message(status_message, parse_mode='html')
            if debug_channel_id:
                debug_msg_cooldown = f"⏳ {log_prefix} Trade Aborted: Market order cooldown active."
                await telegram_sender.send_message(debug_msg_cooldown, target_chat_id=debug_channel_id)
            return # Stop processing this signal

        # --- NEW: Check if Market Order requires confirmation ---
        if is_market_order:
            # --- Confirmation Logic ---
            logger.info(f"{log_prefix} Market order detected. Preparing confirmation request.")
            confirmation_id = str(uuid.uuid4()) # Generate unique ID
            confirmation_timeout_minutes = config.getint('Trading', 'market_confirmation_timeout_minutes', fallback=3)

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
        # 6. Execute Trade(s)
        # --- Determine Minimum Lot Size ---
        symbol_info = mt5_fetcher.get_symbol_info(trade_symbol)
        min_lot = symbol_info.volume_min if symbol_info else 0.01
        lot_step = symbol_info.volume_step if symbol_info else 0.01
        digits = symbol_info.digits if symbol_info else 5 # For rounding prices
        # Use 0.01 as base split lot, but ensure it's not smaller than the symbol's minimum lot
        base_split_lot = max(min_lot, 0.01)

        # --- Read Entry Range Strategy ---
        entry_range_strategy = config.get('Strategy', 'entry_range_strategy', fallback='closest').lower()

        # --- Helper function to parse entry range ---
        def parse_entry_range(range_str):
            try:
                low_str, high_str = range_str.split('-')
                low = float(low_str)
                high = float(high_str)
                if low > high: low, high = high, low # Ensure low <= high
                return low, high
            except Exception as e:
                logger.warning(f"{log_prefix} Failed to parse entry range '{range_str}': {e}")
                return None, None

        # --- Determine Execution Strategy ---
        is_entry_range = isinstance(entry_price_raw, str) and '-' in entry_price_raw
        use_distributed_limits = (
            tp_strategy == 'sequential_partial_close' and
            entry_range_strategy == 'distributed' and
            is_entry_range and
            lot_size >= base_split_lot * 2 and
            numeric_tps
        )
        use_multi_trade_market_stop = (
            not use_distributed_limits and # Only if not using distributed limits
            tp_strategy == 'sequential_partial_close' and
            lot_size >= base_split_lot * 2 and
            numeric_tps and
            determined_order_type in [mt5.ORDER_TYPE_BUY, mt5.ORDER_TYPE_SELL, mt5.ORDER_TYPE_BUY_STOP, mt5.ORDER_TYPE_SELL_STOP] # Market or Stop orders
        )

        # --- Strategy 1: Distributed Pending Limit Orders ---
        if use_distributed_limits:
            logger.info(f"{log_prefix} Applying distributed pending limit order strategy.")
            low_price, high_price = parse_entry_range(entry_price_raw)

            if low_price is None or high_price is None:
                logger.error(f"{log_prefix} Invalid entry range format '{entry_price_raw}'. Aborting distributed strategy.")
                # Fallback? Or just abort? For now, abort. Consider fallback later.
                status_message = f"❌ <b>Trade Execution FAILED</b> <code>[MsgID: {message_id}]</code>\n<b>Reason:</b> Invalid entry range format for distributed strategy: '{entry_price_raw}'"
                await telegram_sender.send_message(status_message, parse_mode='html')
                duplicate_checker.add_processed_id(message_id)
                return # Abort this signal processing

            num_full_trades = int(lot_size // base_split_lot)
            remainder_lot_raw = lot_size % base_split_lot
            remainder_lot = round(remainder_lot_raw / lot_step) * lot_step if lot_step > 0 else remainder_lot_raw
            if remainder_lot < min_lot: remainder_lot = 0.0

            total_trades_to_open = num_full_trades + (1 if remainder_lot > 0 else 0)
            if total_trades_to_open == 0:
                 logger.error(f"{log_prefix} Calculated zero trades to open for distributed strategy. Lot Size: {lot_size}, Base Split: {base_split_lot}")
                 # Abort if somehow calculation leads to zero trades
                 status_message = f"❌ <b>Trade Execution FAILED</b> <code>[MsgID: {message_id}]</code>\n<b>Reason:</b> Calculated zero trades for distributed strategy."
                 await telegram_sender.send_message(status_message, parse_mode='html')
                 duplicate_checker.add_processed_id(message_id)
                 return

            logger.info(f"{log_prefix} Calculated Trades: {num_full_trades} x {base_split_lot}, Remainder: {remainder_lot}. Total: {total_trades_to_open}. Range: {low_price}-{high_price}")

            # Calculate price step
            price_step = 0.0
            if total_trades_to_open > 1:
                price_step = (high_price - low_price) / (total_trades_to_open - 1)

            executed_tickets_info = []
            failed_trades = 0
            successful_trades = 0
            last_error = ""
            limit_order_type = mt5.ORDER_TYPE_BUY_LIMIT if action == "BUY" else mt5.ORDER_TYPE_SELL_LIMIT

            # --- Place Pending Limit Orders ---
            for i in range(total_trades_to_open):
                current_vol = base_split_lot if i < num_full_trades else remainder_lot
                # Calculate entry price for this order
                current_entry_price = low_price + i * price_step
                current_entry_price = round(current_entry_price, digits)

                # Determine TP
                tp_index = min(i, len(numeric_tps) - 1)
                current_exec_tp = numeric_tps[tp_index]
                trade_comment = f"TB SigID {message_id} Dist {i+1}/{total_trades_to_open}"

                logger.info(f"{log_prefix} Placing pending limit order {i+1}/{total_trades_to_open}: Type={limit_order_type}, Vol={current_vol}, Entry={current_entry_price}, TP={current_exec_tp}")

                trade_result_tuple = mt5_executor.execute_trade(
                    action=action, symbol=trade_symbol, order_type=limit_order_type,
                    volume=current_vol, price=current_entry_price, sl=exec_sl, tp=current_exec_tp,
                    comment=trade_comment
                )
                # For pending orders, actual_exec_price is not relevant immediately
                trade_result, _ = trade_result_tuple if trade_result_tuple else (None, None)

                if trade_result and trade_result.retcode == mt5.TRADE_RETCODE_DONE:
                    ticket = trade_result.order
                    executed_tickets_info.append({'ticket': ticket, 'vol': current_vol, 'tp': current_exec_tp, 'entry': current_entry_price})
                    successful_trades += 1
                    logger.info(f"{log_prefix} Pending limit order {i+1} placed successfully. Ticket: {ticket}")
                    # Store individual pending order info
                    open_time = datetime.now(timezone.utc) # Placement time
                    trade_info = {
                        'ticket': ticket, 'symbol': trade_symbol, 'open_time': open_time,
                        'original_msg_id': message_id, 'entry_price': current_entry_price, # The pending price
                        'initial_sl': exec_sl, 'original_volume': current_vol,
                        'all_tps': [current_exec_tp], 'tp_strategy': tp_strategy,
                        'next_tp_index': 0, 'assigned_tp': current_exec_tp, 'tsl_active': False,
                        'sequence_info': f"Dist {i+1}/{total_trades_to_open}",
                        'is_pending': True # Mark as pending
                    }
                    if state_manager:
                        state_manager.add_active_trade(trade_info, auto_tp_applied=False)
                        # AutoSL check might need adjustment for pending orders, TBD
                        # if config.getboolean('AutoSL', 'enable_auto_sl', fallback=False) and exec_sl is None:
                        #     state_manager.mark_trade_for_auto_sl(ticket) # Does this work on pending?
                    else: logger.error(f"Cannot store active trade info: StateManager not initialized.")
                else:
                    failed_trades += 1
                    error_comment = getattr(trade_result, 'comment', 'Unknown Error') if trade_result else 'None Result'
                    last_error = f"{error_comment} (Code: {getattr(trade_result, 'retcode', 'N/A')})"
                    logger.error(f"{log_prefix} Pending limit order {i+1} FAILED. Reason: {last_error}. Result: {trade_result_tuple}")

            # --- Report Distributed Limit Order Result ---
            if successful_trades > 0:
                status_title = f"✅ Distributed Limits Placed ({successful_trades}/{total_trades_to_open} OK)" if failed_trades == 0 else f"⚠️ Distributed Limits Partially Placed ({successful_trades}/{total_trades_to_open} OK)"
                status_message = f"{status_title} <code>[MsgID: {message_id}]</code>\n"
                status_message += f"<b>Symbol:</b> <code>{trade_symbol}</code> | <b>Total Vol:</b> <code>{lot_size}</code>\n"
                status_message += f"<b>Range:</b> <code>{low_price}-{high_price}</code>\n"
                status_message += f"<b>SL:</b> {'<code>'+str(exec_sl)+'</code>' if exec_sl else '<i>None</i>'}\n"
                status_message += "<b>Pending Orders Placed:</b>\n"
                for idx, trade in enumerate(executed_tickets_info):
                    status_message += f"  <code>{idx+1}. Ticket: {trade['ticket']}, Vol: {trade['vol']}, Entry: {trade['entry']}, TP: {trade['tp']}</code>\n"
                if failed_trades > 0:
                    status_message += f"<b>Failures:</b> {failed_trades} order(s) failed. Last Error: {last_error}\n"
                await telegram_sender.send_message(status_message, parse_mode='html')
                if debug_channel_id: await telegram_sender.send_message(f"{log_prefix} Distributed limit order summary:\n{status_message}", target_chat_id=debug_channel_id, parse_mode='html')
                duplicate_checker.add_processed_id(message_id)
            else:
                # All orders failed
                status_message = f"❌ <b>Distributed Limits FAILED</b> <code>[MsgID: {message_id}]</code>\n<b>Reason:</b> All {total_trades_to_open} pending orders failed. Last Error: {last_error}"
                logger.error(f"{log_prefix} All {total_trades_to_open} distributed pending orders failed placement.")
                duplicate_checker.add_processed_id(message_id)
                await telegram_sender.send_message(status_message, parse_mode='html')
                if debug_channel_id: await telegram_sender.send_message(f"❌ {log_prefix} All distributed pending orders failed.\n{status_message}", target_chat_id=debug_channel_id, parse_mode='html')

        # --- Strategy 2: Multi-Trade Market/Stop Orders (Sequential TP) ---
        elif use_multi_trade_market_stop:
            # --- This is the logic moved from the previous version ---
            logger.info(f"{log_prefix} Applying multi-trade sequential TP strategy (Market/Stop). Base Split Lot: {base_split_lot}")
            num_full_trades = int(lot_size // base_split_lot)
            remainder_lot_raw = lot_size % base_split_lot
            remainder_lot = round(remainder_lot_raw / lot_step) * lot_step if lot_step > 0 else remainder_lot_raw
            if remainder_lot < min_lot: remainder_lot = 0.0

            total_trades_to_open = num_full_trades + (1 if remainder_lot > 0 else 0)
            logger.info(f"{log_prefix} Calculated Trades: {num_full_trades} x {base_split_lot} lots, Remainder: {remainder_lot} lots. Total: {total_trades_to_open}")

            executed_tickets_info = []
            failed_trades = 0
            successful_trades = 0
            last_error = ""

            # --- Execute Full Lot Trades ---
            for i in range(num_full_trades):
                tp_index = min(i, len(numeric_tps) - 1)
                current_exec_tp = numeric_tps[tp_index]
                trade_comment = f"TB SigID {message_id} Seq {i+1}/{total_trades_to_open}"

                logger.info(f"{log_prefix} Executing trade {i+1}/{total_trades_to_open}: Vol={base_split_lot}, TP={current_exec_tp}")
                trade_result_tuple = mt5_executor.execute_trade(
                    action=action, symbol=trade_symbol, order_type=determined_order_type,
                    volume=base_split_lot, price=exec_price, sl=exec_sl, tp=current_exec_tp,
                    comment=trade_comment
                )
                trade_result, actual_exec_price = trade_result_tuple if trade_result_tuple else (None, None)

                if trade_result and trade_result.retcode == mt5.TRADE_RETCODE_DONE:
                    ticket = trade_result.order
                    executed_tickets_info.append({'ticket': ticket, 'vol': base_split_lot, 'tp': current_exec_tp})
                    successful_trades += 1
                    logger.info(f"{log_prefix} Sub-trade {i+1} executed successfully. Ticket: {ticket}")
                    # Store individual trade info
                    final_entry_price = actual_exec_price if actual_exec_price is not None else exec_price
                    open_time = datetime.now(timezone.utc)
                    trade_info = {
                        'ticket': ticket, 'symbol': trade_symbol, 'open_time': open_time,
                        'original_msg_id': message_id, 'entry_price': final_entry_price,
                        'initial_sl': exec_sl, 'original_volume': base_split_lot,
                        'all_tps': [current_exec_tp], 'tp_strategy': tp_strategy,
                        'next_tp_index': 0, 'assigned_tp': current_exec_tp, 'tsl_active': False,
                        'sequence_info': f"Seq {i+1}/{total_trades_to_open}"
                    }
                    if state_manager:
                        state_manager.add_active_trade(trade_info, auto_tp_applied=False)
                        if config.getboolean('AutoSL', 'enable_auto_sl', fallback=False) and exec_sl is None:
                            state_manager.mark_trade_for_auto_sl(ticket)
                    else: logger.error(f"Cannot store active trade info: StateManager not initialized.")
                else:
                    failed_trades += 1
                    error_comment = getattr(trade_result, 'comment', 'Unknown Error') if trade_result else 'None Result'
                    last_error = f"{error_comment} (Code: {getattr(trade_result, 'retcode', 'N/A')})"
                    logger.error(f"{log_prefix} Sub-trade {i+1} FAILED. Reason: {last_error}. Result: {trade_result_tuple}")

            # --- Execute Remainder Lot Trade ---
            if remainder_lot > 0:
                current_exec_tp = numeric_tps[-1]
                trade_comment = f"TB SigID {message_id} Seq {total_trades_to_open}/{total_trades_to_open} (Rem)"
                logger.info(f"{log_prefix} Executing remainder trade {total_trades_to_open}/{total_trades_to_open}: Vol={remainder_lot}, TP={current_exec_tp}")
                trade_result_tuple = mt5_executor.execute_trade(
                    action=action, symbol=trade_symbol, order_type=determined_order_type,
                    volume=remainder_lot, price=exec_price, sl=exec_sl, tp=current_exec_tp,
                    comment=trade_comment
                )
                trade_result, actual_exec_price = trade_result_tuple if trade_result_tuple else (None, None)

                if trade_result and trade_result.retcode == mt5.TRADE_RETCODE_DONE:
                    ticket = trade_result.order
                    executed_tickets_info.append({'ticket': ticket, 'vol': remainder_lot, 'tp': current_exec_tp})
                    successful_trades += 1
                    logger.info(f"{log_prefix} Remainder sub-trade executed successfully. Ticket: {ticket}")
                    # Store individual trade info
                    final_entry_price = actual_exec_price if actual_exec_price is not None else exec_price
                    open_time = datetime.now(timezone.utc)
                    trade_info = {
                        'ticket': ticket, 'symbol': trade_symbol, 'open_time': open_time,
                        'original_msg_id': message_id, 'entry_price': final_entry_price,
                        'initial_sl': exec_sl, 'original_volume': remainder_lot,
                        'all_tps': [current_exec_tp], 'tp_strategy': tp_strategy,
                        'next_tp_index': 0, 'assigned_tp': current_exec_tp, 'tsl_active': False,
                        'sequence_info': f"Seq {total_trades_to_open}/{total_trades_to_open} (Rem)"
                    }
                    if state_manager:
                        state_manager.add_active_trade(trade_info, auto_tp_applied=False)
                        if config.getboolean('AutoSL', 'enable_auto_sl', fallback=False) and exec_sl is None:
                            state_manager.mark_trade_for_auto_sl(ticket)
                    else: logger.error(f"Cannot store active trade info: StateManager not initialized.")
                else:
                    failed_trades += 1
                    error_comment = getattr(trade_result, 'comment', 'Unknown Error') if trade_result else 'None Result'
                    last_error = f"{error_comment} (Code: {getattr(trade_result, 'retcode', 'N/A')})"
                    logger.error(f"{log_prefix} Remainder sub-trade FAILED. Reason: {last_error}. Result: {trade_result_tuple}")

            # --- Report Multi-Trade Market/Stop Result ---
            if successful_trades > 0:
                status_title = f"✅ Multi-Trade Executed ({successful_trades}/{total_trades_to_open} OK)" if failed_trades == 0 else f"⚠️ Multi-Trade Partially Executed ({successful_trades}/{total_trades_to_open} OK)"
                status_message = f"{status_title} <code>[MsgID: {message_id}]</code>\n"
                status_message += f"<b>Symbol:</b> <code>{trade_symbol}</code> | <b>Total Vol:</b> <code>{lot_size}</code>\n"
                status_message += f"<b>SL:</b> {'<code>'+str(exec_sl)+'</code>' if exec_sl else '<i>None</i>'}\n"
                status_message += "<b>Trades Opened:</b>\n"
                for idx, trade in enumerate(executed_tickets_info):
                    status_message += f"  <code>{idx+1}. Ticket: {trade['ticket']}, Vol: {trade['vol']}, TP: {trade['tp']}</code>\n"
                if failed_trades > 0:
                    status_message += f"<b>Failures:</b> {failed_trades} trade(s) failed. Last Error: {last_error}\n"
                await telegram_sender.send_message(status_message, parse_mode='html')
                if debug_channel_id: await telegram_sender.send_message(f"{log_prefix} Multi-trade execution summary:\n{status_message}", target_chat_id=debug_channel_id, parse_mode='html')
                duplicate_checker.add_processed_id(message_id)
            else:
                # All trades failed
                status_message = f"❌ <b>Multi-Trade Execution FAILED</b> <code>[MsgID: {message_id}]</code>\n<b>Reason:</b> All {total_trades_to_open} sub-trades failed. Last Error: {last_error}"
                logger.error(f"{log_prefix} All {total_trades_to_open} sub-trades failed execution.")
                duplicate_checker.add_processed_id(message_id)
                await telegram_sender.send_message(status_message, parse_mode='html')
                if debug_channel_id: await telegram_sender.send_message(f"❌ {log_prefix} All sub-trades failed.\n{status_message}", target_chat_id=debug_channel_id, parse_mode='html')

        else:
            # --- Strategy 3: Original Single Trade Execution Logic ---
            logger.info(f"{log_prefix} Executing as single trade. Vol={lot_size}, TP={exec_tp}")
            # Note: exec_price here could be None (for Market) or a specific price (for single Pending)
            # The determined_order_type dictates how execute_trade handles it.
            trade_result_tuple = mt5_executor.execute_trade(
                action=action, symbol=trade_symbol, order_type=determined_order_type,
                volume=lot_size, price=exec_price, sl=exec_sl, tp=exec_tp,
                comment=f"TB SigID {message_id}"
            )
            trade_result, actual_exec_price = trade_result_tuple if trade_result_tuple else (None, None)

            if trade_result and trade_result.retcode == mt5.TRADE_RETCODE_DONE:
                ticket = trade_result.order
                open_time = datetime.now(timezone.utc)
                order_type_str_map = { mt5.ORDER_TYPE_BUY: "Market BUY", mt5.ORDER_TYPE_SELL: "Market SELL", mt5.ORDER_TYPE_BUY_LIMIT: "BUY LIMIT", mt5.ORDER_TYPE_SELL_LIMIT: "SELL LIMIT", mt5.ORDER_TYPE_BUY_STOP: "BUY STOP", mt5.ORDER_TYPE_SELL_STOP: "SELL STOP" }
                order_type_str = order_type_str_map.get(determined_order_type, f"Type {determined_order_type}")
                # Use actual_exec_price if available (for market orders), otherwise use the pending price
                final_entry_price = actual_exec_price if actual_exec_price is not None else exec_price

                entry_str = f"<code>@{final_entry_price}</code>" if final_entry_price is not None else "<code>Market</code>"
                sl_str = f"<code>{exec_sl}</code>" if exec_sl is not None else "<i>None</i>"
                tp_list_str = ', '.join([f"<code>{tp}</code>" if tp != "N/A" else "<i>N/A</i>" for tp in take_profits_list])
                tp_str = f"<code>{exec_tp}</code>" if exec_tp is not None else "<i>None</i>" # TP set on this single order
                auto_tp_label = " (Auto)" if auto_tp_applied else ""
                symbol_str = f"<code>{trade_symbol.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')}</code>"
                lot_str = f"<code>{lot_size}</code>"
                ticket_str = f"<code>{ticket}</code>"
                type_str = f"<code>{order_type_str.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')}</code>"

                status_message = f"""✅ <b>Trade Executed</b> <code>[MsgID: {message_id}]</code>

<b>Ticket:</b> {ticket_str}
<b>Type:</b> {type_str}
<b>Symbol:</b> {symbol_str}
<b>Volume:</b> {lot_str}
<b>Entry:</b> {entry_str}
<b>SL:</b> {sl_str} | <b>TP(s):</b> {tp_list_str} (Initial: {tp_str}{auto_tp_label})"""
                logger.info(f"{log_prefix} Trade executed successfully. Ticket: {ticket}")
                await telegram_sender.send_message(status_message, parse_mode='html')

                if debug_channel_id:
                    debug_msg_exec_success = f"✅ {log_prefix} Trade Executed Successfully.\n<b>Ticket:</b> <code>{ticket}</code>\n<b>Type:</b> <code>{order_type_str}</code>\n<b>Symbol:</b> <code>{trade_symbol}</code>\n<b>Volume:</b> <code>{lot_size}</code>\n<b>Entry:</b> {entry_str}\n<b>SL:</b> {sl_str}\n<b>TP(s):</b> {tp_list_str} (Initial: {tp_str}{auto_tp_label})"
                    await telegram_sender.send_message(debug_msg_exec_success, target_chat_id=debug_channel_id, parse_mode='html')

                # Store trade info
                trade_info = {
                    'ticket': ticket, 'symbol': trade_symbol, 'open_time': open_time,
                    'original_msg_id': message_id, 'entry_price': final_entry_price,
                    'initial_sl': exec_sl, 'original_volume': lot_size,
                    'all_tps': take_profits_list, # Store original full list for reference
                    'tp_strategy': tp_strategy, 'next_tp_index': 0, # May not be used if single TP
                    'assigned_tp': exec_tp, # Store the TP actually set on this trade
                    'tsl_active': False,
                    'is_pending': determined_order_type not in [mt5.ORDER_TYPE_BUY, mt5.ORDER_TYPE_SELL] # Mark if pending
                }
                if state_manager:
                    state_manager.add_active_trade(trade_info, auto_tp_applied=auto_tp_applied)
                    if config.getboolean('AutoSL', 'enable_auto_sl', fallback=False) and exec_sl is None and not trade_info['is_pending']:
                        # Only mark for AutoSL if it's not pending and has no SL
                        state_manager.mark_trade_for_auto_sl(ticket)
                else: logger.error(f"Cannot store active trade info: StateManager not initialized.")

                duplicate_checker.add_processed_id(message_id) # Mark as processed only on success

            else: # Single Execution failed
                error_comment = getattr(trade_result, 'comment', 'Unknown Error') if trade_result else 'None Result (Check Logs)'
                error_code = getattr(trade_result, 'retcode', 'N/A') if trade_result else 'N/A'
                safe_comment = str(error_comment).replace('&', '&amp;').replace('<', '<').replace('>', '>')
                safe_code = str(error_code).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                status_message = f"❌ <b>Trade Execution FAILED</b> <code>[MsgID: {message_id}]</code>\n<b>Reason:</b> {safe_comment} (Code: <code>{safe_code}</code>)"
                logger.error(f"{log_prefix} Trade execution failed. Full Result: {trade_result_tuple}")
                duplicate_checker.add_processed_id(message_id) # Mark as processed even on failure
                await telegram_sender.send_message(status_message, parse_mode='html')
                if debug_channel_id:
                    request_str = trade_result.request if trade_result else 'N/A'
                    debug_msg_exec_fail = f"❌ {log_prefix} Trade Execution FAILED.\n<b>Reason:</b> {safe_comment} (Code: <code>{safe_code}</code>)\n<b>Request:</b> <pre>{request_str}</pre>"
                    await telegram_sender.send_message(debug_msg_exec_fail, target_chat_id=debug_channel_id)

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

async def process_update(analysis_result, event, state_manager: StateManager,
                         signal_analyzer: SignalAnalyzer, mt5_executor: MT5Executor,
                         telegram_sender: TelegramSender, duplicate_checker: DuplicateChecker,
                         config, log_prefix, llm_context, image_data):
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
                update_data = analysis_result.get('data') # Use data from initial analysis
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

                if update_analysis_result and update_analysis_result.get('type') == 'update':
                    update_data = update_analysis_result.get('data')
                    logger.debug(f"{log_prefix} Using update_data from edit/reply re-analysis: {update_data}")
                    # Allow LLM to override target based on index if provided in update analysis
                    target_index_edit = update_data.get('target_trade_index')
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

        # --- Apply the Update if a target and valid update data were found ---
        logger.debug(f"{log_prefix} Checking if update should be applied: is_update_attempt={is_update_attempt}, target_trade_info_exists={target_trade_info is not None}, update_data_exists={update_data is not None}")
        if is_update_attempt and target_trade_info and update_data:
            ticket_to_update = target_trade_info['ticket']
            update_type = update_data.get('update_type', 'unknown')
            new_sl_val = update_data.get('new_stop_loss', 'N/A')
            # Get list of new TPs for updates
            new_tp_list = update_data.get('new_take_profits', ['N/A'])
            logger.debug(f"{log_prefix} Preparing to apply update. Ticket: {ticket_to_update}, Update Type: {update_type}, New SL Val: {new_sl_val}, New TP List: {new_tp_list}")
            # TODO: Add handling for close_volume, close_percentage if implementing partial closes
            mod_success = False
            status_message_mod = ""
            action_description = "Unknown Update"
            new_sl = None
            new_tp = None

            logger.info(f"{log_prefix} Processing update type '{update_type}' for ticket {ticket_to_update}")
            entry_price_str = f"@{target_trade_info.get('entry_price')}" if target_trade_info.get('entry_price') is not None else "Market" # For status messages

            if update_type == "modify_sltp" or update_type == "move_sl":
                action_description = "Modify SL/TP"
                try:
                    if new_sl_val != "N/A": new_sl = float(new_sl_val)
                    # For modify, we only use the *first* TP from the list for now
                    # Multi-TP modification logic could be added later if needed
                    if new_tp_list and new_tp_list[0] != "N/A":
                        new_tp = float(new_tp_list[0])
                except (ValueError, TypeError):
                     logger.warning(f"{log_prefix} Invalid numeric SL/TP value provided for modify_sltp/move_sl: SL='{new_sl_val}', TPs='{new_tp_list}'")
                     status_message_mod = f"⚠️ <b>Update Warning</b> <code>[MsgID: {message_id}]</code> (Ticket: <code>{ticket_to_update}</code>, Entry: {entry_price_str}). Invalid SL/TP value provided."
                else:
                     if new_sl is not None or new_tp is not None:
                         logger.info(f"{log_prefix} Attempting to modify MT5 order/position {ticket_to_update} with new SL={new_sl}, TP={new_tp}")
                         logger.debug(f"{log_prefix} Calling mt5_executor.modify_trade(ticket={ticket_to_update}, sl={new_sl}, tp={new_tp})")
                         mod_success = mt5_executor.modify_trade(ticket_to_update, sl=new_sl, tp=new_tp)
                         logger.debug(f"{log_prefix} mt5_executor.modify_trade result: {mod_success}")
                     else:
                         logger.info(f"{log_prefix} No valid new SL or TP found for modify_sltp/move_sl update.")
                         status_message_mod = f"ℹ️ <b>Update Info</b> <code>[MsgID: {message_id}]</code> (Ticket: <code>{ticket_to_update}</code>, Entry: {entry_price_str}). No valid SL/TP values found in message."

            elif update_type == "set_be":
                action_description = "Set SL to Breakeven"
                logger.info(f"{log_prefix} Attempting to set SL to Breakeven for ticket {ticket_to_update}")
                logger.debug(f"{log_prefix} Calling mt5_executor.modify_sl_to_breakeven(ticket={ticket_to_update})")
                mod_success = mt5_executor.modify_sl_to_breakeven(ticket_to_update)
                logger.debug(f"{log_prefix} mt5_executor.modify_sl_to_breakeven result: {mod_success}")

            elif update_type == "close_trade":
                action_description = "Close Trade"
                logger.info(f"{log_prefix} Attempting to close trade for ticket {ticket_to_update}")
                logger.debug(f"{log_prefix} Calling mt5_executor.close_position(ticket={ticket_to_update})")
                mod_success = mt5_executor.close_position(ticket_to_update) # Close full position
                logger.debug(f"{log_prefix} mt5_executor.close_position result: {mod_success}")

            elif update_type == "cancel_pending":
                action_description = "Cancel Pending Order"
                logger.info(f"{log_prefix} Attempting to cancel pending order for ticket {ticket_to_update}")
                logger.debug(f"{log_prefix} Calling mt5_executor.delete_pending_order(ticket={ticket_to_update})")
                mod_success = mt5_executor.delete_pending_order(ticket_to_update)
                logger.debug(f"{log_prefix} mt5_executor.delete_pending_order result: {mod_success}")

            # TODO: Add elif for "partial_close" if implemented

            elif update_type == "unknown":
                 logger.warning(f"{log_prefix} Update type classified as 'unknown'. No action taken.")
                 status_message_mod = f"❓ <b>Update Unclear</b> <code>[MsgID: {message_id}]</code> (Ticket: <code>{ticket_to_update}</code>, Entry: {entry_price_str}). Could not determine specific action from message."

            # --- Generate Status Message based on outcome ---
            if not status_message_mod: # Only generate if no specific info/warning message was set above
                safe_action_desc = str(action_description).replace('&', '&amp;').replace('<', '<').replace('>', '>')
                details = ""
                if update_type in ["modify_sltp", "move_sl", "set_be"]:
                    # Fetch actual SL/TP after modification attempt? For now, display intended.
                    sl_update_str = f"New SL: <code>{new_sl}</code>" if new_sl is not None else "<i>SL Unchanged</i>"
                    if update_type == "set_be": sl_update_str = "SL to BE"
                    # Display first TP for update status
                    tp_update_str = f"New TP: <code>{new_tp}</code>" if new_tp is not None else "<i>TP Unchanged</i>"
                    details = f"\n<b>Details:</b> {sl_update_str}, {tp_update_str}"

                if mod_success:
                    status_message_mod = f"""✅ <b>{safe_action_desc} Successful</b> <code>[MsgID: {message_id}]</code>
<b>Ticket:</b> <code>{ticket_to_update}</code> (Entry: {entry_price_str}){details}"""
                    logger.info(f"{log_prefix} {action_description} successful for ticket {ticket_to_update}.")
                else:
                    status_message_mod = f"❌ <b>{safe_action_desc} FAILED</b> <code>[MsgID: {message_id}]</code> (Ticket: <code>{ticket_to_update}</code>, Entry: {entry_price_str}). Check logs."
                    logger.error(f"{log_prefix} {action_description} failed for ticket {ticket_to_update}.")

            # Send the final status message
            await telegram_sender.send_message(status_message_mod, parse_mode='html')
            if debug_channel_id:
                debug_msg_update_result = f"🔄 {log_prefix} Update Action Result:\n{status_message_mod}"
                await telegram_sender.send_message(debug_msg_update_result, target_chat_id=debug_channel_id)

            # Mark original message as processed if update came from new message analysis
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