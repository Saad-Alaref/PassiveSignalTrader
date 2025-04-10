# AI-Trader Bot: Architecture and Functionality Overview

## 1. Introduction

The AI-Trader bot is an automated trading system designed to:
- Monitor a Telegram channel for trading signals and updates.
- Analyze these messages using a Large Language Model (LLM).
- Interact with the MetaTrader 5 (MT5) platform to fetch market data and execute/manage trades based on the analyzed signals.
- Maintain application state, including active trades, pending actions, and history.
- Provide feedback and status updates via Telegram.
- Perform automated trade management tasks like setting break-even stops, trailing stops, and handling take-profit levels.

The system is built using Python with `asyncio` for concurrent operations, handling Telegram events, MT5 interactions, and background monitoring tasks efficiently.

## 2. Core Workflow: Signal Processing

This outlines the typical flow when a new message arrives in the monitored Telegram channel.

```pseudocode
// Simplified Signal Processing Flow

FUNCTION handle_new_telegram_message(event):
    // Triggered by TelegramReader upon receiving a message

    // 1. Deduplication
    IF DuplicateChecker.is_duplicate(event.message_id):
        LOG "Duplicate message received, ignoring."
        RETURN
    ENDIF
    DuplicateChecker.add_processed_id(event.message_id)

    // 2. Analysis
    LOG "Analyzing message..."
    analysis_context = StateManager.get_llm_context(MT5DataFetcher) // Get current state (trades, market)
    analysis_result = SignalAnalyzer.analyze(event.text, event.image, context=analysis_context)
    // SignalAnalyzer uses LLMInterface internally

    // 3. Event Routing
    IF analysis_result.type IS "new_signal":
        LOG "Processing as new signal..."
        signal_data: SignalData = analysis_result.data
        StateManager.add_message_to_history(event) // Store original message context
        ASYNC_CALL EventProcessor.process_new_signal(signal_data, event.message_id, StateManager, ...)
    ELSE IF analysis_result.type IS "update":
        LOG "Processing as trade update..."
        update_data: UpdateData = analysis_result.data
        StateManager.add_message_to_history(event) // Store original message context
        ASYNC_CALL EventProcessor.process_update(update_data, event, StateManager, ...)
    ELSE:
        LOG "Message not recognized as actionable signal or update."
    ENDIF
ENDFUNCTION

FUNCTION EventProcessor.process_new_signal(signal_data, message_id, ...):
    // 4. Initial Decision & Checks
    IF NOT DecisionLogic.decide(signal_data): // Basic checks (e.g., price action)
        LOG "Signal rejected by initial decision logic."
        TelegramSender.send_message("Signal rejected: [Reason]")
        RETURN
    ENDIF

    // 5. Calculations
    trade_params = TradeCalculator.calculate_trade_parameters(signal_data) // Lot size, SL, TP etc.

    // 6. Pre-Execution Checks
    IF NOT _run_pre_execution_checks(signal_data, StateManager): // Check cooldowns, existing trades etc.
        LOG "Signal rejected by pre-execution checks."
        TelegramSender.send_message("Signal rejected: [Reason]")
        RETURN
    ENDIF

    // 7. Confirmation (if required, e.g., Market Orders)
    IF signal_data.order_type IS MARKET_ORDER AND ConfigService.getboolean("Trading", "require_market_confirmation"):
        confirmation_id = generate_uuid()
        confirmation_text = TelegramSender.format_confirmation_message(...)
        confirmation_message = ASYNC_CALL TelegramSender.send_confirmation_message(confirmation_id, trade_params, confirmation_text)
        StateManager.add_pending_confirmation(confirmation_id, trade_params, confirmation_message.id, ...)
        LOG "Market order confirmation requested."
        RETURN // Execution happens upon callback confirmation
    ENDIF

    // 8. Strategy Selection & Execution (for pending or confirmed market)
    strategy_type = determine_strategy(signal_data) // e.g., SingleTrade, DistributedLimits, MultiMarketStop
    strategy_instance = CREATE strategy_type(signal_data, trade_params, MT5Executor, StateManager, TelegramSender, ...)
    execution_results = ASYNC_CALL strategy_instance.execute() // Places order(s) via MT5Executor

    // 9. Post-Execution
    FOR result IN execution_results:
        IF result.success:
            // Strategy instance calls _store_trade_info which uses StateManager.add_active_trade
            LOG "Trade executed successfully. Ticket: {result.ticket}"
            TelegramSender.send_message("✅ Trade Executed: {result.details}")
        ELSE:
            LOG "Trade execution failed: {result.error}"
            TelegramSender.send_message("❌ Execution Failed: {result.error}")
        ENDIF
    ENDFOR
ENDFUNCTION

// Similar detailed flow exists for EventProcessor.process_update using UpdateCommands
```

