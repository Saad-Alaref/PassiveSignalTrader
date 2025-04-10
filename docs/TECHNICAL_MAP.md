# AI-Trader Technical Map

## Overview

AI-Trader is an automated trading bot that processes signals from a Telegram channel,
analyzes them using an LLM (Google Gemini), makes trading decisions, executes trades
via MetaTrader 5 (MT5), and manages open positions based on configured strategies
(AutoSL, AutoBE, Trailing Stop, Sequential TPs). It uses a user account to read
the signal channel and a bot account to send confirmations and status updates.

The system is built asynchronously using Python's asyncio and the Telethon library.
It follows a modular design with distinct components for configuration, state management,
MT5 interaction (connection, data fetching, execution), LLM interaction, signal analysis,
decision logic, trade calculation, execution strategies, update handling, and Telegram communication.
Background tasks handle periodic checks and configuration reloading.

## Core Components

| Module                          | Responsibility                                                                                                     |
| :------------------------------ | :----------------------------------------------------------------------------------------------------------------- |
| `main.py`                       | Main entry point, initializes components, starts async tasks, defines main event handler.                          |
| `config_service.py`             | Loads, provides access to, and handles hot-reloading of `config.ini` settings.                                     |
| `logger_setup.py`               | Configures application logging.                                                                                    |
| `models.py`                     | Defines core data structures (SignalData, UpdateData, TradeInfo).                                                  |
| `state_manager.py`              | Manages application state: active trades, pending confirmations, message history, cooldowns.                         |
| `telegram_reader.py`            | Connects to Telegram (user account), monitors the signal channel, triggers event handler.                          |
| `telegram_sender.py`            | Connects to Telegram (bot account), sends status/confirmation messages, handles button callbacks.                  |
| `llm_interface.py`              | Interfaces with the Google Gemini API, prepares prompts, sends requests, parses responses.                         |
| `signal_analyzer.py`            | Uses LLMInterface to analyze messages, classifies them (new_signal, update, ignore), extracts structured data.      |
| `event_processor.py`            | Orchestrates the processing of new signals and updates based on analysis results.                                  |
| `decision_logic.py`             | Decides whether to approve a signal based on type, price action, and optional sentiment score.                     |
| `trade_calculator.py`           | Calculates trade parameters (lot size, SL/TP prices based on distance).                                            |
| `mt5_connector.py`              | Manages the connection lifecycle with the MT5 terminal.                                                            |
| `mt5_data_fetcher.py`           | Fetches market data (ticks, symbol info) and account info from MT5.                                                |
| `mt5_executor.py`               | Executes trading actions (send order, modify, close, delete) via the MT5 API, includes retry logic.                |
| `trade_execution_strategies.py` | Defines different methods for placing initial orders (Single, Distributed Limits, Multi-Market/Stop).              |
| `update_commands.py`            | Implements the Command pattern for handling specific trade update instructions (Modify SL/TP, Close, Cancel, etc.). |
| `trade_manager.py`              | Manages active trades periodically (AutoSL, AutoBE, Trailing Stop, Sequential TP handling).                        |
| `duplicate_checker.py`          | Prevents processing duplicate Telegram messages.                                                                   |
| `daily_summary.py`              | Background task for sending daily performance summaries.                                                           |
| `trade_closure_monitor.py`      | Background task to detect trades closed outside the bot.                                                           |

## Main Workflows

### New Telegram Message (Signal) Processing

