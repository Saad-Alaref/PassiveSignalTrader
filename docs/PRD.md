# Product Requirements Document (PRD): Telegram Signal Follower Bot for MT5

## 1. Introduction

### 1.1 Purpose
This document outlines the requirements for a Python application designed to automatically execute trading signals, received from a specific Telegram channel, on a MetaTrader 5 (MT5) account.

### 1.2 Project Goal
To create a reliable bot that monitors a Telegram channel, parses XAUUSD trading signals posted within it, and executes these trades (including entry type, Stop Loss (SL), and Take Profit (TP)) on a user-specified MT5 account.

### 1.3 Target User
The primary user of this application (managing configuration, running the bot).

### 1.4 Scope Overview
The bot will focus solely on XAUUSD signals from the specified channel, executing them via the official MT5 Python integration. Initial development and execution will be on a Windows 11 Desktop.

## 2. Goals & Objectives

*   **2.1 Monitor Telegram:** Continuously monitor the designated Telegram channel for new messages in near real-time.
*   **2.2 LLM Signal Analysis:** Utilize a Google Gemini LLM to analyze relevant Telegram messages to identify potential XAUUSD trading signals, extract parameters (Action, Entry, SL, TP), and assess market sentiment.
*   **2.3 Execute Trades:** Translate parsed signal parameters into precise trade orders (Market or Pending) on the connected MT5 account.
*   **2.4 Trade Decision Logic:** Implement logic to decide whether to execute an identified signal based on a combination of LLM-derived sentiment and a basic price action check (details in `Trading_Logic.md`).
*   **2.5 Manage Orders:** If a trade is approved by the decision logic, correctly set the Stop Loss and Take Profit levels based on the LLM-extracted parameters.
*   **2.6 Manage Lot Size:** Calculate the trade volume (lot size) based on rules defined in `Trading_Logic.md` (e.g., fixed value, risk percentage). Default 0.01 initially.
*   **2.7 Prevent Duplicates:** Ensure that the same Telegram signal message does not trigger multiple trades.
*   **2.8 Robust Logging:** Maintain detailed logs of operations, including received messages, LLM interactions, decision outcomes, executed trades, and errors.
*   **2.9 Configurability:** Allow easy configuration of Telegram channel, MT5 credentials, Gemini API Key, risk parameters (for lot sizing), and decision logic parameters.

## 3. Scope

### 3.1 In Scope:
*   Connecting to Telegram using user-provided credentials (API ID/Hash or User Session).
*   Monitoring a single, specified public or private Telegram channel.
*   Analyzing relevant messages using Google Gemini to identify XAUUSD signals, extract parameters (Action, Entry, SL, TP), and determine market sentiment.
*   Applying decision logic (based on LLM sentiment and price action check) to approve or reject potential trades.
*   Executing approved Market Orders (BUY/SELL).
*   Placing approved Pending Orders (BUY LIMIT, SELL LIMIT, BUY STOP, SELL STOP) based on LLM-extracted parameters.
*   Calculating trade Volume/Lot Size for approved trades based on rules in `Trading_Logic.md`.
*   Setting SL and TP levels provided in the signal.
*   Basic duplicate message handling (based on message ID).
*   Comprehensive logging to console and/or file.
*   Running on Windows 11 via Python script interacting with a running MT5 terminal.

### 3.2 Out of Scope:
*   Monitoring multiple Telegram channels simultaneously.
*   Trading symbols other than XAUUSD.
*   Handling signals that modify existing trades (e.g., "move SL to entry").
*   Advanced order management (e.g., script-managed trailing stops, partial closes).
*   Complex risk management strategies beyond the defined lot sizing rules.
*   Dynamic adjustment of SL/TP based on confidence or other factors.
*   Backtesting capabilities.
*   Graphical User Interface (GUI).
*   Deployment on server environments (addressed post-development).

## 4. Functional Requirements

### FR1: System Initialization:
*   **FR1.1:** Load configuration from config file/module (Telegram API details, Channel ID, MT5 Credentials, Gemini API Key, Trading/Decision parameters).
*   **FR1.2:** Initialize logging system as defined in `logger_setup.py`.

