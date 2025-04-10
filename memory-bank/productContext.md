# Product Context

This document captures the high-level project description, goals, features, and overall architecture.

---

## Overview

The **AI-Trader Bot** is an automated trading system that connects a Telegram channel with a MetaTrader 5 (MT5) account. It uses a Large Language Model (LLM, e.g., Google Gemini) to analyze Telegram messages, extract trading signals, and execute trades on MT5 with minimal latency and high reliability.

---

## Goals & Objectives

- **Automate trade execution** for XAUUSD signals received via Telegram.
- **Leverage LLMs** to parse unstructured messages, extract actionable trade parameters (action, entry, SL, TP), and assess sentiment.
- **Implement decision logic** combining LLM sentiment and price action checks to approve or reject trades.
- **Support both market and pending orders** with correct order types.
- **Prevent duplicate trades** from the same signal.
- **Provide detailed logging** and maintain application state.
- **Allow easy configuration** of credentials, risk parameters, and decision logic weights.

---

## Key Features

- **Telegram Monitoring:** Real-time listening to a specified channel.
- **LLM Signal Analysis:** Distinguish actionable signals from commentary, extract structured data.
- **Trade Decision Logic:** Weighted approval based on sentiment and price action.
- **MT5 Integration:** Place, modify, and close trades via official MT5 Python API.
- **Duplicate Prevention:** Avoid multiple executions of the same signal.
- **Trade Management:** Handle SL/TP, trailing stops, break-even moves, and updates.
- **Configurable:** Via `config.ini` for API keys, risk, weights, and thresholds.
- **Async Architecture:** Efficient concurrent handling of events and background tasks.

---

## Scope

- **In Scope:**
  - Single Telegram channel
  - XAUUSD symbol only
  - Market and pending orders
  - Basic lot sizing (fixed, default 0.01)
  - Logging and state management
  - Windows 11 environment

- **Out of Scope:**
  - Multi-symbol or multi-channel support
  - Advanced risk management
  - GUI interface
  - Backtesting
  - Server deployment (future)

---

## Architecture Overview

- **Telegram Monitor:** Listens for new messages.
- **LLM Interface:** Sends messages to Gemini, parses responses.
- **Signal Analyzer:** Extracts structured trade data.
- **Duplicate Checker:** Prevents reprocessing.
- **Decision Logic:** Combines sentiment and price action.
- **Trade Calculator:** Determines lot size, SL, TP.
- **MT5 Connector:** Manages MT5 connection.
- **MT5 Executor:** Places and manages orders.
- **Event Processor:** Orchestrates the workflow.
- **Trade Manager:** Manages active trades.
- **State Manager:** Maintains runtime state.
- **Config Service:** Loads and manages configuration.
- **Logger:** Centralized logging.
- **Async Tasks:** Background monitoring, summaries, confirmations.

---

## Assumptions & Dependencies

- User has Telegram and MT5 credentials.
- MT5 terminal is running and logged in.
- Required Python libraries installed.
- Network connectivity is stable.
- XAUUSD symbol is available on broker.

---

## Open Questions / Future Considerations

- Advanced lot sizing based on risk.
- Multi-symbol and multi-channel support.
- Deployment on server/cloud.
- Enhanced error handling and notifications.
- GUI or web interface.