1.  TelegramReader receives new message in target channel.
2.  TelegramReader calls `main.handle_telegram_event`.
3.  `handle_telegram_event` logs event, checks duplicates (`DuplicateChecker`), gets context (`StateManager.get_llm_context`).
4.  `handle_telegram_event` calls `SignalAnalyzer.analyze`.
5.  `SignalAnalyzer.analyze` calls `LLMInterface.analyze_message`.
6.  `LLMInterface` prepares prompt (using `ConfigService` templates + context), calls Gemini API, parses JSON response.
7.  `SignalAnalyzer` validates LLM response, creates `SignalData` object, returns `{'type': 'new_signal', 'data': SignalData}`.
8.  `handle_telegram_event` calls `event_processor.process_new_signal`.
9.  `process_new_signal` calls `DecisionLogic.decide`.
10. `DecisionLogic` checks signal type. If Market, approves. If Pending, calls `_perform_price_action_check` (using `MT5DataFetcher`) and evaluates weighted score (price action + optional sentiment). Returns approval status and MT5 order type.
11. IF rejected THEN `process_new_signal` sends rejection message (`TelegramSender`) and stops.
12. IF approved THEN `process_new_signal` calls `TradeCalculator.calculate_lot_size`.
13. `TradeCalculator` calculates lot size based on `config.ini` method (e.g., fixed), adjusts for symbol constraints (`MT5DataFetcher`).
14. `process_new_signal` runs pre-execution checks (`_run_pre_execution_checks`: Max Lot, Cooldown using `MT5DataFetcher`, `StateManager`).
15. IF checks fail THEN `process_new_signal` sends abort message (`TelegramSender`) and stops.
16. `process_new_signal` determines execution parameters (price, SL, TP, potentially using `TradeCalculator.calculate_tp_from_distance` for AutoTP).
17. IF Market Order THEN `process_new_signal` sends confirmation request (`TelegramSender.send_confirmation_message`), stores pending confirmation (`StateManager.add_pending_confirmation`), and stops.
18. IF Pending Order THEN `process_new_signal` selects appropriate strategy (`trade_execution_strategies`: Single, Distributed, MultiMarketStop) based on `config.ini`.
19. Selected Strategy's `execute` method is called.
20. Strategy `execute` calls `MT5Executor.execute_trade` (potentially multiple times).
21. `MT5Executor.execute_trade` builds request, calls `_send_order_with_retry`.
22. `_send_order_with_retry` calls `mt5.order_send`, handles retries/filling modes.
23. Strategy `execute` stores trade info (`_store_trade_info` -> `StateManager.add_active_trade`), sends status message (`TelegramSender`).

### Telegram Message Update/Reply Processing

1.  TelegramReader receives message edit or reply.
2.  TelegramReader calls `main.handle_telegram_event`.
3.  `handle_telegram_event` identifies it as edit/reply, gets context (`StateManager.get_llm_context`).
4.  `handle_telegram_event` calls `event_processor.process_update`.
5.  `process_update` finds the target trade associated with the original message ID (`StateManager.get_trade_by_original_msg_id`).
6.  IF no target trade THEN stops.
7.  `process_update` attempts heuristic check for simple SL/TP edits.
8.  IF heuristic fails OR complex update THEN `process_update` calls `SignalAnalyzer.analyze` on the *new* message text.
9.  `SignalAnalyzer` (via `LLMInterface`) analyzes, validates, creates `UpdateData` object, returns `{'type': 'update', 'data': UpdateData}`.
10. IF analysis fails OR not 'update' type THEN stops.
11. `process_update` gets the appropriate command class using `update_commands.get_command(update_type)`.
12. `process_update` instantiates the command (e.g., `ModifySLTPCommand`, `CloseTradeCommand`).
13. Command's `execute` method is called.
14. Command `execute` performs checks (e.g., config flags), calls relevant `MT5Executor` methods (e.g., `modify_trade`, `close_position`, `delete_pending_order`).
15. Command `execute` sends status message using `_send_status_message` (`TelegramSender`).

### Market Order Confirmation Handling

1.  User clicks confirmation button (Yes/No) on message sent by `TelegramSender`.
2.  Telethon triggers callback query event.
3.  `TelegramSender._handle_callback_query` receives the event.
4.  `_handle_callback_query` extracts confirmation ID and action (yes/no).
5.  `_handle_callback_query` retrieves pending confirmation data (`StateManager.get_pending_confirmation`).
6.  `_handle_callback_query` removes pending confirmation (`StateManager.remove_pending_confirmation`).
7.  IF Yes THEN `_handle_callback_query` calls `MT5Executor.execute_trade` with stored parameters.
8.  IF execution succeeds THEN `_handle_callback_query` stores trade info (`StateManager.add_active_trade`).
9.  `_handle_callback_query` edits the original confirmation message (`TelegramSender.edit_message`) to show final status (Executed, Failed, Rejected, Expired).

### Periodic Trade Monitoring (AutoSL, AutoBE, TSL, TP Hits)

