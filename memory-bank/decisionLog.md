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