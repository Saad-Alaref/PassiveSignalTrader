import asyncio
import logging
import signal
import sys
import os
import MetaTrader5 as mt5 # Import the MT5 library
from telethon import events # For type hinting in handler
from datetime import datetime, timezone, timedelta # For AutoSL timing
import html # For escaping HTML in debug messages
import json # For debug logging analysis results
from typing import Optional # Added for type hinting

# Import local modules
from .state_manager import StateManager # Use relative import
from .logger_setup import setup_logging # Use relative import
# from .config_loader import load_config # No longer needed directly
from .config_service import config_service, ConfigService # Import the service instance and class
from .mt5_connector import MT5Connector # Use relative import
from .mt5_data_fetcher import MT5DataFetcher # Use relative import
from .llm_interface import LLMInterface # Use relative import
from .signal_analyzer import SignalAnalyzer # Use relative import
from .duplicate_checker import DuplicateChecker # Use relative import
from .decision_logic import DecisionLogic # Use relative import
from .trade_calculator import TradeCalculator # Use relative import
from .mt5_executor import MT5Executor # Use relative import
from .telegram_reader import TelegramReader # Use relative import
from .telegram_sender import TelegramSender # Use relative import
from .trade_manager import TradeManager # Use relative import
from . import event_processor # Use relative import
from .trade_closure_monitor import periodic_trade_closure_monitor_task
from .daily_summary import daily_summary_task
# from .confirmation_updater import confirmation_updater_task # Task defined below

# --- Global Variables ---
logger = None # Will be configured in main
# shared_config = None # Replaced by config_service instance
config_file_path = 'config/config.ini' # Keep for potential direct path use if needed
last_config_mtime = 0 # Track modification time
# config_lock = asyncio.Lock() # Lock might not be needed if service handles internal state safely
config_reloader_task: Optional[asyncio.Task] = None # Task handle for the reloader
periodic_monitor_task: Optional[asyncio.Task] = None # Task handle for the periodic MT5 monitor
confirmation_update_task: Optional[asyncio.Task] = None # Task handle for confirmation message updates
daily_summary_task_handle: Optional[asyncio.Task] = None # Task handle for daily summary

# --- Core Components (Initialized in main) ---
mt5_connector: Optional[MT5Connector] = None
mt5_fetcher: Optional[MT5DataFetcher] = None
llm_interface: Optional[LLMInterface] = None
signal_analyzer: Optional[SignalAnalyzer] = None
duplicate_checker: Optional[DuplicateChecker] = None
decision_logic: Optional[DecisionLogic] = None
trade_calculator: Optional[TradeCalculator] = None
mt5_executor: Optional[MT5Executor] = None
telegram_reader: Optional[TelegramReader] = None
telegram_sender: Optional[TelegramSender] = None
state_manager: Optional[StateManager] = None
trade_manager: Optional[TradeManager] = None

# --- Signal Handling for Graceful Shutdown ---
def handle_shutdown_signal(sig, frame):
    """Initiates graceful shutdown when SIGINT or SIGTERM is received."""
    global periodic_monitor_task, config_reloader_task, confirmation_update_task, daily_summary_task_handle, telegram_sender, logger # Added telegram_sender
    logger.info(f"Received shutdown signal: {sig}. Initiating graceful shutdown...")

    # Send Telegram shutdown notification
    if telegram_sender:
        try:
            shutdown_time = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
            msg = (
                f"🛑 <b>AI-Trader Bot Stopped</b>\n"
                f"<b>Status:</b> <code>Stopped</code>\n"
                f"<b>Shutdown Time:</b> <code>{shutdown_time}</code>\n"
                f"<i>The bot has been stopped (manual Ctrl+C or system signal).</i>"
            )
            # Use asyncio to ensure the message is sent
            import asyncio
            loop = None
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
            if loop and loop.is_running():
                loop.create_task(telegram_sender.send_message(msg, parse_mode='html'))
            else:
                loop.run_until_complete(telegram_sender.send_message(msg, parse_mode='html'))
            logger.info("Sent Telegram shutdown notification message.")
        except Exception as shutdown_notify_err:
            logger.error(f"Failed to send Telegram shutdown notification: {shutdown_notify_err}")

    # Cancel tasks
    if daily_summary_task_handle and not daily_summary_task_handle.done():
        logger.info("Signal handler: Cancelling daily summary task...")
        daily_summary_task_handle.cancel()
    if confirmation_update_task and not confirmation_update_task.done():
        logger.info("Signal handler: Cancelling confirmation updater task...")
        confirmation_update_task.cancel()
    if periodic_monitor_task and not periodic_monitor_task.done():
        logger.info("Signal handler: Cancelling periodic monitor task...")
        periodic_monitor_task.cancel()
    if config_reloader_task and not config_reloader_task.done():
        logger.info("Signal handler: Cancelling config reloader task...")
        config_reloader_task.cancel()

    # Disconnect Telegram reader client if running
    if telegram_reader and telegram_reader.client and telegram_reader.client.is_connected():
        logger.info("Signal handler: Disconnecting Telegram reader client...")
        # Use disconnect() which is generally safer than stop() from sync context
        try:
             # Schedule disconnect to run on the loop
             asyncio.create_task(telegram_reader.client.disconnect())
        except Exception as e:
            logger.error(f"Signal handler: Error scheduling client.disconnect(): {e}")
    else:
        logger.info("Signal handler: Telegram reader client not available or not connected.")

