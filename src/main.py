import asyncio
import logging
import signal
import sys
import os
import configparser
import MetaTrader5 as mt5 # Import the MT5 library
from telethon import events # For type hinting in handler
from datetime import datetime, timezone, timedelta # For AutoSL timing

# Import local modules
from .state_manager import StateManager # Use relative import
from .logger_setup import setup_logging # Use relative import
from .config_loader import load_config # Use relative import
from .mt5_connector import MT5Connector # Use relative import
from .mt5_data_fetcher import MT5DataFetcher # Use relative import
from .llm_interface import LLMInterface # Use relative import
from .signal_analyzer import SignalAnalyzer # Use relative import
from .duplicate_checker import DuplicateChecker # Use relative import
from .decision_logic import DecisionLogic # Use relative import
from .trade_calculator import TradeCalculator # Use relative import
from .mt5_executor import MT5Executor # Use relative import
# Import the renamed reader and the new sender
from .telegram_reader import TelegramReader # Use relative import
from .telegram_sender import TelegramSender # Use relative import
from .trade_manager import TradeManager # Use relative import
from . import event_processor # Use relative import

# --- Global Variables ---
# Use asyncio events for graceful shutdown handling
shutdown_event = asyncio.Event()
logger = None # Will be configured in main

# --- State Management (Now handled by StateManager) ---
# bot_active_trades = [] # Moved to StateManager
# message_history = None # Moved to StateManager

# --- Core Components (Initialized in main) ---
config: configparser.ConfigParser = None
mt5_connector: MT5Connector = None
mt5_fetcher: MT5DataFetcher = None
llm_interface: LLMInterface = None
signal_analyzer: SignalAnalyzer = None
duplicate_checker: DuplicateChecker = None
decision_logic: DecisionLogic = None
trade_calculator: TradeCalculator = None
mt5_executor: MT5Executor = None
telegram_reader: TelegramReader = None # Renamed from monitor
telegram_sender: TelegramSender = None
state_manager: StateManager = None
trade_manager: TradeManager = None # Add TradeManager instance
# --- Signal Handling for Graceful Shutdown ---
def handle_shutdown_signal(sig, frame):
    """Sets the shutdown event when SIGINT or SIGTERM is received."""
    logger.info(f"Received shutdown signal: {sig}. Initiating graceful shutdown...")
    shutdown_event.set()

# --- Main Telegram Event Handler ---
# async def check_and_apply_auto_sl(): # Moved to TradeManager
#     ... (function content removed) ...


