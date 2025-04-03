# Trading Logic Rules

This document outlines the specific rules the bot uses for trade execution parameter calculation and decision-making.

## 1. Lot Size Calculation

for now the lot will always be 0.01.

**Options for the future (wont be implemeted now):**

*   **Fixed Lot Size:**
    *   Specify the fixed value (e.g., `0.01`).
*   **Risk Percentage of Account Equity:**
    *   Specify the percentage (e.g., `1.0` for 1%).
    *   Requires fetching account equity from MT5.
    *   Requires calculating stop loss distance in pips/points.
    *   Requires knowing the contract size and value per point/pip for the symbol (XAUUSD).
    *   Formula: `Lot Size = (Account Equity * Risk Percentage) / (Stop Loss Distance in Points * Value Per Point Per Lot)`
*   **Risk Percentage of Account Balance:**
    *   Similar to equity, but uses balance. Specify percentage.
*   **Other Method:**
    *   Describe any other custom calculation method.

**Selected Method:** *(Clearly state the chosen method here, e.g., Fixed Lot Size)*

**Parameters Needed:** *(List parameters required for the chosen method, e.g., Fixed Lot Value = 0.01)*

**Default Lot Size:** If the primary calculation method fails or is not specified, use `config[Trading][default_lot_size]` (e.g., 0.01).

## 2. Trade Decision Logic

*(This section defines whether an LLM-identified signal results in a trade order.)*

### 2.1 Market Execution Signals (e.g., "BUY NOW", "SELL NOW")

*   **Identification:** The LLM identifies the signal as requiring immediate market execution.
*   **Decision:** These signals **bypass** the weighted decision logic.
*   **Action:** If identified, proceed directly to Lot Size Calculation (Section 1) and Trade Execution (Section 7 of PRD).

### 2.2 Pending Order Signals (Specified Entry Price, e.g., "BUY @ 2910.00")

*   **Identification:** The LLM identifies the signal specifying a target entry price.
*   **Decision:** These signals **require approval** based on a weighted combination of LLM Sentiment and a Price Action Check.
    *   **A. Price Action Check (Weight configurable, e.g., 0.5):**
        *   Fetch the current market price (Ask for BUY signals, Bid for SELL signals) from MT5 (`mt5_data_fetcher`).
        *   Compare the signal's entry price (`signal_price`) to the current market price (`current_price`).
        *   Determine the correct MT5 pending order type:
            *   If `Action == BUY` and `signal_price < current_price`: `ORDER_TYPE_BUY_LIMIT`
            *   If `Action == BUY` and `signal_price > current_price`: `ORDER_TYPE_BUY_STOP`
            *   If `Action == SELL` and `signal_price > current_price`: `ORDER_TYPE_SELL_LIMIT`
            *   If `Action == SELL` and `signal_price < current_price`: `ORDER_TYPE_SELL_STOP`
        *   **Outcome:** This check primarily determines the *correct order type*. For weighting, assign a score (e.g., 1.0 if a valid type is determined, 0.0 otherwise, or potentially more nuanced based on proximity - TBD).
    *   **B. LLM Sentiment Check (Weight configurable, e.g., 0.5):**
        *   Retrieve the sentiment score provided by the LLM for this signal (e.g., a value between -1.0 for very negative and +1.0 for very positive).
        *   Normalize or map this score to the decision weighting scale if necessary (e.g., map [-1, 1] to [0, 1]).
        *   **Outcome:** A numerical score representing LLM confidence/sentiment.
    *   **C. Combined Decision:**
        *   Calculate the weighted score: `Total Score = (Price Action Score * price_action_weight) + (LLM Sentiment Score * sentiment_weight)`
        *   Compare `Total Score` to a configurable approval threshold (e.g., `0.6`).
        *   **Approval:** If `Total Score >= approval_threshold`, the trade is approved.
        *   **Rejection:** If `Total Score < approval_threshold`, the trade is rejected. Log the reason.
*   **Action:**
    *   If **Approved**: Proceed to Lot Size Calculation (Section 1) and Trade Execution (Section 7 of PRD), using the determined pending order type.
    *   If **Rejected**: Log the rejection and take no further action on this signal.

**Configuration Parameters:**
*   `config[DecisionLogic][sentiment_weight]`
*   `config[DecisionLogic][price_action_weight]`
*   `config[DecisionLogic][approval_threshold]` *(Needs to be added to config)*

## 3. Default Behaviors

*(Define how the bot should behave if certain information is missing or ambiguous.)*

*   **LLM Fails to Extract Parameters:**
    *   If essential parameters (Action, Price, SL, TP) are missing after LLM analysis, it might be that the signal was sent incomplete (like: BUY NOW) it will execute trade based on the previous logic. If it was a market order (not market execution) it will wait for telegram message to be edited, because the sender will occasionally send a missing signal, and then complete it by editing the same message. The app will need to be aware of this scenario.
*   **LLM Fails to Provide Sentiment (for Pending Orders):**
    *   Assign a neutral sentiment score 0.5

*   **Missing Stop Loss (LLM Extraction):**
    *   Reject the trade (Essential for risk calculation)

*   **Missing Take Profit (LLM Extraction):**
    *   Proceed without TP
    *   Wait for the message to be edited, and when a TP is specified, edit the trade to add he TP


## 4. Pre-Trade Checks (Applied *after* decision approval, before execution)

*(Define any final checks performed *before* sending the order to MT5.)*

*   **Maximum Spread Tolerance:**
    *   Fetch current spread from MT5 (`mt5_data_fetcher`).
    *   Specify maximum allowed spread in points/pips (e.g., `30` points).
    *   If current spread exceeds tolerance, cancel trade execution, Log warning.

*   **Trading Hours Check:**
    *   Check if the market for XAUUSD is currently open? (Requires MT5 symbol info).

*   **Maximum Slippage (Market Orders Only):**
    *   Specify allowed slippage in points when sending market orders (parameter in `order_send`).

*   **Account State Check:**
    *   Check if sufficient free margin exists for the calculated lot size

## 5. Handling Multiple TP Levels

*   Instruct LLM to extract only TP1. Execute with TP1. *(Simplest)*


## 6. Error Handling during Execution

*(Define basic responses to MT5 execution errors)*

*   **Requotes:** Log the requote, retry automatically, multiple times, for 20 seconds, then abort if still not successful.
*   **Insufficient Funds:** Log error.
*   **Connection Issues:** Log error, rely on `mt5_connector` reconnection logic.
