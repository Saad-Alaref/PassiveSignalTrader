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
- Preparing for new feature development based on this stable foundation.

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

## Open Questions / Issues
- How to optimize LLM prompts and parsing accuracy further?
- What are the best weights and thresholds for decision logic in production?
- How to extend lot sizing beyond fixed 0.01 to risk-based dynamically?
- How to handle complex multi-TP management and partial closes more elegantly?
- How to improve resilience to MT5 errors, requotes, and network issues?
- How to support multi-symbol, multi-channel, or multi-account setups?
- What is the best deployment strategy (local, server, cloud)?
- How to add advanced analytics, dashboards, or GUI interfaces?