async def handle_telegram_event(event):
    # Make all necessary components accessible
    global logger, config, state_manager, trade_manager, signal_analyzer, \
           duplicate_checker, decision_logic, trade_calculator, mt5_executor, \
           telegram_sender, mt5_fetcher

    # --- Run AutoSL Check ---
    # Run this check periodically, e.g., every time an event comes in
    # A dedicated timer task would be more robust but adds complexity
    try:
        if trade_manager:
            await trade_manager.check_and_apply_auto_sl()
        else:
            logger.error("TradeManager not initialized, cannot run AutoSL check.")
    except Exception as auto_sl_err:
         logger.error(f"Error during AutoSL check: {auto_sl_err}", exc_info=True)
    # --- End AutoSL Check ---

    # --- Run TP Hit Check ---
    try:
        if trade_manager:
            await trade_manager.check_and_handle_tp_hits()
        else:
            logger.error("TradeManager not initialized, cannot run TP check.")
    except Exception as tp_err:
         logger.error(f"Error during TP check: {tp_err}", exc_info=True)
    # --- End TP Hit Check ---

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
        chat_id = event.chat_id
        message_text = getattr(event, 'text', '')
        is_edit = isinstance(event, events.MessageEdited.Event)
        reply_to_msg_id = getattr(event, 'reply_to_msg_id', None)
        image_data = None

        # TODO: Handle image data if message has photo
        # if event.photo:
        #     logger.debug(f"Message {message_id} contains a photo.")
        #     # Download image data (consider size limits and efficiency)
        #     # image_data = await event.download_media(bytes)
        #     # logger.info(f"Downloaded {len(image_data)} bytes for image.")
        #     pass # Placeholder for image handling logic

        log_prefix = f"[MsgID: {message_id}{' (Edit)' if is_edit else ''}{f' (Reply to {reply_to_msg_id})' if reply_to_msg_id else ''}]"
        logger.info(f"{log_prefix} Received event. Text: '{message_text[:80]}...'")
        # Send initial debug message to debug channel if configured
        debug_channel_id = getattr(telegram_sender, 'debug_target_channel_id', None)
        if debug_channel_id:
            debug_msg_start = f"🔎 {log_prefix} Processing event...\n<b>Text:</b><pre>{message_text}</pre>"
            await telegram_sender.send_message(debug_msg_start, target_chat_id=debug_channel_id)

        # --- Pre-filter Messages from Own Sender Bot ---
        sender_id = event.sender_id
        # Ensure telegram_sender and its ID are available
        if telegram_sender and telegram_sender.sender_bot_id:
            if sender_id == telegram_sender.sender_bot_id:
                logger.info(f"{log_prefix} Ignoring message from own sender bot (ID: {sender_id}).")
                if debug_channel_id:
                    debug_msg_ignore_self = f"🚫 {log_prefix} Ignoring own message (Sender ID: {sender_id})."
                    await telegram_sender.send_message(debug_msg_ignore_self, target_chat_id=debug_channel_id)
                # Mark as processed to prevent issues if message is edited later? Optional.
                # duplicate_checker.add_processed_id(message_id)
                return # Stop processing this message
        else:
            # Log a warning if we can't get the sender bot ID for comparison
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
                import json # Ensure json is imported if not already
                analysis_data_str = json.dumps(analysis_result.get('data'), indent=2) if analysis_result.get('data') else "None"
                if debug_channel_id:
                    debug_msg_analysis = f"🧠 {log_prefix} LLM Analysis Result:\n<b>Type:</b> <code>{analysis_type}</code>\n<b>Data:</b>\n<pre>{analysis_data_str}</pre>"
                    await telegram_sender.send_message(debug_msg_analysis, target_chat_id=debug_channel_id)
                # Handle 'ignore' type immediately
                if analysis_result.get('type') == 'ignore':
                    # status_message = f"🧐 Ignoring Message `[ID: {message_id}]` \\(Not actionable\\)\\." # Keep ignored simple
                    logger.info(f"{log_prefix} Ignored (Not actionable by LLM).")
                    duplicate_checker.add_processed_id(message_id)
                    if debug_channel_id:
                        debug_msg_llm_ignore = f"🧐 {log_prefix} Ignored by LLM (Not actionable)."
                        await telegram_sender.send_message(debug_msg_llm_ignore, target_chat_id=debug_channel_id)
                    # Optional: Send status to Telegram channel
                    # status_message = f"🧐 Ignoring Message `[ID: {message_id}]` (Not actionable)."
                    # await telegram_sender.send_message(status_message, parse_mode='html')
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
                     debug_msg_analysis_err = f"🆘 {log_prefix} Error during signal analysis:\n<pre>{analysis_err}</pre>"
                     await telegram_sender.send_message(debug_msg_analysis_err, target_chat_id=debug_channel_id)
                 # Optionally send an error status message to channel
                 # status_message_err = f"🆘 <b>Error analyzing message</b> <code>[MsgID: {message_id}]</code>. Check logs."
                 # await telegram_sender.send_message(status_message_err, parse_mode='html')
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
                config=config,
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
                 config=config,
                 log_prefix=log_prefix,
                 llm_context=llm_context, # Pass context for potential re-analysis
                 image_data=image_data # Pass image data
             )


    # Outer try...except for unexpected errors in the handler itself
    except Exception as handler_err:
        logger.error(f"{log_prefix} Unhandled exception in event handler: {handler_err}", exc_info=True)
        # Send unhandled error debug message
        debug_msg_handler_err = f"🆘🆘🆘 {log_prefix} UNHANDLED EXCEPTION in event handler:\n<pre>{handler_err}</pre>"
        if debug_channel_id: await telegram_sender.send_message(debug_msg_handler_err, target_chat_id=debug_channel_id)