## 3. Key Modules and Responsibilities

### 3.1. Configuration (`config_service.py`)
- **`ConfigService`**: Loads settings from `config.ini`. Provides typed access (getint, getfloat, getboolean) to configuration values used throughout the application. Supports reloading configuration during runtime.

### 3.2. External Interfaces
- **Telegram (`telegram_reader.py`, `telegram_sender.py`)**:
    - `TelegramReader`: Connects to Telegram (as a user), monitors the specified channel for messages/events, and forwards them to the main handler (`handle_telegram_event`).
    - `TelegramSender`: Sends formatted messages (status, confirmations, errors) back to Telegram. Handles interactive elements like confirmation buttons and processes their callbacks (`_handle_callback_query`). Manages target channels (main and debug).
- **MetaTrader 5 (`mt5_connector.py`, `mt5_data_fetcher.py`, `mt5_executor.py`)**:
    - `MT5Connector`: Manages the connection lifecycle to the MT5 terminal. Ensures a valid connection is available.
    - `MT5DataFetcher`: Fetches data from MT5 (account info, symbol prices/ticks, symbol properties) using an active connection.
    - `MT5Executor`: Executes actions on MT5 (places market/pending orders, modifies SL/TP, closes positions, cancels orders). Handles retries and potential adjustments (e.g., SL for spread).
- **LLM (`llm_interface.py`)**:
    - `LLMInterface`: Interacts with the configured LLM API (e.g., Google Gemini). Prepares prompts based on message content and context (provided by `StateManager`), sends requests, and parses the LLM's response.

### 3.3. Event Handling & Orchestration
- **Entry Point (`main.py`)**:
    - Initializes all core components (services, managers, interfaces).
    - Sets up `asyncio` event loop.
    - Starts the `TelegramReader` to listen for events.
    - Launches background tasks (MT5 monitoring, confirmation updates, trade closure monitoring, daily summary, config reloading).
    - Contains the main event handler (`handle_telegram_event`) which receives events from `TelegramReader` and routes them for analysis and processing.
    - Handles graceful shutdown.
- **Event Processing (`event_processor.py`)**:
    - Contains `process_new_signal` and `process_update`.
    - Orchestrates the workflow after a message is analyzed.
    - Coordinates calls to `DecisionLogic`, `TradeCalculator`, `StateManager`, `TelegramSender`, `TradeExecutionStrategies`, and `UpdateCommands`.
- **Signal Analysis (`signal_analyzer.py`)**:
    - `SignalAnalyzer`: Uses `LLMInterface` to interpret message text/images. Validates the extracted information (prices, SL/TP values). Structures the results into `SignalData` or `UpdateData` objects for further processing.

### 3.4. Trading Logic & Execution
- **Decision Logic (`decision_logic.py`)**:
    - `DecisionLogic`: Performs preliminary checks on a potential signal (e.g., comparing signal price against current market price action) before committing to full processing or execution.
- **Trade Calculation (`trade_calculator.py`)**:
    - `TradeCalculator`: Performs various financial calculations: lot size based on risk parameters, SL/TP price levels from distance/pips, adjusted entry prices considering spread, trailing stop loss levels.