# --- Main Telegram Event Handler ---
async def handle_telegram_event(event):
    # Make all necessary components accessible
    # Use config_service instead of shared_config
    global logger, config_service, state_manager, trade_manager, signal_analyzer, \
           duplicate_checker, decision_logic, trade_calculator, mt5_executor, \
           telegram_sender, mt5_fetcher

    # AutoSL, AutoBE, and TP checks are now handled by the periodic task

    # Add message to history via StateManager
    if state_manager:
        state_manager.add_message_to_history(event)

    """
    The main callback function passed to TelegramMonitor.
    Handles new messages and edits.
    """

    # Outer try...except for unexpected errors in the handler itself
    try:
        message_id = event.id
        # chat_id = event.chat_id # Not currently used directly in handler
        message_text = getattr(event, 'text', '')
        is_edit = isinstance(event, events.MessageEdited.Event)
        reply_to_msg_id = getattr(event, 'reply_to_msg_id', None)

        log_prefix = f"[MsgID: {message_id}{' (Edit)' if is_edit else ''}{f' (Reply to {reply_to_msg_id})' if reply_to_msg_id else ''}]"
        logger.info(f"{log_prefix} Received event. Text: '{message_text[:80]}...'")
        # Send initial debug message to debug channel if configured
        debug_channel_id = getattr(telegram_sender, 'debug_target_channel_id', None)
        if debug_channel_id:
            safe_text = html.escape(message_text)
            debug_msg_start = f"🔎 {log_prefix} Processing event...\n<b>Text:</b><pre>{safe_text}</pre>"
            await telegram_sender.send_message(debug_msg_start, target_chat_id=debug_channel_id, parse_mode='html')

        # --- Pre-filter Messages from Own Sender Bot ---
        sender_id = event.sender_id
        if telegram_sender and telegram_sender.sender_bot_id:
            if sender_id == telegram_sender.sender_bot_id:
                logger.info(f"{log_prefix} Ignoring message from own sender bot (ID: {sender_id}).")
                if debug_channel_id:
                    debug_msg_ignore_self = f"🚫 {log_prefix} Ignoring own message (Sender ID: {sender_id})."
                    await telegram_sender.send_message(debug_msg_ignore_self, target_chat_id=debug_channel_id)
                return # Stop processing this message
        else:
            logger.warning(f"{log_prefix} Could not verify sender bot ID to filter own messages.")
        # --- End Pre-filter ---


        # 1. Duplicate Check (Only for new, non-reply messages)
        if not is_edit and not reply_to_msg_id and duplicate_checker.is_duplicate(message_id):
            logger.info(f"{log_prefix} Message ID already processed. Ignoring.")
            if debug_channel_id:
                debug_msg_duplicate = f"🚫 {log_prefix} Ignoring duplicate message."
                await telegram_sender.send_message(debug_msg_duplicate, target_chat_id=debug_channel_id)
            return
        # 2. Analyze Message Type and Content (New Messages)
        analysis_result = None # Holds {'type': '...', 'data': ...} or similar
        # --- Gather Context for LLM via StateManager ---
        llm_context = {}
        if state_manager:
            llm_context = state_manager.get_llm_context(mt5_fetcher)
        else:
            logger.error("StateManager not initialized, cannot gather LLM context.")
        # --- End Context Gathering ---

        if not is_edit and not reply_to_msg_id:
            try: # Add try block around analysis
                # --- Call Analyzer with Context ---
                # Always pass None for image_data to disable image analysis
                analysis_result = signal_analyzer.analyze(message_text, context=llm_context)
                analysis_type = analysis_result.get('type')
                logger.info(f"{log_prefix} Signal analysis result type: {analysis_type}")
                # Send analysis debug message
                # import json # Moved to top
                data_obj = analysis_result.get('data')
                if data_obj:
                    import dataclasses
                    if dataclasses.is_dataclass(data_obj):
                        analysis_data_str = json.dumps(dataclasses.asdict(data_obj), indent=2)
                    else:
                        analysis_data_str = json.dumps(data_obj, indent=2)
                else:
                    analysis_data_str = "None"
                if debug_channel_id:
                    debug_msg_analysis = f"🧠 {log_prefix} LLM Analysis Result:\n<b>Type:</b> <code>{analysis_type}</code>\n<b>Data:</b>\n<pre>{analysis_data_str}</pre>"
                    await telegram_sender.send_message(debug_msg_analysis, target_chat_id=debug_channel_id, parse_mode='html')
                # Handle 'ignore' type immediately
                if analysis_result.get('type') == 'ignore':
                    logger.info(f"{log_prefix} Ignored (Not actionable by LLM).")
                    duplicate_checker.add_processed_id(message_id)
                    if debug_channel_id:
                        debug_msg_llm_ignore = f"🧐 {log_prefix} Ignored by LLM (Not actionable)."
                        await telegram_sender.send_message(debug_msg_llm_ignore, target_chat_id=debug_channel_id)
                    return # Stop processing
                # Handle 'update' type (will be processed later in step 4)
                elif analysis_result.get('type') == 'update':
                    logger.info(f"{log_prefix} Classified as potential update. Symbol hint: {analysis_result.get('symbol')}. Will process in update logic.")
                    # Don't return yet, fall through to update logic section
                # Handle 'new_signal' type (will be processed in step 3)
                elif analysis_result.get('type') == 'new_signal':
                    logger.info(f"{log_prefix} Classified as new signal. Data: {analysis_result.get('data')}")
                    # Don't return yet, fall through to execution logic section
                else: # Should not happen if analyzer returns correctly
                     logger.error(f"{log_prefix} Unknown analysis result type: {analysis_result}. Ignoring.")
                     duplicate_checker.add_processed_id(message_id)
                     if debug_channel_id:
                         debug_msg_unknown_type = f"❓ {log_prefix} Unknown LLM analysis type: {analysis_result}. Ignored."
                         await telegram_sender.send_message(debug_msg_unknown_type, target_chat_id=debug_channel_id)
                     return
            except Exception as analysis_err:
                 logger.error(f"{log_prefix} Error during signal analysis: {analysis_err}", exc_info=True)
                 # Mark as processed to avoid retrying on error
                 duplicate_checker.add_processed_id(message_id)
                 if debug_channel_id:
                     # Removed redundant import html here
                     debug_msg_analysis_err = f"🆘 {log_prefix} Error during signal analysis:\n<pre>{html.escape(str(analysis_err))}</pre>"
                     await telegram_sender.send_message(debug_msg_analysis_err, target_chat_id=debug_channel_id, parse_mode='html')
                 return # Stop processing on analysis error

        # 3. Handle New Signal Execution (via Event Processor)
        if analysis_result and analysis_result.get('type') == 'new_signal':
            await event_processor.process_new_signal(
                signal_data=analysis_result.get('data'),
                message_id=message_id,
                state_manager=state_manager,
                decision_logic=decision_logic,
                trade_calculator=trade_calculator,
                mt5_executor=mt5_executor,
                telegram_sender=telegram_sender,
                duplicate_checker=duplicate_checker,
                config_service_instance=config_service, # Pass service instance
                log_prefix=log_prefix,
                mt5_fetcher=mt5_fetcher # Pass fetcher
            )

        # 4. Handle Updates (via Event Processor)
        # This covers both new messages analyzed as 'update' and edits/replies
        elif (analysis_result and analysis_result.get('type') == 'update') or is_edit or reply_to_msg_id:
             await event_processor.process_update(
                 analysis_result=analysis_result, # Can be None if it's an edit/reply not analyzed yet
                 event=event,
                 state_manager=state_manager,
                 signal_analyzer=signal_analyzer, # Needed for re-analysis inside process_update
                 mt5_executor=mt5_executor,
                 telegram_sender=telegram_sender,
                 duplicate_checker=duplicate_checker, # Needed for marking processed inside process_update
                 config_service_instance=config_service, # Pass service instance
                 log_prefix=log_prefix,
                 llm_context=llm_context, # Pass context for potential re-analysis
                 # image_data=image_data # Removed image data passing
             )


    # Outer try...except for unexpected errors in the handler itself
    except Exception as handler_err:
        logger.error(f"{log_prefix} Unhandled exception in event handler: {handler_err}", exc_info=True)
        # Send unhandled error debug message
        debug_msg_handler_err = f"🆘🆘🆘 {log_prefix} UNHANDLED EXCEPTION in event handler:\n<pre>{html.escape(str(handler_err))}</pre>"
        if debug_channel_id: await telegram_sender.send_message(debug_msg_handler_err, target_chat_id=debug_channel_id, parse_mode='html')