1.  `main.py` starts `periodic_mt5_monitor_task`.
2.  Task loops every `periodic_check_interval_seconds`.
3.  Task ensures MT5 connection (`MT5Connector.ensure_connection`).
4.  Task fetches current open positions (`mt5.positions_get`).
5.  Task iterates through active trades tracked by `StateManager.get_active_trades`.
6.  FOR each tracked trade AND corresponding MT5 position:
    *   Task calls `TradeManager.check_and_apply_auto_sl`.
        *   `check_and_apply_auto_sl` checks config, delay, existing SL. If conditions met, calls `TradeCalculator.calculate_sl_from_distance`, then `MT5Executor.modify_trade`, sends notification (`TelegramSender`), updates state (`StateManager`).
    *   Task calls `TradeManager.check_and_apply_auto_be`.
        *   `check_and_apply_auto_be` checks config, profit threshold (scaled by volume), existing SL. If conditions met, calls `MT5Executor.modify_sl_to_breakeven`, sends notification (`TelegramSender`), updates state (`TradeInfo.auto_be_applied`).
    *   Task calls `TradeManager.check_and_apply_trailing_stop`.
        *   `check_and_apply_trailing_stop` checks config. If not active, checks activation profit (scaled). If active or activating, calculates TSL price (`TradeCalculator.calculate_trailing_sl_price` using `MT5DataFetcher` for current price). If new TSL is better than current SL, calls `MT5Executor.modify_trade`, sends notification (`TelegramSender`), updates state (`TradeInfo.tsl_active`).
    *   Task calls `TradeManager.check_and_handle_tp_hits`.
        *   `check_and_handle_tp_hits` checks if `tp_execution_strategy` is `sequential_partial_close`. If so, checks if current price (`MT5DataFetcher`) hits the `trade_info.next_tp_index`. If hit, calculates partial close volume, calls `MT5Executor.close_position`. If not last TP, calls `MT5Executor.modify_trade` to set next TP. Sends notification (`TelegramSender`), updates state (`TradeInfo.next_tp_index`).

### Configuration Reloading

1.  `main.py` starts `config_reloader_task_func`.
2.  Task loops every N seconds.
3.  Task checks modification time of `config/config.ini`.
4.  IF modified THEN Task calls `ConfigService.reload_config`.
5.  `ConfigService.reload_config` re-parses the INI file into its internal state.
6.  Components using `ConfigService` instance will get updated values on subsequent calls to `get/getint/getfloat/getboolean`.

## Key Function Call Examples (Illustrative Call Graph Snippets)

*   `main.handle_telegram_event` -> `SignalAnalyzer.analyze` -> `LLMInterface.analyze_message` -> `genai.GenerativeModel.generate_content`
*   `main.handle_telegram_event` -> `event_processor.process_new_signal` -> `DecisionLogic.decide` -> `TradeCalculator.calculate_lot_size` -> `_run_pre_execution_checks` -> `Strategy.execute` -> `MT5Executor.execute_trade` -> `_send_order_with_retry` -> `mt5.order_send`
*   `main.handle_telegram_event` -> `event_processor.process_update` -> `update_commands.get_command` -> `Command.execute` -> `MT5Executor.modify_trade` / `close_position` / etc.
*   `TelegramSender._handle_callback_query` -> `MT5Executor.execute_trade` -> `StateManager.add_active_trade` -> `TelegramSender.edit_message`
*   `periodic_mt5_monitor_task` -> `TradeManager.check_and_apply_trailing_stop` -> `TradeCalculator.calculate_trailing_sl_price` -> `MT5Executor.modify_trade`

## Extension Points

*   **Adding New Signal Parameters/Types:**
    *   Modify `models.SignalData` dataclass.
    *   Update LLM prompts in `config.ini` (`[LLMPrompts]`) to instruct extraction of new parameters.
    *   Update `SignalAnalyzer._validate_*` methods if new validation is needed.
    *   Update `DecisionLogic.decide` if new parameters affect decisions.
    *   Update `TradeCalculator.calculate_lot_size` if new parameters affect risk/sizing.
    *   Update `trade_execution_strategies` if new parameters affect execution.
*   **Adding New Execution Strategies:**
    *   Create new class inheriting from `trade_execution_strategies.ExecutionStrategy`.
    *   Implement the `execute` method, calling `MT5Executor` as needed.
    *   Update logic in `event_processor.process_new_signal` to select the new strategy based on config/signal data.
    *   Add relevant configuration options in `config.ini` (`[Strategy]`).
*   **Adding New Update Commands:**
    *   Create new class inheriting from `update_commands.UpdateCommand`.
    *   Implement the `execute` method, calling `MT5Executor`.
    *   Add the new command class to `update_commands.COMMAND_MAP`.
    *   Update LLM prompts (`config.ini`) to recognize and classify messages triggering this command, returning the new `update_type`.
    *   Add control flag to `[UpdateControls]` in `config.ini` and check it using `_check_config_flag`.
*   **Modifying LLM Prompts/Behavior:**
    *   Edit prompt templates directly in `config.ini` (`[LLMPrompts]` section).
    *   Adjust `temperature` or `enable_json_mode` in `config.ini` (`[Gemini]`).
    *   Modify context gathering in `StateManager.get_llm_context`.
    *   Modify prompt preparation logic in `LLMInterface._prepare_prompt`.