- **Execution Strategies (`trade_execution_strategies.py`)**:
    - Defines different methods for placing trades based on signal characteristics:
        - `SingleTradeStrategy`: Executes a single market or pending order.
        - `DistributedLimitsStrategy`: Places multiple pending limit orders across a price range.
        - `MultiMarketStopStrategy`: Executes multiple market/stop orders, often used for sequential take profits.
    - Chosen and invoked by `EventProcessor`.
- **Update Commands (`update_commands.py`)**:
    - Defines specific actions for handling trade update messages (e.g., `ModifySLTPCommand`, `SetBECommand`, `CloseTradeCommand`, `CancelPendingCommand`, `ModifyEntryCommand`, `PartialCloseCommand`).
    - Encapsulates the logic for interacting with `MT5Executor` and `StateManager` for each update type.
    - Chosen and invoked by `EventProcessor`.

### 3.5. State Management & Monitoring
- **State (`state_manager.py`)**:
    - `StateManager`: The central repository for the application's runtime state. Tracks:
        - Active trades (`TradeInfo` objects).
        - Pending confirmations (market orders awaiting user input).
        - Message history (for context).
        - Closed trade logs.
        - Flags for pending automated actions (e.g., auto-SL).
        - Market execution cooldown timers.
    - Provides context (`get_llm_context`) for LLM analysis.
- **Active Trade Management (`trade_manager.py`)**:
    - `TradeManager`: Implements logic for managing *ongoing* trades based on market movements and pre-defined rules. Handles:
        - Automatically moving Stop Loss to Break-Even (`check_and_apply_auto_be`).
        - Applying Trailing Stop Loss (`check_and_apply_trailing_stop`).
        - Handling partial closures when Take Profit levels are hit for multi-TP strategies (`check_and_handle_tp_hits`).
        - Applying initial automatic Stop Loss if configured (`check_and_apply_auto_sl`).
    - Likely invoked periodically by a monitoring task.
- **Closure Monitoring (`trade_closure_monitor.py`)**:
    - `periodic_trade_closure_monitor_task`: A background task that periodically compares active trades stored in `StateManager` with actual positions/orders in MT5. Detects closures that occurred externally (e.g., manual close, SL/TP hit detected by MT5 directly). Updates `StateManager` and notifies via `TelegramSender`.
- **Duplicate Checking (`duplicate_checker.py`)**:
    - `DuplicateChecker`: Prevents processing the same Telegram message multiple times by storing and checking message IDs.
- **Daily Summary (`daily_summary.py`)**:
    - `daily_summary_task`: A background task that runs once daily, gathers statistics from `StateManager` (e.g., closed trades), and sends a summary report via `TelegramSender`.

### 3.6. Utilities
- **Logging (`logger_setup.py`)**:
    - `setup_logging`: Configures application-wide logging (level, format, file output).
- **Data Models (`models.py`)**:
    - Defines `dataclass` structures (`SignalData`, `UpdateData`, `TradeInfo`) to ensure consistent data representation and transfer between modules.

## 4. Data Flow

- **Input**: Telegram messages (text, images).
- **Core Data Structures**:
    - `SignalData`: Structured data representing a new trade signal (action, symbol, entry, SL, TPs, etc.).
    - `UpdateData`: Structured data representing an update to an existing trade (update type, target identifier, new values).
    - `TradeInfo`: Represents an active trade being managed by the system, stored in `StateManager`.
- **Configuration**: Loaded from `config.ini` via `ConfigService`.
- **State**: Managed centrally by `StateManager`.
- **Output**: Telegram messages (status, confirmations, errors, summaries), MT5 orders/modifications.

## 5. Concurrency

- The application heavily relies on `asyncio` to handle:
    - Waiting for Telegram events.
    - Performing network I/O with Telegram, LLM API, and potentially MT5 (depending on the library used).
    - Running multiple background tasks concurrently (monitoring, updates, summaries).

This structure allows the bot to remain responsive while performing various long-running or I/O-bound operations.