# --- Callback Query Handler (REMOVED - Moved to TelegramSender) ---

# --- Config Reloader Task ---
async def config_reloader_task_func(interval_seconds=30):
    """Periodically checks config file for changes and triggers reload via ConfigService."""
    global config_service, config_file_path, last_config_mtime, logger # Remove shared_config, config_lock

    # Construct absolute path relative to this script's location
    # Assuming main.py is in src/, config is in ../config/
    script_dir = os.path.dirname(__file__)
    abs_config_path = os.path.abspath(os.path.join(script_dir, '..', config_file_path))
    logger.info(f"Config reloader watching file: {abs_config_path}")

    while True:
        try:
            await asyncio.sleep(interval_seconds)
            logger.debug(f"Checking config file '{abs_config_path}' for modifications...")

            if not os.path.exists(abs_config_path):
                logger.warning(f"Config file '{abs_config_path}' not found during check. Skipping reload.")
                continue

            current_mtime = os.path.getmtime(abs_config_path)

            if current_mtime > last_config_mtime:
                logger.info(f"Detected change in config file '{abs_config_path}'. Triggering reload via ConfigService...")
                try:
                    # Tell the service to reload its internal config
                    config_service.reload_config()
                    last_config_mtime = current_mtime # Update mtime only on successful reload by service
                    logger.info(f"ConfigService successfully reloaded configuration from '{config_file_path}'.")
                except Exception as reload_err:
                    logger.error(f"ConfigService failed to reload config from '{config_file_path}': {reload_err}. Keeping previous configuration.")
                    # Do not update last_config_mtime if reload failed
            else:
                 logger.debug("No config file changes detected.")

        except asyncio.CancelledError:
            logger.info("Config reloader task cancelled.")
            break # Exit the loop on cancellation
        except Exception as e:
            logger.error(f"Error in config reloader task: {e}", exc_info=True)
            # Avoid rapid errors, wait longer before next check
            await asyncio.sleep(interval_seconds * 2)


