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
from .trade_closure_monitor import periodic_trade_closure_monitor_task, closed_trades_log
from .daily_summary import daily_summary_task

# --- Global Variables ---
logger = None # Will be configured in main
# shared_config = None # Replaced by config_service instance
config_file_path = 'config/config.ini' # Keep for potential direct path use if needed
last_config_mtime = 0 # Track modification time
# config_lock = asyncio.Lock() # Lock might not be needed if service handles internal state safely
config_reloader_task = None # Task handle for the reloader
periodic_monitor_task = None # Task handle for the periodic MT5 monitor

# --- Core Components (Initialized in main) ---
mt5_connector: MT5Connector = None
mt5_fetcher: MT5DataFetcher = None
llm_interface: LLMInterface = None
signal_analyzer: SignalAnalyzer = None
duplicate_checker: DuplicateChecker = None
decision_logic: DecisionLogic = None
trade_calculator: TradeCalculator = None
mt5_executor: MT5Executor = None
telegram_reader: TelegramReader = None
telegram_sender: TelegramSender = None
state_manager: StateManager = None
trade_manager: TradeManager = None

# --- Signal Handling for Graceful Shutdown ---
def handle_shutdown_signal(sig, frame):
    """Initiates graceful shutdown when SIGINT or SIGTERM is received."""
    global periodic_monitor_task, config_reloader_task # Use correct task names
    logger.info(f"Received shutdown signal: {sig}. Initiating graceful shutdown...")

    # Cancel the periodic monitor task first
    if periodic_monitor_task and not periodic_monitor_task.done():
        logger.info("Signal handler: Cancelling periodic monitor task...")
        periodic_monitor_task.cancel()

    # Cancel the config reloader task
    if config_reloader_task and not config_reloader_task.done():
        logger.info("Signal handler: Cancelling config reloader task...")
        config_reloader_task.cancel()

    # Try to disconnect the client directly from the signal handler
    # This should cause run_until_disconnected() to exit
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
        image_data = None

        # TODO: Handle image data if message has photo

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
                analysis_result = signal_analyzer.analyze(message_text, image_data, llm_context)
                analysis_type = analysis_result.get('type')
                logger.info(f"{log_prefix} Signal analysis result type: {analysis_type}")
                # Send analysis debug message
                # import json # Moved to top
                analysis_data_str = json.dumps(analysis_result.get('data'), indent=2) if analysis_result.get('data') else "None"
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
                config_service=config_service, # Pass service instance
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
                 config_service=config_service, # Pass service instance
                 log_prefix=log_prefix,
                 llm_context=llm_context, # Pass context for potential re-analysis
                 image_data=image_data # Pass image data
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
    """Periodically fetches positions and calls trade management functions."""
    global trade_manager, state_manager, logger, mt5_connector, config_service # Use config_service

    logger.info(f"Starting periodic MT5 monitor task (initial interval: {interval_seconds}s).")
    while True:
        try:
            # Read interval dynamically inside the loop for hot-reloading
            current_interval = config_service.getint('Misc', 'periodic_check_interval_seconds', fallback=60)
            if current_interval <= 0:
                 logger.warning("Periodic check interval is zero or negative, task will sleep for 60s.")
                 current_interval = 60 # Prevent busy-looping

            await asyncio.sleep(current_interval)

            if not trade_manager or not state_manager or not mt5_connector:
                logger.warning("Periodic check: Core components not initialized.")
                continue

            if not mt5_connector.ensure_connection():
                 logger.warning("Periodic check: MT5 connection failed. Skipping checks.")
                 continue

            logger.debug("Running periodic trade checks (AutoSL, AutoBE, TP)...")
            positions = mt5.positions_get()
            if positions is None:
                if mt5.last_error()[0] != 0: # Log only if there was an actual error
                    logger.error(f"Periodic check: Failed to get positions: {mt5.last_error()}")
                continue # Skip checks if positions couldn't be fetched

            if not positions:
                # logger.debug("Periodic check: No open positions found.")
                continue # No positions to check

            # Create a mapping of ticket -> position for quick lookup
            positions_dict = {pos.ticket: pos for pos in positions}

            # Iterate through trades tracked by the bot
            # Get a copy to avoid issues if the list is modified during iteration
            active_bot_trades = list(state_manager.get_active_trades())
            for trade_info in active_bot_trades:
                ticket = trade_info.get('ticket')
                if not ticket: continue

                # Get the latest position data from MT5 for this ticket
                current_position = positions_dict.get(ticket)

                if current_position:
                    # Call individual check functions, passing the fetched position and stored trade info
                    try:
                        await trade_manager.check_and_apply_auto_sl(current_position, trade_info)
                    except Exception as e:
                        logger.error(f"Error during periodic AutoSL check for ticket {ticket}: {e}", exc_info=True)

                    try:
                        await trade_manager.check_and_apply_auto_be(current_position, trade_info)
                    except Exception as e:
                        logger.error(f"Error during periodic AutoBE check for ticket {ticket}: {e}", exc_info=True)

                    try:
                        await trade_manager.check_and_handle_tp_hits(current_position, trade_info)
                    except Exception as e:
                        logger.error(f"Error during periodic TP check for ticket {ticket}: {e}", exc_info=True)

                    # --- Add Trailing Stop Check ---
                    try:
                        await trade_manager.check_and_apply_trailing_stop(current_position, trade_info)
                    except Exception as e:
                         logger.error(f"Error during periodic Trailing Stop check for ticket {ticket}: {e}", exc_info=True)
                    # --- End Trailing Stop Check ---
                # else: # Position no longer exists in MT5, StateManager cleanup should handle it eventually
                #    logger.debug(f"Periodic check: Tracked trade {ticket} not found in current MT5 positions.")

            logger.debug("Finished periodic trade checks.")

        except asyncio.CancelledError:
            logger.info("Periodic MT5 monitor task cancelled.")
            break # Exit the loop on cancellation
        except Exception as e:
            logger.error(f"Error in periodic MT5 monitor task: {e}", exc_info=True)
            # Avoid rapid errors, wait longer before next check
            # Use the interval read at the start of the loop or a default
            error_sleep_interval = current_interval if 'current_interval' in locals() and current_interval > 0 else 60
            await asyncio.sleep(error_sleep_interval * 2)


