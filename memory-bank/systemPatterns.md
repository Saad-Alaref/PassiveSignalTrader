# System Patterns

This document catalogs architectural patterns, design principles, and best practices used in the project.

---

## Architectural Patterns

- **Event-Driven Async Architecture:**  
  The bot uses Python's `asyncio` to handle Telegram events, MT5 API calls, LLM requests, and background tasks concurrently, ensuring responsiveness and scalability.

- **Modular Clean Architecture:**  
  The system is split into clear modules (Telegram, LLM, MT5, Decision Logic, Trade Management, State, Config, Logging) with well-defined interfaces, promoting maintainability and testability.

- **Pipeline Workflow:**  
  Incoming Telegram messages flow through deduplication → LLM analysis → decision logic → trade calculation → pre-checks → execution, with clear logging and error handling at each stage.

- **Strategy Pattern for Trade Execution:**  
  Different execution strategies (single trade, distributed limits, multi-market stop) are encapsulated in interchangeable classes, selected dynamically based on signal type.

- **Command Pattern for Trade Updates:**  
  Trade update actions (modify SL/TP, close, cancel, partial close) are encapsulated as commands, simplifying orchestration and extension.

---

## Design Principles

- **Separation of Concerns:**  
  Parsing, decision-making, execution, and state management are decoupled.

- **Configuration-Driven:**  
  Behavior is controlled via `config.ini` (weights, thresholds, credentials), avoiding hardcoding.

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
  Designed to parse zones, multiple TPs, missing info, and edits/replies to incomplete signals.

---

## Trade Decision Logic Pattern

- **Bypass for Market Orders:**  
  Immediate execution if LLM identifies a market order.

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