# --- Periodic Task for Trade Management (AutoSL, AutoBE, TP Checks) ---
async def periodic_mt5_monitor_task(interval_seconds=60):
    """Periodically fetches positions and calls trade management functions, including onboarding manual trades."""
    global trade_manager, state_manager, logger, mt5_connector, config_service # Use config_service

    logger.info(f"Starting periodic MT5 monitor task (initial interval: {interval_seconds}s).")
    while True:
        try:
            # Read interval dynamically inside the loop for hot-reloading
            current_interval = config_service.getint('Misc', 'periodic_check_interval_seconds', fallback=60)
            if current_interval <= 0:
                logger.warning("Periodic monitor interval is zero or negative. Skipping this cycle.")
                await asyncio.sleep(60)
                continue

            # --- Fetch all open positions and orders from MT5 ---
            open_positions = mt5.positions_get() or []
            open_orders = mt5.orders_get() or []
            open_tickets = {p.ticket for p in open_positions} | {o.ticket for o in open_orders}

            # --- Get tracked tickets from StateManager ---
            tracked_trades = state_manager.get_active_trades() or []
            tracked_tickets = {t.ticket for t in tracked_trades}

            # --- Detect new/manual trades ---
            new_manual_tickets = open_tickets - tracked_tickets
            for ticket in new_manual_tickets:
                # Try to fetch full details from MT5 (positions first, then orders)
                mt5_trade = next((p for p in open_positions if p.ticket == ticket), None)
                if not mt5_trade:
                    mt5_trade = next((o for o in open_orders if o.ticket == ticket), None)
                if not mt5_trade:
                    logger.error(f"[ManualTradeOnboarding] Could not fetch details for ticket {ticket}, skipping onboarding.")
                    continue

                # Prepare trade_info_data dict for StateManager
                trade_info_data = {
                    'ticket': mt5_trade.ticket,
                    'symbol': mt5_trade.symbol,
                    'open_time': getattr(mt5_trade, 'time', None),
                    'original_msg_id': None,  # Not from Telegram
                    'entry_price': getattr(mt5_trade, 'price_open', None),
                    'initial_sl': getattr(mt5_trade, 'sl', None),
                    'original_volume': getattr(mt5_trade, 'volume', None),
                    'all_tps': [getattr(mt5_trade, 'tp', None)] if getattr(mt5_trade, 'tp', None) else [],
                    'assigned_tp': getattr(mt5_trade, 'tp', None),
                    'is_pending': hasattr(mt5_trade, 'type') and mt5_trade.type in [mt5.ORDER_TYPE_BUY_LIMIT, mt5.ORDER_TYPE_SELL_LIMIT, mt5.ORDER_TYPE_BUY_STOP, mt5.ORDER_TYPE_SELL_STOP],
                    'tsl_active': False,  # Will be handled by trade_manager
                    'auto_tp_applied': False,
                    'sequence_info': None,
                    'auto_sl_pending_timestamp': None,
                    'source': 'manual',  # Mark as manual
                }
                logger.info(f"[ManualTradeOnboarding] Onboarding manual trade: {trade_info_data}")
                state_manager.add_active_trade(trade_info_data, auto_tp_applied=False)

                # --- DEBUG: Log config and trade state before applying management ---
                logger.info(f"[ManualTradeOnboarding][DEBUG] enable_auto_sl={config_service.getboolean('AutoSL', 'enable_auto_sl', fallback=False)} enable_auto_tp={config_service.getboolean('AutoTP', 'enable_auto_tp', fallback=False)} enable_auto_be={config_service.getboolean('AutoBE', 'enable_auto_be', fallback=False)} enable_trailing_stop={config_service.getboolean('TrailingStop', 'enable_trailing_stop', fallback=False)}")
                logger.info(f"[ManualTradeOnboarding][DEBUG] Trade SL before: {getattr(mt5_trade, 'sl', None)}, TP before: {getattr(mt5_trade, 'tp', None)}")

                trade_info = next((t for t in state_manager.get_active_trades() if t.ticket == mt5_trade.ticket), None)
                if trade_info and not trade_info.is_pending and hasattr(mt5_trade, 'profit'):
                    # Apply Auto SL if enabled and SL is missing (do not overwrite existing SL)
                    if config_service.getboolean('AutoSL', 'enable_auto_sl', fallback=False) and (getattr(mt5_trade, 'sl', None) in [None, 0.0]):
                        logger.info(f"[ManualTradeOnboarding][DEBUG] Attempting to apply Auto SL...")
                        await trade_manager.check_and_apply_auto_sl(mt5_trade, trade_info)
                    # Apply Auto TP if enabled and TP is missing (do not overwrite existing TP)
                    if config_service.getboolean('AutoTP', 'enable_auto_tp', fallback=False) and (getattr(mt5_trade, 'tp', None) in [None, 0.0]):
                        logger.info(f"[ManualTradeOnboarding][DEBUG] Attempting to apply Auto TP...")
                        await trade_manager.check_and_apply_auto_tp(mt5_trade, trade_info)
                    # Apply Auto BE if enabled (will only trigger if price condition is met)
                    if config_service.getboolean('AutoBE', 'enable_auto_be', fallback=False):
                        logger.info(f"[ManualTradeOnboarding][DEBUG] Attempting to apply Auto BE...")
                        await trade_manager.check_and_apply_auto_be(mt5_trade, trade_info)
                    # Apply TSL if enabled (will only trigger if price condition is met)
                    if config_service.getboolean('TrailingStop', 'enable_trailing_stop', fallback=False):
                        logger.info(f"[ManualTradeOnboarding][DEBUG] Attempting to apply TSL...")
                        await trade_manager.check_and_apply_trailing_stop(mt5_trade, trade_info)
                logger.info(f"[ManualTradeOnboarding][DEBUG] Trade SL after: {getattr(mt5_trade, 'sl', None)}, TP after: {getattr(mt5_trade, 'tp', None)}")

                # --- Send Telegram notification about the onboarded manual trade ---
                if telegram_sender:
                    try:
                        # Format a message with trade info
                        trade_type = "Pending Order" if trade_info_data['is_pending'] else "Market Position"
                        msg = (
                            f"📥 <b>Manual Trade Onboarded</b>\n"
                            f"<b>Type:</b> {trade_type}\n"
                            f"<b>Ticket:</b> <code>{trade_info_data['ticket']}</code>\n"
                            f"<b>Symbol:</b> <code>{trade_info_data['symbol']}</code>\n"
                            f"<b>Volume:</b> <code>{trade_info_data['original_volume']}</code>\n"
                            f"<b>Entry Price:</b> <code>{trade_info_data['entry_price']}</code>\n"
                            f"<b>SL:</b> <code>{trade_info_data['initial_sl']}</code>\n"
                            f"<b>TP:</b> <code>{trade_info_data['assigned_tp']}</code>\n"
                            f"<b>Source:</b> Manual (MT5)\n"
                            f"<b>Time:</b> <code>{trade_info_data['open_time']}</code>\n"
                        )
                        await telegram_sender.send_message(msg, parse_mode='html')
                        logger.info(f"[ManualTradeOnboarding] Sent Telegram notification for manual trade {trade_info_data['ticket']}")
                    except Exception as notify_err:
                        logger.error(f"[ManualTradeOnboarding] Failed to send Telegram notification for manual trade {trade_info_data['ticket']}: {notify_err}")

            # --- Call trade management routines as before ---
            # Existing logic follows here (unchanged)
            # ...

            await asyncio.sleep(current_interval)

        except asyncio.CancelledError:
            logger.info("Periodic MT5 monitor task cancelled.")
            break # Exit the loop on cancellation
        except Exception as e:
            logger.error(f"Error in periodic MT5 monitor task: {e}", exc_info=True)
            # Avoid rapid errors, wait longer before next check
            error_sleep_interval = current_interval if 'current_interval' in locals() and current_interval > 0 else 60
            await asyncio.sleep(error_sleep_interval * 2)