# --- Main Application Function ---
async def run_bot():
    """Initializes and runs the main application components."""
    # Ensure all globals including task handles are declared
    global logger, config_service, mt5_connector, mt5_fetcher, llm_interface, \
           signal_analyzer, duplicate_checker, decision_logic, trade_calculator, \
           mt5_executor, telegram_reader, telegram_sender, state_manager, trade_manager, \
           config_file_path, last_config_mtime, config_reloader_task, periodic_monitor_task # Replace shared_config

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


    # 5. Run main loop until shutdown signal
    # Start the periodic tasks using initial config values
    monitor_interval = config_service.getint('Misc', 'periodic_check_interval_seconds', fallback=60)
    periodic_monitor_task = asyncio.create_task(periodic_mt5_monitor_task(monitor_interval), name="MT5MonitorTask")
    config_reload_interval = 30 # Check every 30 seconds (could be made configurable)
    config_reloader_task = asyncio.create_task(config_reloader_task_func(config_reload_interval), name="ConfigReloaderTask")
    trade_closure_monitor_task = asyncio.create_task(
        periodic_trade_closure_monitor_task(state_manager, telegram_sender, interval_seconds=60),
        name="TradeClosureMonitorTask"
    )

    logger.info("Bot main components started. Handing control to Telegram client...")
    # Run the reader client until it's disconnected (e.g., by signal handler)
    try:
        daily_summary_task_handle = asyncio.create_task(
            daily_summary_task(state_manager, telegram_sender),
            name="DailySummaryTask"
        )
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
            if telegram_reader: asyncio.create_task(telegram_reader.stop())
            if telegram_sender: asyncio.create_task(telegram_sender.disconnect())
            if mt5_connector: mt5_connector.disconnect()
        except Exception as cleanup_err:
             logger.error(f"Error during emergency cleanup: {cleanup_err}")
        sys.exit(1)
    finally:
         # Ensure logs are flushed
         logging.shutdown()