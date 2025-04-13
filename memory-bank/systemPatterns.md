# System Patterns

This document catalogs architectural patterns, design principles, and best practices used in the project.

---

## Architectural Patterns

- **Event-Driven Async Architecture:**
  The bot uses Python's `asyncio` to handle Telegram events, MT5 API calls, LLM requests, and background tasks concurrently, ensuring responsiveness and scalability.

- **Modular Clean Architecture:**
  The system is split into clear modules (Telegram, LLM, MT5, Decision Logic, Trade Management, State, Config, Logging) with well-defined interfaces, promoting maintainability and testability.

- **Pipeline Workflow:**
  Incoming Telegram messages flow through early deduplication → LLM analysis → decision logic → trade calculation → pre-checks → execution, with clear logging and error handling at each stage.

- **Strategy Pattern for Trade Execution:**
  Different execution strategies (single trade, distributed limits, multi-market stop) are encapsulated in interchangeable classes, selected dynamically based on signal type.

- **Command Pattern for Trade Updates:**
  Trade update actions (modify SL/TP, close, cancel, partial close) are encapsulated as commands, simplifying orchestration and extension.

---

## Take Profit (TP) Assignment System [2025-04-13 21:37]

- **Modular TP Assignment:**
  The bot uses a configuration-driven TP assignment system (`src/tp_assignment.py`) based on the Strategy pattern.
  - The `[TPAssignment]` section in `config.ini` controls the TP assignment mode.
  - **Supported Modes:**
    - `mode = none`: Assigns no TP (`None`) to any trade. Relies on other mechanisms like TSL/BE.
    - `mode = first_tp_first_trade`: Assigns the first valid TP from the signal to the first trade only. Subsequent trades (in multi-trade scenarios) get no TP. Single trades get the first TP if available.
    - `mode = custom_mapping`: Assigns TPs based on a user-defined `mapping` list in the config. The list contains 0-based indices of TPs from the signal or the string 'none'. Example: `mapping = 0, none, 1` assigns the first signal TP to the first trade, no TP to the second, and the second signal TP to the third trade. Indices out of range for the signal's TPs result in no TP (`None`).
  - **Removed Modes:** `fixed`, `risk-ratio`, `custom` (using `custom_func`).
  - **Removed Parameters:** `tp_sequence`, `SequenceMapper` logic is no longer used.
  - The old `[Strategy] tp_execution_strategy` setting is still used by `event_processor.py` to determine an *initial* TP candidate from the signal, but the final TP assigned to the order is determined by the active `[TPAssignment]` mode.

---

## Design Principles

- **Separation of Concerns:**
  Parsing, decision-making, execution, and state management are decoupled.

- **Configuration-Driven:**
  Behavior is controlled via `config.ini` (weights, thresholds, credentials, distances), avoiding hardcoding.

- **Fail-Safe Defaults:**
  Defaults like fixed lot size (0.01), neutral sentiment (0.5), and conservative trade rejection on missing SL ensure safe operation.

- **Extensibility:**
  New strategies, commands, or LLM models can be integrated with minimal changes.

- **Robust Error Handling:**
  Handles MT5 requotes, connection drops, missing data, and retries gracefully.

---

## LLM Prompting & Parsing

- **Multi-Intent Parsing:**
  LLM distinguishes between actionable signals, commentary, updates, and promotions.

- **Structured Extraction:**
  Prompts guide the LLM to extract action, entry, SL, TP, and sentiment in a structured format.

- **Handling Variability:**
  Designed to parse zones, multiple TPs (filtering out non-numeric like "open"), missing info, and edits/replies to incomplete signals.

---

## Trade Management Patterns

- **Auto Stop Loss (AutoSL):**
  - Applies a protective SL based on a configured fixed price distance (`auto_sl_price_distance`) if the signal lacks one.
  - Triggered after a configurable delay (`auto_sl_delay_seconds`).
  - Does *not* currently account for spread/offset.

- **Auto Break-Even (AutoBE):**
  - Moves SL to a slightly profitable position (entry + spread + offset) when a pip profit threshold (`auto_be_profit_pips`) is met.
  - Activation is now based on price movement in pips, not USD or trade volume.
  - Ensures no loss if BE SL is hit.

- **Auto Take Profit (AutoTP):**
  - Applies a TP based on a configured fixed pip distance (`auto_tp_distance_pips`) if the signal lacks one.

- **Trailing Stop Loss (TSL):**
  - Activates when profit reaches a configured pip threshold (`activation_profit_pips`).
  - Trails the market price by a fixed pip distance (`trail_distance_pips`).
  - Updates SL only when the market moves favorably.

- **Sequential Partial Close:**
  - Handles multiple TPs by closing a percentage (`partial_close_percentage`) at each TP hit.
  - Modifies the remaining position's TP to the next level.

---

## Trade Decision Logic Pattern

- **Bypass for Market Orders:**
  Immediate execution if LLM identifies a market order (may require confirmation).

- **Weighted Approval for Pending Orders:**
  Combines LLM sentiment and price action check with configurable weights and threshold.

- **Pre-Trade Checks:**
  Spread tolerance, trading hours, margin sufficiency, and slippage limits before execution.

---

## Best Practices

- **No Secrets in Code:**
  API keys and credentials are stored in config files or environment variables.

- **Comprehensive Logging:**
  Logs all key events, decisions, errors, and trade outcomes for transparency and debugging.

- **Async Background Tasks:**
  For trade monitoring, daily summaries, and confirmations without blocking main flow.

- **Graceful Shutdown:**
  Ensures connections are closed cleanly on exit.

- **Test Coverage:**
  Unit and integration tests are used to verify component behavior and end-to-end workflows, especially focusing on interactions and external system mocking.

- **Python Compatibility:**
  - The project targets Python 3.9. Use `Optional[...]` from the `typing` module for union types (e.g., `Optional[float]`), not the `|` union syntax, to ensure compatibility. The `|` syntax is only valid in Python 3.10+.