*   **Adding New Configuration Options:**
    *   Add the new option to the relevant section in `config/config.ini`.
    *   Use `ConfigService.get/getint/getfloat/getboolean` in the relevant module(s) to read the new value.
    *   Ensure appropriate fallback values are provided.

## Debugging Entry Points

*   **Key Log Statements:**
    *   `main.py`: Startup, component initialization, event reception (`handle_telegram_event`), task management, shutdown.
    *   `llm_interface.py`: Prompt construction (DEBUG), API requests/retries, raw responses (DEBUG), JSON parsing results/errors.
    *   `signal_analyzer.py`: Raw LLM results (DEBUG), classification results (INFO), constructed data objects (DEBUG), validation warnings.
    *   `event_processor.py`: Workflow steps (INFO), decision results (INFO), calculation results (INFO), execution strategy selection (INFO), errors during processing (ERROR).
    *   `decision_logic.py`: Decision steps (INFO), price action checks (DEBUG), sentiment scores (DEBUG), final score calculation (INFO), rejection reasons (INFO/WARNING).
    *   `trade_calculator.py`: Lot size method/result (DEBUG/INFO), SL/TP calculation inputs/results (DEBUG/INFO), constraint adjustments (INFO).
    *   `mt5_executor.py`: Order send attempts/retries (INFO), request details (DEBUG), `order_send` results (INFO), modification/close attempts (INFO), errors (ERROR).
    *   `trade_manager.py`: Periodic check start/end (DEBUG), AutoSL/BE/TSL checks and results (INFO/DEBUG), TP hit detection (INFO), modification/close results (INFO/ERROR).
    *   `telegram_sender.py`: Message sending attempts/results (INFO/DEBUG), callback query handling (INFO), confirmation status updates (INFO).
    *   `update_commands.py`: Command execution start (INFO), success/failure counts (INFO/WARNING/ERROR).
*   **Error Handling:**
    *   Most core functions have `try...except` blocks logging errors and often returning `None` or `False`.
    *   `MT5Executor._send_order_with_retry` handles specific MT5 error codes (requote, invalid fill).
    *   `main.py` has top-level exception handlers for the main loop and signal handling.
*   **Async Task Management:**
    *   Tasks are created and named in `main.run_bot` (e.g., `MT5MonitorTask`, `ConfigReloaderTask`).
    *   Signal handler (`main.handle_shutdown_signal`) attempts graceful cancellation.
    *   Monitor task loops (`periodic_mt5_monitor_task`, `config_reloader_task_func`, etc.) have internal exception handling to prevent crashes.
*   **Debug Channel Output:**
    *   If `debug_channel_id` is configured in `[Telegram]`, `TelegramSender` sends detailed step-by-step processing info, analysis results, decision logic, errors, etc., to that channel.

## Configuration & Dependencies

*   **`config.ini` Sections:**
    *   `[Telegram]`: API credentials (user & bot), channel IDs (signal source, debug target).
    *   `[MT5]`: Account details, password, server, symbol, connection path.
    *   `[Gemini]`: API key, model name, temperature, JSON mode flag.
    *   `[LLMPrompts]`: Templates for prompts sent to Gemini.
    *   `[Trading]`: Lot size method/value, SL/TP defaults, slippage, cooldowns, confirmation timeout, max lots.
    *   `[DecisionLogic]`: Weights for sentiment/price action, approval threshold, sentiment usage flag.
    *   `[AutoSL]`: Enable flag, delay, distance.
    *   `[AutoBE]`: Enable flag, profit threshold (USD), base lot for scaling.
    *   `[TrailingStop]`: Enable flag, activation profit (USD), trail distance (price units).
    *   `[AutoTP]`: Enable flag, distance.
    *   `[Strategy]`: TP execution strategy, entry range strategy, partial close percentage.
    *   `[UpdateControls]`: Flags to enable/disable specific update commands (e.g., `allow_modify_sltp`).
    *   `[Retries]`: Requote retry attempts/delay.
    *   `[Logging]`: Log file path, log level.
    *   `[Misc]`: Duplicate cache size, periodic check intervals.
*   **Environment Dependencies:**
    *   Python 3.x environment with packages from `requirements.txt` installed.
    *   Valid Google Gemini API Key (set in `config.ini`).
    *   Valid Telegram API ID and Hash (set in `config.ini`).
    *   MetaTrader 5 Terminal installed and running.
    *   MT5 Account Credentials (set in `config.ini`).
    *   Access to the specified Telegram signal channel for the user account.