# --- Confirmation Message Updater Task ---
async def confirmation_updater_task_func(interval_seconds=10):
    """Periodically updates active confirmation messages with current market price."""
    global state_manager, telegram_sender, mt5_fetcher, config_service, logger

    if interval_seconds <= 0:
        logger.info("Confirmation message update interval is zero or negative. Task disabled.")
        return # Don't run the task if interval is invalid

    logger.info(f"Starting confirmation message updater task (Interval: {interval_seconds}s).")
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            if not state_manager or not telegram_sender or not mt5_fetcher:
                logger.warning("[ConfUpdater] Core components not ready. Skipping update cycle.")
                continue

            active_confirmations = state_manager.get_active_confirmations()
            if not active_confirmations:
                # logger.debug("[ConfUpdater] No active confirmations to update.")
                continue

            logger.debug(f"[ConfUpdater] Checking {len(active_confirmations)} active confirmation(s)...")
            now_utc = datetime.now(timezone.utc)
            timeout_minutes = config_service.getint('Trading', 'market_confirmation_timeout_minutes', fallback=3)

            for conf_id, conf_data in active_confirmations.items():
                conf_timestamp = conf_data['timestamp']
                expiry_time = conf_timestamp + timedelta(minutes=timeout_minutes)
                message_id_to_edit = conf_data['message_id']
                chat_id_to_edit_in = conf_data['chat_id'] # Get chat_id
                trade_params = conf_data['trade_details']
                initial_price = conf_data.get('initial_market_price') # Get stored initial price
                symbol = trade_params.get('symbol')
                action = trade_params.get('action') # BUY or SELL

                # Check expiry (StateManager might remove it, but double check)
                if now_utc > expiry_time:
                    logger.info(f"[ConfUpdater] Confirmation {conf_id} seems expired. Skipping update (should be handled by callback).")
                    # Optionally force removal if callback handler missed it?
                    # state_manager.remove_pending_confirmation(conf_id)
                    continue

                if not symbol or not action:
                    logger.warning(f"[ConfUpdater] Skipping update for {conf_id}: Missing symbol or action in trade_details.")
                    continue

                # Fetch current price
                current_price_str = "<i>N/A</i>"
                tick = mt5_fetcher.get_symbol_tick(symbol)
                if tick:
                    current_price_str = f"Ask:<code>{tick.ask}</code> Bid:<code>{tick.bid}</code>"
                else:
                     logger.warning(f"[ConfUpdater] Could not fetch current tick for {symbol} to update confirmation {conf_id}.")
                     # Keep previous "Fetching..." or show N/A? Let's show N/A after first failure.
                     # We need the original message text structure here.

                # Reconstruct the message text (similar to event_processor)
                # This is fragile if the original text format changes.
                # Consider storing the base text format or using a template.
                sl_str_conf = f"<code>{trade_params.get('sl')}</code>" if trade_params.get('sl') is not None else "<i>None</i>"
                tp_str_conf = f"<code>{trade_params.get('tp')}</code>" if trade_params.get('tp') is not None else "<i>None</i>"
                symbol_str_safe = symbol.replace('&', '&amp;').replace('<', '<').replace('>', '>') # Basic escaping
                action_str = "BUY" if action == "BUY" else "SELL" # Assuming action is BUY/SELL string
                # Display initial price based on action
                initial_price_display = "<i>N/A</i>"
                if initial_price is not None:
                     initial_price_display = f"Ask:<code>{initial_price}</code>" if action == "BUY" else f"Bid:<code>{initial_price}</code>"


                # Use the specific ID for the span - This won't work as Telegram doesn't support dynamic IDs/JS
                # We need to replace the whole line.
                # current_price_span = f'<span class="tg-spoiler" id="current-price-{conf_id}"><b>Current Price:</b> {current_price_str}</span>'
                current_price_line = f"<b>Current Price:</b> {current_price_str}"


                updated_text = TelegramSender.format_confirmation_message(
                    trade_params=trade_params,
                    confirmation_id=conf_id,
                    timeout_minutes=timeout_minutes,
                    initial_market_price=initial_price,
                    current_price_str=current_price_line
                )

                # Edit the message using TelegramSender
                if chat_id_to_edit_in:
                    # Recreate the original buttons to pass them to edit_message
                    # This assumes the button structure is always the same Yes/No
                    from telethon.tl.custom import Button # Ensure import
                    original_buttons = [
                        [ Button.inline("✅ Yes", data=f"confirm_yes_{conf_id}"),
                          Button.inline("❌ No", data=f"confirm_no_{conf_id}") ]
                    ]
                    edit_success = await telegram_sender.edit_message(
                        chat_id=chat_id_to_edit_in,
                        message_id=message_id_to_edit,
                        new_text=updated_text,
                        buttons=original_buttons # Pass buttons to preserve them
                    )
                    if not edit_success:
                         logger.warning(f"[ConfUpdater] Failed to edit confirmation message {message_id_to_edit} in chat {chat_id_to_edit_in}.")
                         # Consider removing confirmation from state if edit fails repeatedly?
                else:
                    # This case should ideally not happen if chat_id is stored correctly
                    logger.error(f"[ConfUpdater] Cannot edit message {message_id_to_edit}, chat_id not found in confirmation data for {conf_id}.")


        except asyncio.CancelledError:
            logger.info("Confirmation message updater task cancelled.")
            break
        except Exception as e:
            logger.error(f"Error in confirmation message updater task: {e}", exc_info=True)
            # Avoid rapid errors
            await asyncio.sleep(interval_seconds * 5)