### FR2: Telegram Connection & Monitoring:
*   **FR2.1:** Establish and maintain a connection to Telegram using specified credentials.
*   **FR2.2:** Subscribe to and receive new messages from the target Telegram channel.
*   **FR2.3:** Handle potential Telegram connection errors and attempt reconnection.
*   **FR2.4:** Handle various message types:
    *   Process the text content of new, standalone messages as the primary input.
    *   If the configured Gemini model supports multimodal input, send accompanying image data along with text to the API. Otherwise, ignore image data.
    *   Handling of message replies requires correlation to the original message (see FR10). Processing of standalone replies without clear context to a signal may be ignored initially.
    *   Message edits relevant to active signals should be processed (see FR10).

### FR3: LLM Signal Analysis:
*   **FR3.1:** Send relevant incoming Telegram message content (including image data if supported and applicable per FR2.4) to the configured Google Gemini API.
*   **FR3.2:** Prompt the LLM to reliably distinguish actionable XAUUSD trading signals (BUY/SELL with necessary parameters) from market commentary, performance reports, promotions, and general chat, based on message content and context (referencing examples in `docs/Message Examples.md`).
*   **FR3.3:** If a signal is identified, prompt the LLM to extract key parameters (Action, Symbol [verify XAUUSD], Entry Price/Type, SL Price, TP Price) and provide a market sentiment score/analysis. Use structured output if possible.
*   **FR3.4:** The LLM extraction should handle variations like "zone" entries (e.g., "Zone 3106 - 3108") by providing a single representative entry price (e.g., the midpoint, or as defined in `Trading_Logic.md`).
*   **FR3.5:** Validate the format and basic plausibility of LLM-extracted parameters (e.g., prices are numeric). Log LLM interaction details and extraction success/failure.

### FR4: Trade Decision Logic:
*   **FR4.1:** Retrieve the LLM sentiment score for the identified signal.
*   **FR4.2:** Perform a basic price action check (determining pending order type based on current price vs. signal price, as defined in `Trading_Logic.md`, resulting in a score e.g., 1.0 or 0.0).
*   **FR4.3:** Apply weighting (e.g., 50% LLM sentiment, 50% price action check) to determine if the trade should proceed.
*   **FR4.4:** Log the decision outcome (Approved/Rejected) and the factors contributing to it.

### FR5: Duplicate Signal Prevention:
*   **FR4.1:** Maintain a record (e.g., in memory, file, or simple DB) of processed Telegram message IDs.
*   **FR5.2:** Before submitting a signal to the LLM analysis (FR3), check if the originating message ID has already been processed. If yes, log and ignore. If no, proceed and record the ID *after* successful processing/decision.

### FR6: MT5 Connection:
*   **FR5.1:** Establish and maintain a connection to the running MT5 terminal using configured credentials.
*   **FR5.2:** Verify successful login and connection. Handle connection errors, and reconnections.

### FR7: Trade Preparation (Post-Approval):
*   **FR6.1:** Verify XAUUSD symbol is available and tradable on the MT5 account.
*   **FR7.2:** If the trade is approved by the Decision Logic (FR4), calculate the appropriate trade Volume (Lot Size) based on rules defined in `Trading_Logic.md`.
*   **FR7.3:** Construct the trade request dictionary using the LLM-extracted parameters (Action, Symbol, Order Type, Price, SL, TP) and the calculated Volume.

### FR8: Trade Execution:
*   **FR8.1:** Send the prepared trade request to the MT5 terminal using `order_send()`. Includes handling for requotes via a timed retry mechanism (details in `Trading_Logic.md`).
*   **FR7.2:** Check the result returned by `order_send()` for success or failure.
*   **FR7.3:** Log detailed results of the trade attempt (success/failure codes, order ticket number if successful, error messages).

### FR9: Application Lifecycle:
*   **FR8.1:** Run continuously until manually stopped.
*   **FR9.2:** Ensure graceful shutdown, closing Telegram and MT5 connections properly upon termination.

### FR10: Signal State Management & Edit Handling:
*   **FR10.1:** Maintain state for trades initiated based on signals that might be incomplete (e.g., placed without a TP as per `Trading_Logic.md`). Associate this state with the original Telegram message ID and the MT5 order ticket.
*   **FR10.2:** Monitor Telegram events for message edits and new messages that are replies.
*   **FR10.3:** If an edit occurs on a message corresponding to an active, incomplete signal state, re-analyze the edited content with the LLM.
*   **FR10.4:** If a new message is identified as a reply to an original signal message associated with an active, incomplete state, analyze the reply content with the LLM.
*   **FR10.5:** If the analysis (FR10.3 or FR10.4) yields previously missing parameters (e.g., TP), modify the corresponding open MT5 order accordingly (e.g., using `order_modify()`). Define a reasonable timeout for waiting for such updates.