# --- Main Application Function ---
async def run_bot():
    """Initializes and runs the main application components."""
    global logger, config, mt5_connector, mt5_fetcher, llm_interface, \
           signal_analyzer, duplicate_checker, decision_logic, trade_calculator, \
           mt5_executor, telegram_reader, telegram_sender, state_manager, trade_manager # Add trade_manager

    # 1. Load Config
    config = load_config()
    if not config:
        print("Exiting due to configuration error.", file=sys.stderr)
        sys.exit(1)

    # 2. Setup Logging
    log_file = config.get('Logging', 'log_file', fallback='logs/bot.log')
    log_level = config.get('Logging', 'log_level', fallback='INFO')
    # Construct absolute path for log file if it's relative
    if not os.path.isabs(log_file):
        log_file = os.path.join(os.path.dirname(__file__), '..', log_file)
    setup_logging(log_file_path=log_file, log_level_str=log_level)
    logger = logging.getLogger('TradeBot') # Get configured logger
    logger.info("--- Trade Bot Starting ---")

    # 3. Initialize Components
    try:
        mt5_connector = MT5Connector(config)
        mt5_fetcher = MT5DataFetcher(mt5_connector)
        llm_interface = LLMInterface(config)
        signal_analyzer = SignalAnalyzer(llm_interface, mt5_fetcher, config) # Pass fetcher and config
        # Read validated int value
        duplicate_cache_size = config.getint('Misc', 'duplicate_cache_size', fallback=10000)
        duplicate_checker = DuplicateChecker(max_size=duplicate_cache_size)
        decision_logic = DecisionLogic(config, mt5_fetcher)
        trade_calculator = TradeCalculator(config, mt5_fetcher)
        mt5_executor = MT5Executor(config, mt5_connector)
        telegram_reader = TelegramReader(config, handle_telegram_event)
        telegram_sender = TelegramSender(config)
        state_manager = StateManager(config)
        # Initialize TradeManager after its dependencies
        trade_manager = TradeManager(config, state_manager, mt5_executor, trade_calculator, telegram_sender, mt5_fetcher) # Pass mt5_fetcher
    except Exception as e:
        logger.critical(f"Failed to initialize components: {e}", exc_info=True)
        sys.exit(1)

    # 4. Connect to Services
    logger.info("Connecting to MT5...")
    if not mt5_connector.connect():
        logger.critical("Failed to connect to MT5. Exiting.")
        sys.exit(1)
    logger.info("MT5 Connected.")

    logger.info("Starting Telegram Reader (User Account)...")
    reader_started = await telegram_reader.start() # Start reader
    if not reader_started:
         logger.critical("Failed to start Telegram Reader. Exiting.")
         mt5_connector.disconnect()
         sys.exit(1)
    logger.info("Telegram Reader Started.")

    logger.info("Starting Telegram Sender (Bot Account)...")
    sender_started = await telegram_sender.connect() # Connect sender
    if not sender_started:
         # Log critical error but maybe don't exit? App can still read.
         # Or exit if sending status is essential. Let's exit for now.
         logger.critical("Failed to start Telegram Sender. Exiting.")
         await telegram_reader.stop() # Stop reader before exiting
         mt5_connector.disconnect()
         sys.exit(1)
    logger.info("Telegram Sender Started.")

    # 5. Run main loop until shutdown signal
    logger.info("Bot is running. Waiting for messages or shutdown signal...")
    await shutdown_event.wait() # Wait until handle_shutdown_signal sets the event

    # 6. Graceful Shutdown
    logger.info("--- Initiating Bot Shutdown ---")
    await telegram_sender.disconnect() # Disconnect sender
    await telegram_reader.stop() # Stop reader
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
        logger.info("KeyboardInterrupt caught in main. Shutting down.")
        # Ensure shutdown event is set if signal handler didn't run
        shutdown_event.set()
    except Exception as e:
        print(f"CRITICAL UNHANDLED ERROR: {e}", file=sys.stderr)
        if logger:
             logger.critical(f"CRITICAL UNHANDLED ERROR in main: {e}", exc_info=True)
        sys.exit(1)
    finally:
         # Ensure logs are flushed
         logging.shutdown()