# Decision Log

This document records significant architectural and technical decisions, including rationale and implications.

---

### [2025-04-10 15:14:00] - Fixed Lot Size for Initial Version
- **Decision:** Use a fixed lot size of 0.01 for all trades.
- **Rationale:** Simplifies initial implementation, reduces risk, avoids complex margin calculations.
- **Implications:** Future versions may add risk-based sizing.

---

### [2025-04-10 15:14:00] - Weighted Trade Approval Logic
- **Decision:** For pending orders, combine LLM sentiment and price action check with configurable weights and threshold.
- **Rationale:** Improves trade quality by filtering low-confidence signals.
- **Implications:** Weights and threshold can be tuned in config.

---

### [2025-04-10 15:14:00] - Bypass Approval for Market Orders
- **Decision:** If LLM identifies a market execution signal, skip weighted approval and execute immediately.
- **Rationale:** Market orders imply urgency; delays reduce effectiveness.
- **Implications:** Relies on LLM accuracy in identifying order type.

---

### [2025-04-10 15:14:00] - Use LLM to Parse Telegram Messages
- **Decision:** Employ Google Gemini LLM to extract structured trade data from unstructured Telegram messages.
- **Rationale:** Handles variability in message formats, reduces need for brittle regex parsing.
- **Implications:** Requires prompt engineering and error handling for LLM failures.

---

### [2025-04-10 15:14:00] - Async Event-Driven Architecture
- **Decision:** Use Python `asyncio` for Telegram, MT5, LLM, and background tasks.
- **Rationale:** Enables responsive, concurrent operations.
- **Implications:** Requires careful async design and error handling.

---

### [2025-04-10 15:14:00] - Modular Clean Architecture
- **Decision:** Separate modules for Telegram, LLM, MT5, Decision Logic, Trade Management, Config, and Logging.
- **Rationale:** Improves maintainability, testability, and extensibility.
- **Implications:** Facilitates future feature additions.

---

### [2025-04-10 15:14:00] - Handle Incomplete Signals via Edits/Replies
- **Decision:** If initial signal lacks SL/TP, wait for Telegram message edits or replies to update trade.
- **Rationale:** Signal senders often update messages; bot should adapt.
- **Implications:** Requires message state tracking and update handling.

---

### [2025-04-11 15:30:00] - Refactor Trailing Stop Loss to Use Pips
- **Decision:** Changed TSL activation and trail distance from USD/price units to pips.
- **Rationale:** Makes TSL behavior consistent across different trade volumes and simplifies configuration.
- **Implications:** Updated `config.ini` keys (`activation_profit_pips`, `trail_distance_pips`), modified logic in `trade_manager.py` and `trade_calculator.py` to handle pip-based calculations. Requires users to update their config files.

---

### [2025-04-11 15:50:00] - Refactor Auto Take Profit to Use Pips
- **Decision:** Changed AutoTP distance from price units to pips.
- **Rationale:** Makes AutoTP behavior consistent and simplifies configuration.
- **Implications:** Updated `config.ini` key (`auto_tp_distance_pips`), modified logic in `event_processor.py` and `trade_calculator.py` to handle pip-based calculations. Requires users to update their config files.

---

### [2025-04-11 16:28:00] - Refactor Auto Break-Even Activation to Use Pips
- **Decision:** Changed AutoBE activation from USD profit to pip profit threshold.
- **Rationale:** Makes BE activation consistent and independent of trade volume.
- **Implications:** Updated `config.ini` key (`auto_be_profit_pips`), modified logic in `trade_manager.py` to handle pip-based activation. Requires users to update their config files.

---

### [2025-04-11 16:48:00] - Fix AutoSL Calculation to Use Pips
- **Decision:** Modified `trade_calculator.calculate_sl_from_distance` to `calculate_sl_from_pips`, accepting pip distance (`sl_distance_pips`) instead of price distance. Updated `trade_manager.check_and_apply_auto_sl` to call the corrected method with the `auto_sl_risk_pips` config value.
- **Rationale:** Aligns AutoSL calculation with the pip-based configuration (`auto_sl_risk_pips`) and ensures consistency with other pip-based features (AutoTP, TSL, AutoBE).
- **Implications:** AutoSL now correctly uses the configured pip distance. No config changes needed as `auto_sl_risk_pips` was already present.

---

### [2025-04-11 16:48:00] - Fix Adjusted Entry Price Calculation to Use Pips
- **Decision:** Modified `trade_calculator.calculate_adjusted_entry_price` to correctly use pip-based offset (`entry_price_offset_pips`) instead of assuming a fixed price conversion. Renamed config key from `entry_price_offset` to `entry_price_offset_pips` and updated `config_service.py`.
- **Rationale:** Ensures entry price adjustments for spread/offset are calculated correctly based on symbol's point value and configured pips.
- **Implications:** Requires users to update `config.ini` key name. Calculation is now accurate for XAUUSD.

---

### [2025-04-11 16:49:00] - Add Initial AutoSL Application During Order Placement
- **Decision:** Added logic to `event_processor.process_new_signal` to check for missing SL in the signal and apply AutoSL (using `calculate_sl_from_pips` and `auto_sl_risk_pips`) *before* placing the order.
- **Rationale:** Ensures trades are protected by an SL immediately upon execution if the signal lacks one, rather than waiting for the delayed check in `TradeManager`.
- **Implications:** Reduces the window of vulnerability where a trade might exist without an SL. The delayed check in `TradeManager` remains as a fallback.

---

### [2025-04-11 17:10:50] - Full Codebase Inspection and Debug
- **Decision:** Performed a comprehensive inspection and debug of all core modules (trade_calculator, trade_manager, mt5_executor, event_processor, config_service, etc.) to ensure robust, pip-based trade management.
- **Rationale:** User requested a thorough review to guarantee all calculations, config usage, and error handling are pip-standardized and robust.
- **Implications:** All SL, TP, BE, TSL, and entry price calculations now use pip-based config values and correct symbol info. Spread and offset are consistently handled. All unit tests for pip-based trade management pass, covering critical paths and edge cases. No legacy USD-based logic remains. Documentation and Memory Bank updated to reflect the current state.

---

### [2025-04-13 05:56:08] - Add Early Duplicate Check in Signal Processing
- **Decision:** Moved the duplicate message check using `duplicate_checker.is_processed` to the beginning of the `event_processor.process_new_signal` function.
- **Rationale:** Prevents unnecessary processing (LLM analysis, decision logic, etc.) for messages that have already been handled. Improves efficiency and avoids potential side effects from reprocessing. This was identified as missing during integration testing.
- **Implications:** Duplicate signals are now rejected earlier in the workflow.

---

### [2025-04-13 21:37] - Refactor TP Assignment System
- **Decision:** Refactored the TP assignment system (`src/tp_assignment.py`) to support only three modes: `none` (no TP), `first_tp_first_trade` (first signal TP for first trade only), and `custom_mapping` (user-defined mapping of signal TPs to trades). Removed `fixed`, `risk-ratio`, and `custom` (function-based) modes.
- **Rationale:** Simplify TP logic based on user requirements and remove unused strategies.
- **Implications:** Requires a `[TPAssignment]` section in `config.ini` with `mode` set to one of the three supported values. `custom_mapping` mode requires a `mapping` parameter (e.g., `mapping = 0, none, 1`). Obsolete config parameters (`fixed_tps`, `risk_ratio`, `custom_func`, `tp_sequence`) are no longer used. Code in `event_processor.py` and `trade_execution_strategies.py` updated to use the new system. `SequenceMapper` removed.