## 5. Non-Functional Requirements

*   **NFR1: Performance:** Aim for minimal latency (ideally under 2-3 seconds) between receiving a valid Telegram signal message and sending the corresponding order to MT5.
*   **NFR2: Reliability:** The application should run stably for extended periods. It must handle minor network interruptions gracefully (attempt reconnects). Parsing must be reliable for the defined signal format.
*   **NFR3: Security:** Telegram and MT5 credentials must not be hardcoded directly in scripts. Use configuration files (excluded from version control) or environment variables.
*   **NFR4: Maintainability:** Code must be modular (as per architecture plan), well-commented, and adhere to Python best practices (PEP 8).
*   **NFR5: Usability:** Configuration should be straightforward. Logs should be clear and informative for debugging.

## 6. System Architecture (Proposed)

### Core Components:
*   **Telegram Monitor:** Connects to Telegram (using Telethon/Pyrogram), listens to the channel.
*   **LLM Interface (Gemini):** Handles communication (API calls, prompting) with the Google Gemini API.
*   **Signal Analyzer/Parser (LLM-based):** Uses the LLM Interface to analyze messages, identify signals, extract parameters, and get sentiment.
*   **Duplicate Checker / State Manager:** Tracks processed message IDs.
*   **MT5 Connector:** Manages connection/login to MT5 terminal.
*   **MT5 Data Fetcher:** Gets current prices, account info, symbol specs needed for decision logic and lot sizing.
*   **Decision Logic:** Implements the 50/50 logic (or other defined rules) using LLM sentiment and price action data.
*   **Trade Parameter Calculator:** Determines lot size for approved trades based on `Trading_Logic.md`.
*   **MT5 Trade Executor:** Builds and sends trade requests to MT5 API.
*   **Main Orchestrator:** Ties all components together, handles the flow.
*   **Configuration Module:** Loads settings.
*   **Logging Module:** Provides logging services.

### Interaction Flow:
Telegram Message -> Monitor -> Duplicate Check (Initial) -> LLM Analyzer -> Decision Logic -> [If Approved] -> Trade Parameter Calc -> Trade Executor -> MT5. All steps logged.

## 7. Data Requirements

*   **Input:** Text messages from Telegram channel, Configuration settings.
*   **Output:** Trade orders sent to MT5, Log files.
*   **State Data:** Set of processed Telegram message IDs, MT5 connection status, Telegram connection status.

## 8. Assumptions & Dependencies

*   User possesses necessary Telegram API ID/Hash or can authenticate a user session via Telethon/Pyrogram.
*   User account has access to read the specified Telegram channel.
*   MT5 terminal (Windows) is running, logged into the correct account, and "Allow Algo Trading" is enabled.
*   Required Python libraries (`MetaTrader5`, `Telethon` or `Pyrogram`, etc.) are installed.
*   Network connectivity is available for both Telegram and MT5 communication.
*   The XAUUSD symbol name on the broker's MT5 platform is known and configured.

## 9. Open Questions / Future Considerations

*   Exact method for handling Telegram authentication (API vs User)?
*   Detailed error handling strategy (retries, notifications)?
*   Specific algorithm for lot size calculation? (To be defined in `Trading_Logic.md`)
*   Handling potential partial fills or requotes from MT5?
*   Scalability if needing to monitor more channels or symbols later? (Out of scope for now).
*   Long-term deployment environment and strategy (TBD).

## 10. Supporting Documents

*   **`docs/Signal_Format.md`:** Provides examples of typical signal messages to help guide LLM prompting and testing. Not a strict format definition for parsing.
*   **`docs/Trading_Logic.md`:** This document outlines rules applied by the bot:
    *   How to calculate lot size (e.g., fixed value, % equity).
    *   Definition of the "basic price action check" used in the decision logic.
    *   Parameters for the decision logic (e.g., weighting factors).
    *   Default behavior if the LLM fails to extract required parameters or provide sentiment.
    *   Any pre-trade checks (e.g., max spread tolerance).
    *   How to handle signals with multiple TP levels (e.g., always use TP1).