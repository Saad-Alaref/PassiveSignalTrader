# Active Context

This document tracks the current focus of work, recent changes, and open questions or issues.

## Current Focus
- Maintaining a modular, async, event-driven Telegram-to-MT5 trading bot.
- Using LLM (Google Gemini) to parse Telegram messages into structured trade signals or updates.
- Applying weighted decision logic combining LLM sentiment and price action for pending orders.
- Managing interactive Telegram confirmations for market orders.
- Handling trade updates via message edits and replies, with heuristic and LLM re-analysis.
- Tracking active trades, confirmations, message history, and cooldowns in StateManager.
- Ensuring robust error handling, logging, and user feedback throughout.
- **Improving test coverage**, particularly for component interactions and end-to-end workflows.
- **Debugging issues** identified through new integration tests.

## Recent Changes
- Fully implemented async orchestrator (`main.py`) with graceful shutdown and periodic tasks.
- Developed `event_processor.py` with modular, extensible trade execution and update workflows.
- Implemented `decision_logic.py` with config-driven, weighted approval logic.
- Built `signal_analyzer.py` for LLM-based parsing with validation and fallback.
- Created `telegram_sender.py` for bot communication, confirmations, and callback handling.
- Developed `state_manager.py` for runtime state, confirmations, LLM context, and cooldowns.
- Integrated MT5 components (`mt5_connector`, `mt5_data_fetcher`, `mt5_executor`) for trading.
- Modularized trade management, strategies, and update commands.
- Extensive logging, debug channels, and config hot-reloading added.
- **[2025-04-13] Added unit tests** for `DistributedLimitsStrategy` and `StateManager`.
- **[2025-04-13] Added integration test** (`test_process_multiple_signals`) for end-to-end signal processing workflow in `tests/test_workflows.py`.
- **[2025-04-13] Fixed bugs** in `src/event_processor.py` related to config loading (`NameError`) and missing duplicate check logic, identified via integration testing.
- **[2025-04-13] Refined test mocks** in `tests/test_workflows.py` to accurately simulate MT5 interactions and configuration values.
- **[2025-04-13 21:38] Refactored TP Assignment:** Simplified `src/tp_assignment.py` to support only `none`, `first_tp_first_trade`, and `custom_mapping` modes. Removed obsolete strategies (`fixed`, `risk-ratio`, `custom_func`) and parameters (`tp_sequence`). Updated `event_processor.py` and `trade_execution_strategies.py` accordingly.
- **[2025-04-13 21:38] Updated Config Example:** Added `[TPAssignment]` section to `config/example-config.ini` with documentation for the three supported TP modes.
- **[2025-04-13 21:38] Added TP Assignment Tests:** Rewrote `tests/test_tp_assignment.py` with comprehensive unit tests for the three supported TP modes.
- **[2025-04-13 21:38] Added Integration Tests:** Created `tests/test_trade_execution_integration.py` with advanced tests simulating realistic signal-to-trade scenarios, including TP assignment and entry/SL adjustments for spread/offset.

## Open Questions / Issues
- How to optimize LLM prompts and parsing accuracy further?
- What are the best weights and thresholds for decision logic in production?
- How to extend lot sizing beyond fixed 0.01 to risk-based dynamically?
- How to handle complex multi-TP management and partial closes more elegantly?
- How to improve resilience to MT5 errors, requotes, and network issues?
- How to support multi-symbol, multi-channel, or multi-account setups?
- What is the best deployment strategy (local, server, cloud)?
- How to add advanced analytics, dashboards, or GUI interfaces?
- **Continue increasing test coverage** across remaining modules (e.g., `config_service`, `llm_interface`, `mt5_connector`, `telegram_sender`).
- **[2025-04-15 13:23] Removed Image Handling Code:** Removed all `image_data` parameters and related logic from `main.py`, `llm_interface.py`, `signal_analyzer.py`, and `event_processor.py` to completely eliminate image processing capabilities.
- **[2025-04-15 13:32] Fixed Trailing Stop Loss (TSL) Logging:** Reviewed TSL logic in `src/trade_manager.py` and `src/trade_calculator.py`. Confirmed core logic is sound (pip-based, one-way movement). Fixed misleading log message in `trade_manager.py` regarding the 'from' SL value during updates. Fixed a formatting warning in `trade_calculator.py`'s logging of price distance.