# --- Main Application Function ---
async def run_bot():
    """Initializes and runs the main application components."""
    # Ensure all globals including task handles are declared
    global logger, config_service, mt5_connector, mt5_fetcher, llm_interface, \
           signal_analyzer, duplicate_checker, decision_logic, trade_calculator, \
           mt5_executor, telegram_reader, telegram_sender, state_manager, trade_manager, \
           config_file_path, last_config_mtime, config_reloader_task, periodic_monitor_task, confirmation_update_task, daily_summary_task_handle # Added tasks

    # 1. Load Initial Config into shared_config
    # 1. Initialize Config Service (this loads the config)
    # config_service is already initialized at module level when imported
    if config_service is None:
        print("CRITICAL: ConfigService failed to initialize during import. Check logs. Exiting.", file=sys.stderr)
        sys.exit(1)

    # Set initial modification time for reloader task
    script_dir = os.path.dirname(__file__)
    abs_config_path = os.path.abspath(os.path.join(script_dir, '..', config_file_path))
    if os.path.exists(abs_config_path):
         last_config_mtime = os.path.getmtime(abs_config_path)
    else:
         last_config_mtime = 0
         print(f"Warning: Could not get initial modification time for {abs_config_path}", file=sys.stderr)

    # 2. Setup Logging
    # Use config_service for initial setup
    log_file = config_service.get('Logging', 'log_file', fallback='logs/bot.log')
    log_level = config_service.get('Logging', 'log_level', fallback='INFO')
    # Construct absolute path for log file if it's relative
    if not os.path.isabs(log_file):
        log_file = os.path.join(os.path.dirname(__file__), '..', log_file)
    setup_logging(log_file_path=log_file, log_level_str=log_level)
    logger = logging.getLogger('TradeBot') # Get configured logger
    logger.info("--- Trade Bot Starting ---")

    # 3. Initialize Components
    try:
        # Pass the config_service instance to components
        # Components can now get config values dynamically via the service
        mt5_connector = MT5Connector(config_service) # Pass service
        mt5_fetcher = MT5DataFetcher(mt5_connector) # Initialize fetcher early
        llm_interface = LLMInterface(config_service) # Pass service
        signal_analyzer = SignalAnalyzer(llm_interface, mt5_fetcher, config_service) # Pass service
        # Duplicate checker size likely doesn't need hot-reload
        duplicate_cache_size = config_service.getint('Misc', 'duplicate_cache_size', fallback=10000)
        duplicate_checker = DuplicateChecker(max_size=duplicate_cache_size) # Size likely doesn't need hot-reload
        decision_logic = DecisionLogic(config_service, mt5_fetcher) # Pass service
        trade_calculator = TradeCalculator(config_service, mt5_fetcher) # Pass service
        mt5_executor = MT5Executor(config_service, mt5_connector) # Pass service
        state_manager = StateManager(config_service) # Pass service

        # Initialize TelegramSender *after* components it depends on
        telegram_sender = TelegramSender(config_service, state_manager, mt5_executor, mt5_connector, mt5_fetcher) # Pass service

        # Initialize TelegramReader (remove callback handler argument)
        telegram_reader = TelegramReader(config_service, handle_telegram_event) # Pass service

        # Initialize TradeManager after its dependencies
        trade_manager = TradeManager(config_service, state_manager, mt5_executor, trade_calculator, telegram_sender, mt5_fetcher) # Pass service
    except Exception as e:
        logger.critical(f"Failed to initialize components: {e}", exc_info=True)
        sys.exit(1)

    # 4. Connect to Services
    logger.info("Connecting to MT5...")
    if not mt5_connector.connect():
        logger.critical("Failed to connect to MT5. Exiting.")
        sys.exit(1)
    logger.info("MT5 Connected.")

    # Connect Sender *before* Reader, as Reader might receive messages needing sender actions
    logger.info("Starting Telegram Sender (Bot Account)...")
    sender_started = await telegram_sender.connect() # Connect sender (this now adds the callback handler)
    if not sender_started:
         logger.critical("Failed to start Telegram Sender. Exiting.")
         mt5_connector.disconnect()
         sys.exit(1)
    logger.info("Telegram Sender Started.")

    logger.info("Starting Telegram Reader (User Account)...")
    reader_started = await telegram_reader.start() # Start reader
    if not reader_started:
         logger.critical("Failed to start Telegram Reader. Exiting.")
         await telegram_sender.disconnect() # Disconnect sender before exiting
         mt5_connector.disconnect()
         sys.exit(1)
    logger.info("Telegram Reader Started.")

    # --- Notify Telegram channel that the bot is booting up (after clients are connected) ---
    if telegram_sender:
        try:
            boot_time = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
            msg = (
                f"🚦 <b>AI-Trader Bot Started</b>\n"
                f"<b>Status:</b> <code>Running</code>\n"
                f"<b>Boot Time:</b> <code>{boot_time}</code>\n"
                f"<i>This is an automated notification that the trading bot is online.</i>"
            )
            await telegram_sender.send_message(msg, parse_mode='html')
            logger.info("Sent Telegram boot notification message.")
        except Exception as boot_notify_err:
            logger.error(f"Failed to send Telegram boot notification: {boot_notify_err}")

    # 5. Run main loop until shutdown signal
    # Start the periodic tasks using initial config values
    monitor_interval = config_service.getint('Misc', 'periodic_check_interval_seconds', fallback=60)
    periodic_monitor_task = asyncio.create_task(periodic_mt5_monitor_task(monitor_interval), name="MT5MonitorTask")
    config_reload_interval = 30 # Check every 30 seconds (could be made configurable)
    config_reloader_task = asyncio.create_task(config_reloader_task_func(config_reload_interval), name="ConfigReloaderTask")
    trade_closure_monitor_task = asyncio.create_task(
        periodic_trade_closure_monitor_task(state_manager, telegram_sender, mt5_executor, interval_seconds=monitor_interval),
        name="TradeClosureMonitorTask"
    )
    conf_update_interval = config_service.getint('Misc', 'confirmation_update_interval_seconds', fallback=10)
    if conf_update_interval > 0:
        confirmation_update_task = asyncio.create_task(
            confirmation_updater_task_func(conf_update_interval),
            name="ConfirmationUpdateTask"
    )
    daily_summary_task_handle = asyncio.create_task( # Start daily summary task
        daily_summary_task(state_manager, telegram_sender),
        name="DailySummaryTask"
    )

    logger.info("Bot main components started. Handing control to Telegram client...")
    # Run the reader client until it's disconnected (e.g., by signal handler)
    try:
        if telegram_reader and telegram_reader.client:
            # Also run the sender client if it's separate and needs to run
            # In this case, sender uses start() which doesn't block like run_until_disconnected
            # We rely on the reader's run_until_disconnected to keep the loop alive
            await telegram_reader.client.run_until_disconnected()
        else:
            logger.error("Telegram reader client not initialized, cannot run.")
    except Exception as e:
        logger.error(f"Error during client.run_until_disconnected: {e}", exc_info=True)
    finally:
        # 6. Graceful Shutdown (after run_until_disconnected finishes)
        logger.info("--- Initiating Bot Shutdown Sequence ---")
        # Cancel tasks in reverse order of dependency (config first)
        if config_reloader_task and not config_reloader_task.done():
             logger.info("Shutdown: Cancelling config reloader task...")
             config_reloader_task.cancel()
        if periodic_monitor_task and not periodic_monitor_task.done():
             logger.info("Shutdown: Cancelling periodic monitor task...")
             periodic_monitor_task.cancel()
        if confirmation_update_task and not confirmation_update_task.done():
             logger.info("Shutdown: Cancelling confirmation updater task...")
             confirmation_update_task.cancel()
        if daily_summary_task_handle and not daily_summary_task_handle.done(): # Cancel daily summary
             logger.info("Shutdown: Cancelling daily summary task...")
             daily_summary_task_handle.cancel()
        if telegram_reader:
             await telegram_reader.stop()
        if telegram_sender:
            await telegram_sender.disconnect() # Disconnect sender
        if mt5_connector:
            mt5_connector.disconnect()
        logger.info("--- Trade Bot Shutdown Complete ---")


# --- Entry Point ---
if __name__ == "__main__":
    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, handle_shutdown_signal)  # Ctrl+C
    signal.signal(signal.SIGTERM, handle_shutdown_signal) # Kill/system shutdown

    # Run the main async function
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        # Should be caught by signal handler, but handle defensively
        logger.info("KeyboardInterrupt caught in main. Attempting shutdown.")
        # Signal handler should have been called, but call disconnect defensively
        if telegram_reader and telegram_reader.client and telegram_reader.client.is_connected():
             logger.info("KeyboardInterrupt: Disconnecting reader client...")
             # Schedule disconnect as we are outside the loop now potentially (or signal handler did it)
             asyncio.create_task(telegram_reader.client.disconnect())
    except Exception as e:
        print(f"CRITICAL UNHANDLED ERROR: {e}", file=sys.stderr)
        # Ensure cleanup attempts even on critical error
        if logger:
             logger.critical(f"CRITICAL UNHANDLED ERROR in main: {e}", exc_info=True)
        # Attempt cleanup (might fail if loop is broken), ensure tasks are cancelled
        try:
            if config_reloader_task and not config_reloader_task.done(): config_reloader_task.cancel()
            if periodic_monitor_task and not periodic_monitor_task.done(): periodic_monitor_task.cancel()
            if confirmation_update_task and not confirmation_update_task.done(): confirmation_update_task.cancel()
            if daily_summary_task_handle and not daily_summary_task_handle.done(): daily_summary_task_handle.cancel() # Cancel daily summary
            if telegram_reader: asyncio.create_task(telegram_reader.stop())
            if telegram_sender: asyncio.create_task(telegram_sender.disconnect())
            if mt5_connector: mt5_connector.disconnect()
        except Exception as cleanup_err:
             logger.error(f"Error during emergency cleanup: {cleanup_err}")
        sys.exit(1)
    finally:
         # Ensure logs are flushed
         logging.shutdown()