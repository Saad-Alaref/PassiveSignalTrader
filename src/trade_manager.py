import logging
import MetaTrader5 as mt5
from datetime import datetime, timezone, timedelta
from .state_manager import StateManager # Use relative import
from .mt5_executor import MT5Executor # Use relative import
from .trade_calculator import TradeCalculator # Use relative import
from .telegram_sender import TelegramSender # Use relative import
from .models import TradeInfo # Import the dataclass
from .config_service import config_service # Import the service

logger = logging.getLogger('TradeBot')

class TradeManager:
    """
    Manages trade-related operations like applying AutoSL, AutoBE, and handling TPs.
    """

    def __init__(self, config_service_instance, state_manager: StateManager, mt5_executor: MT5Executor, # Inject service
                 trade_calculator: TradeCalculator, telegram_sender: TelegramSender,
                 mt5_fetcher):
        """
        Initializes the TradeManager.

        Args:
            config (configparser.ConfigParser): The application configuration (shared reference).
            state_manager (StateManager): Instance for accessing trade state.
            mt5_executor (MT5Executor): Instance for modifying trades.
            trade_calculator (TradeCalculator): Instance for calculating SL/TP prices.
            telegram_sender (TelegramSender): Instance for sending notifications.
            mt5_fetcher (MT5DataFetcher): Instance for fetching market data.
        """
        self.config_service = config_service_instance # Store service instance
        self.state_manager = state_manager
        self.mt5_executor = mt5_executor
        self.trade_calculator = trade_calculator
        self.telegram_sender = telegram_sender
        self.mt5_fetcher = mt5_fetcher # Store fetcher
        logger.info("TradeManager initialized.")

    async def check_and_apply_auto_sl(self, position, trade_info: TradeInfo): # Type hint
        """
        Checks a specific trade pending AutoSL and applies SL if conditions are met.
        Called periodically by the main monitor task.

        Args:
            position (mt5.PositionInfo): The current position data from MT5.
            trade_info (TradeInfo): The internally tracked trade data object from StateManager.
        """
        # --- Read AutoSL config dynamically ---
        enable_auto_sl = self.config_service.getboolean('AutoSL', 'enable_auto_sl', fallback=False) # Use service
        if not enable_auto_sl:
            return # Feature disabled

        auto_sl_delay_sec = self.config_service.getint('AutoSL', 'auto_sl_delay_seconds', fallback=60) # Use service
        auto_sl_distance = self.config_service.getfloat('AutoSL', 'auto_sl_price_distance', fallback=5.0) # Use service
        # --- End Read AutoSL config ---

        now_utc = datetime.now(timezone.utc)

        # Check if this specific trade is marked for AutoSL
        pending_timestamp = trade_info.auto_sl_pending_timestamp # Use attribute access
        if not pending_timestamp:
            return # Not pending for this trade

        ticket = trade_info.ticket # Use attribute access
        log_prefix_auto_sl = f"[AutoSL Check][Ticket: {ticket}]"
        needs_flag_removal = False # Flag to indicate if pending flag should be removed after checks

        # Check if delay has passed
        if now_utc < pending_timestamp + timedelta(seconds=auto_sl_delay_sec):
            logger.debug(f"{log_prefix_auto_sl} AutoSL delay not yet passed.")
            return # Delay not yet passed, keep flag

        logger.info(f"{log_prefix_auto_sl} AutoSL delay passed. Checking trade status...")

        # Verify trade exists (using position passed in) and still has no SL
        if not position: # Should not happen if called correctly
            logger.error(f"{log_prefix_auto_sl} Position data missing. Cannot apply AutoSL.")
            needs_flag_removal = True # Remove flag as we can't process
        else:
            current_sl = position.sl
            trade_type = position.type
            volume = position.volume
            entry_price = position.price_open # Use actual open price
            symbol = position.symbol
            logger.debug(f"{log_prefix_auto_sl} Position details: SL={current_sl}, Type={trade_type}, Vol={volume}, Entry={entry_price}")

            # Check if SL was added manually or by another update
            if current_sl is not None and current_sl != 0.0:
                logger.info(f"{log_prefix_auto_sl} SL ({current_sl}) already exists. Removing from AutoSL check.")
                needs_flag_removal = True

        # --- Conditions met: Apply Auto SL ---
        if not needs_flag_removal: # Only proceed if SL doesn't exist and position is valid
            logger.info(f"{log_prefix_auto_sl} Applying AutoSL (Distance: {auto_sl_distance})...")

            # Basic validation of fetched position data
            if entry_price is None or entry_price == 0.0:
                 logger.error(f"{log_prefix_auto_sl} Cannot apply AutoSL: Entry price is invalid ({entry_price}).")
                 needs_flag_removal = True # Avoid retrying
            elif volume is None or volume <= 0:
                 logger.error(f"{log_prefix_auto_sl} Cannot apply AutoSL: Invalid volume ({volume}).")
                 needs_flag_removal = True # Avoid retrying
            elif trade_type is None or trade_type not in [mt5.ORDER_TYPE_BUY, mt5.ORDER_TYPE_SELL]:
                 logger.error(f"{log_prefix_auto_sl} Cannot apply AutoSL: Invalid trade type ({trade_type}).")
                 needs_flag_removal = True # Avoid retrying
            else:
                # Calculate SL price using the fixed distance
                auto_sl_price = self.trade_calculator.calculate_sl_from_distance(
                    symbol=symbol,
                    order_type=trade_type,
                    entry_price=entry_price,
                    sl_price_distance=auto_sl_distance
                )

                if auto_sl_price is None:
                    logger.error(f"{log_prefix_auto_sl} Failed to calculate AutoSL price.")
                    needs_flag_removal = True # Remove flag on calculation failure for now
                else:
                    # Apply the calculated SL
                    modify_success = self.mt5_executor.modify_trade(ticket=ticket, sl=auto_sl_price)

                    if modify_success:
                        logger.info(f"{log_prefix_auto_sl} Successfully applied AutoSL: {auto_sl_price}")
                        needs_flag_removal = True # Mark as done

                        # Send notifications
                        entry_price_str = f"@{entry_price}" if entry_price is not None else "Market"
                        status_msg_auto_sl = f"🤖 <b>Auto StopLoss Applied</b>\n<b>Ticket:</b> <code>{ticket}</code> (Entry: {entry_price_str})\n<b>New SL:</b> <code>{auto_sl_price}</code> (Distance: {auto_sl_distance})"
                        await self.telegram_sender.send_message(status_msg_auto_sl, parse_mode='html')
                        debug_channel_id = getattr(self.telegram_sender, 'debug_target_channel_id', None)
                        if debug_channel_id:
                             await self.telegram_sender.send_message(f"🤖 {log_prefix_auto_sl} Applied AutoSL: {auto_sl_price}", target_chat_id=debug_channel_id)
                    else:
                        logger.error(f"{log_prefix_auto_sl} Failed to apply AutoSL price {auto_sl_price} via modify_trade.")
                        # Keep pending flag, maybe modification works next time?

        # Remove pending flag if processed or invalid
        if needs_flag_removal:
            self.state_manager.remove_auto_sl_pending_flag(ticket)


    async def check_and_apply_auto_be(self, position, trade_info: TradeInfo): # Type hint
        """
        Checks if a trade's profit meets the threshold and moves SL to breakeven.
        Called periodically by the main monitor task.

        Args:
            position (mt5.PositionInfo): The current position data from MT5.
            trade_info (TradeInfo): The internally tracked trade data object from StateManager.
        """
        # --- Read AutoBE config dynamically ---
        enable_auto_be = self.config_service.getboolean('AutoBE', 'enable_auto_be', fallback=False) # Use service
        if not position or not trade_info:
            logger.error("AutoBE check missing position or trade_info.")
            return

        ticket = position.ticket
        log_prefix_auto_be = f"[AutoBE Check][Ticket: {ticket}]"

        if not enable_auto_be:
            return # Feature disabled

        if trade_info.auto_be_applied:
            logger.debug(f"{log_prefix_auto_be} AutoBE already applied. Skipping.")
            return

        profit_threshold_config = self.config_service.getfloat('AutoBE', 'auto_be_profit_usd', fallback=3.0) # Use service
        base_lot_size = self.config_service.getfloat('Trading', 'base_lot_size_for_usd_targets', fallback=0.01) # Use service

        if profit_threshold_config <= 0:
            logger.warning("AutoBE profit threshold (auto_be_profit_usd) is zero or negative, disabling check.")
            return
        if base_lot_size <= 0:
             logger.warning("Trading base_lot_size_for_usd_targets is zero or negative. Cannot scale AutoBE threshold.")
             return
        # --- End Read AutoBE config ---

        current_profit = position.profit
        current_sl = position.sl
        entry_price = position.price_open
        trade_type = position.type

        # Calculate scaling factor and adjusted threshold
        scaling_factor = position.volume / base_lot_size
        adjusted_be_threshold = profit_threshold_config * scaling_factor
        logger.debug(f"{log_prefix_auto_be} BaseLot={base_lot_size}, PosVol={position.volume}, ScaleFactor={scaling_factor:.2f}, ConfigThreshold=${profit_threshold_config:.2f}, AdjustedThreshold=${adjusted_be_threshold:.2f}")

        # Check if profit threshold is met using the adjusted value
        if current_profit < adjusted_be_threshold:
            # logger.debug(f"{log_prefix_auto_be} Profit {current_profit:.2f} < Adjusted Threshold {adjusted_be_threshold:.2f}. No action.")
            return

        # Check if SL is already at breakeven or better
        sl_is_at_or_better_than_be = False
        if current_sl is not None and current_sl != 0.0:
            if trade_type == mt5.ORDER_TYPE_BUY and current_sl >= entry_price:
                sl_is_at_or_better_than_be = True
            elif trade_type == mt5.ORDER_TYPE_SELL and current_sl <= entry_price:
                sl_is_at_or_better_than_be = True

        if sl_is_at_or_better_than_be:
            logger.debug(f"{log_prefix_auto_be} SL ({current_sl}) is already at or better than breakeven ({entry_price}). No action.")
            return

        # --- Conditions met: Apply Auto BE ---
        logger.info(f"{log_prefix_auto_be} Profit {current_profit:.2f} >= Adjusted Threshold {adjusted_be_threshold:.2f}. Attempting to move SL to Breakeven ({entry_price}).")

        # Adjust BE SL for spread + offset
        be_sl = self.mt5_executor._adjust_sl_for_spread_offset(entry_price, trade_type, position.symbol)

        modify_success = self.mt5_executor.modify_trade(ticket=ticket, sl=be_sl)

        if modify_success:
            logger.info(f"{log_prefix_auto_be} Successfully moved SL to Breakeven: {entry_price}")
            trade_info.auto_be_applied = True
            # Send notifications
            entry_price_str = f"@{entry_price}"
            status_msg_auto_be = f"🛡️ <b>Auto Breakeven Applied</b>\n<b>Ticket:</b> <code>{ticket}</code> (Entry: {entry_price_str})\n<b>New SL:</b> <code>{entry_price}</code> (Profit Trigger: ≥ ${adjusted_be_threshold:.2f})"
            await self.telegram_sender.send_message(status_msg_auto_be, parse_mode='html')
            debug_channel_id = getattr(self.telegram_sender, 'debug_target_channel_id', None)
            if debug_channel_id:
                 await self.telegram_sender.send_message(f"🛡️ {log_prefix_auto_be} Applied AutoBE: SL moved to {entry_price}", target_chat_id=debug_channel_id)
        else:
            logger.error(f"{log_prefix_auto_be} Failed to apply AutoBE via modify_sl_to_breakeven.")


    async def check_and_apply_trailing_stop(self, position, trade_info: TradeInfo): # Type hint
        """
        Checks if a trade qualifies for Trailing Stop Loss (TSL) activation or update,
        and applies the TSL if conditions are met.
        Called periodically by the main monitor task.

        Args:
            position (mt5.PositionInfo): The current position data from MT5.
            trade_info (TradeInfo): The internally tracked trade data object from StateManager.
        """
        # --- Read TrailingStop config dynamically ---
        enable_tsl = self.config_service.getboolean('TrailingStop', 'enable_trailing_stop', fallback=False) # Use service
        if not enable_tsl:
            return # Feature disabled

        activation_profit_config = self.config_service.getfloat('TrailingStop', 'activation_profit_usd', fallback=10.0) # Use service
        trail_distance_config = self.config_service.getfloat('TrailingStop', 'trail_distance_price', fallback=5.0) # Use service
        base_lot_size = self.config_service.getfloat('Trading', 'base_lot_size_for_usd_targets', fallback=0.01) # Use service

        if activation_profit_config <= 0 or trail_distance_config <= 0:
            logger.warning("TrailingStop activation_profit_usd or trail_distance_usd is zero or negative. TSL disabled.")
            return
        if base_lot_size <= 0:
            logger.warning("Trading base_lot_size_for_usd_targets is zero or negative. Cannot scale TSL thresholds.")
            return
        if trail_distance_config >= activation_profit_config:
            logger.warning("TrailingStop trail_distance_usd must be less than activation_profit_usd in config. TSL disabled.")
            return
        # --- End Read TrailingStop config ---

        if not position or not trade_info:
            logger.error("TSL check missing position or trade_info.")
            return

        ticket = position.ticket
        current_profit = position.profit
        current_sl = position.sl
        entry_price = position.price_open
        trade_type = position.type
        volume = position.volume
        symbol = position.symbol
        tsl_active = trade_info.tsl_active # Use attribute access

        log_prefix_tsl = f"[TSL Check][Ticket: {ticket}]"

        # --- Get current market price for TSL calculation ---
        tick = self.mt5_fetcher.get_symbol_tick(symbol)
        if not tick:
            logger.warning(f"{log_prefix_tsl} Could not get current tick for {symbol}. Skipping TSL check.")
            return
        # Price relevant for trailing: If BUY, trail below BID. If SELL, trail above ASK.
        relevant_market_price = tick.bid if trade_type == mt5.ORDER_TYPE_BUY else tick.ask

        # Calculate scaling factor and adjusted thresholds
        scaling_factor = position.volume / base_lot_size
        adjusted_activation_threshold = activation_profit_config * scaling_factor
        # NOTE: trail_distance_usd is now interpreted as price distance, scaling is removed here.
        # The scaling now happens for the *activation threshold* only.
        # adjusted_trail_distance = trail_distance_config * scaling_factor # Removed scaling for distance
        trail_price_distance = trail_distance_config # Use the config value directly as price distance
        logger.debug(f"{log_prefix_tsl} BaseLot={base_lot_size}, PosVol={position.volume}, ScaleFactor={scaling_factor:.2f}")
        logger.debug(f"{log_prefix_tsl} ConfigActivation=${activation_profit_config:.2f}, AdjustedActivation=${adjusted_activation_threshold:.2f}")
        logger.debug(f"{log_prefix_tsl} ConfigTrailDistance=${trail_distance_config:.2f}") # Log the direct distance


        # --- TSL Activation Logic ---
        if not tsl_active:
            # Compare current profit with ADJUSTED activation threshold
            if current_profit >= adjusted_activation_threshold:
                logger.info(f"{log_prefix_tsl} Profit {current_profit:.2f} >= Adjusted Activation Threshold {adjusted_activation_threshold:.2f}. Attempting TSL activation...")

                # Calculate initial TSL price based on current price and FIXED trail price distance
                new_tsl_price = self.trade_calculator.calculate_trailing_sl_price(
                    symbol=symbol,
                    order_type=trade_type,
                    current_price=relevant_market_price, # Use Bid for BUY, Ask for SELL
                    trail_distance_price=trail_price_distance # Use fixed distance
                )

                if new_tsl_price is None:
                    logger.error(f"{log_prefix_tsl} Failed to calculate initial TSL price.")
                    return # Cannot proceed without calculated price

                # --- Sanity Check: Ensure initial TSL locks in *some* profit ---
                # (i.e., SL is better than entry price)
                initial_tsl_locks_profit = False
                # Adjust trailing SL for spread + offset relative to entry price
                adjusted_entry_sl = self.mt5_executor._adjust_sl_for_spread_offset(entry_price, trade_type, symbol)

                if trade_type == mt5.ORDER_TYPE_BUY and new_tsl_price > adjusted_entry_sl:
                    initial_tsl_locks_profit = True
                elif trade_type == mt5.ORDER_TYPE_SELL and new_tsl_price < adjusted_entry_sl:
                    initial_tsl_locks_profit = True

                if not initial_tsl_locks_profit:
                     logger.warning(f"{log_prefix_tsl} Calculated initial TSL price ({new_tsl_price}) does not lock profit (Entry: {entry_price}). Activation condition might be too tight or market moved unfavorably. Will retry next cycle.")
                     return # Don't activate if it doesn't lock profit

                # --- Check if calculated TSL is better than existing SL (if any) ---
                apply_initial_tsl = True
                if current_sl is not None and current_sl != 0.0:
                     if trade_type == mt5.ORDER_TYPE_BUY and current_sl >= new_tsl_price:
                          apply_initial_tsl = False # Existing SL is better or equal
                     elif trade_type == mt5.ORDER_TYPE_SELL and current_sl <= new_tsl_price:
                          apply_initial_tsl = False # Existing SL is better or equal

                if not apply_initial_tsl:
                     logger.info(f"{log_prefix_tsl} Existing SL ({current_sl}) is already better than calculated initial TSL ({new_tsl_price}). Activating TSL flag without modifying SL.")
                     trade_info.tsl_active = True # Use attribute access
                     return

                # --- Apply Initial TSL ---
                logger.info(f"{log_prefix_tsl} Applying initial TSL. Calculated SL: {new_tsl_price}")
                modify_success = self.mt5_executor.modify_trade(ticket=ticket, sl=new_tsl_price)

                if modify_success:
                    logger.info(f"{log_prefix_tsl} Successfully applied initial TSL: {new_tsl_price}")
                    trade_info.tsl_active = True # Use attribute access

                    # Send notifications
                    entry_price_str = f"@{entry_price}"
                    status_msg_tsl_act = f"📈 <b>Trailing Stop Activated</b>\n<b>Ticket:</b> <code>{ticket}</code> (Entry: {entry_price_str})\n<b>Initial SL:</b> <code>{new_tsl_price}</code> (Profit ≥ ${adjusted_activation_threshold:.2f}, Trail Distance: {trail_price_distance})"
                    await self.telegram_sender.send_message(status_msg_tsl_act, parse_mode='html')
                    debug_channel_id = getattr(self.telegram_sender, 'debug_target_channel_id', None)
                    if debug_channel_id:
                         await self.telegram_sender.send_message(f"📈 {log_prefix_tsl} Activated TSL. Initial SL set to {new_tsl_price}", target_chat_id=debug_channel_id)
                else:
                    logger.error(f"{log_prefix_tsl} Failed to apply initial TSL price {new_tsl_price} via modify_trade.")
                    # Don't set tsl_active flag if modification failed

            # else: # Profit below adjusted activation threshold
            #     logger.debug(f"{log_prefix_tsl} Profit {current_profit:.2f} < Adjusted Activation Threshold {adjusted_activation_threshold:.2f}. TSL not active.")

        # --- TSL Update Logic (if already active) ---
        elif tsl_active:
            # Calculate the new potential TSL price based on the current market price and FIXED trail price distance
            new_tsl_price = self.trade_calculator.calculate_trailing_sl_price(
                symbol=symbol,
                order_type=trade_type,
                current_price=relevant_market_price, # Use Bid for BUY, Ask for SELL
                trail_distance_price=trail_price_distance # Use fixed distance
            )

            if new_tsl_price is None:
                logger.error(f"{log_prefix_tsl} Failed to calculate new TSL price for update.")
                return # Cannot proceed

            # --- Check if New TSL is Better than Current SL ---
            # We only move the SL if the new calculated price is more favorable
            # (higher for BUY, lower for SELL) than the current SL.
            move_sl = False
            if current_sl is None or current_sl == 0.0:
                # If there's no current SL (shouldn't happen if TSL is active, but handle defensively),
                # apply the new TSL only if it locks profit.
                # Adjust trailing SL for spread + offset relative to entry price
                adjusted_entry_sl = self.mt5_executor._adjust_sl_for_spread_offset(entry_price, trade_type, symbol)

                if trade_type == mt5.ORDER_TYPE_BUY and new_tsl_price > adjusted_entry_sl:
                    move_sl = True
                elif trade_type == mt5.ORDER_TYPE_SELL and new_tsl_price < adjusted_entry_sl:
                    move_sl = True
                if move_sl: logger.warning(f"{log_prefix_tsl} TSL active but current SL is missing. Applying new TSL {new_tsl_price}.")
            else:
                # Compare new TSL with current SL
                if trade_type == mt5.ORDER_TYPE_BUY and new_tsl_price > current_sl:
                    move_sl = True
                elif trade_type == mt5.ORDER_TYPE_SELL and new_tsl_price < current_sl:
                    move_sl = True

            if move_sl:
                logger.info(f"{log_prefix_tsl} Price moved favorably. Updating TSL from {current_sl} to {new_tsl_price}...")
                modify_success = self.mt5_executor.modify_trade(ticket=ticket, sl=new_tsl_price)

                if modify_success:
                    logger.info(f"{log_prefix_tsl} Successfully updated TSL to: {new_tsl_price}")
                    # Send notifications (optional, could be noisy)
                    # status_msg_tsl_upd = f"➡️ <b>Trailing Stop Updated</b>\n<b>Ticket:</b> <code>{ticket}</code>\n<b>New SL:</b> <code>{new_tsl_price}</code>"
                    # await self.telegram_sender.send_message(status_msg_tsl_upd, parse_mode='html')
                    debug_channel_id = getattr(self.telegram_sender, 'debug_target_channel_id', None)
                    if debug_channel_id:
                         await self.telegram_sender.send_message(f"➡️ {log_prefix_tsl} Updated TSL to {new_tsl_price}", target_chat_id=debug_channel_id)
                else:
                    logger.error(f"{log_prefix_tsl} Failed to update TSL price to {new_tsl_price} via modify_trade.")
            # else: # New TSL is not better than current SL
            #     logger.debug(f"{log_prefix_tsl} New TSL ({new_tsl_price}) not better than current SL ({current_sl}). No update.")



    async def check_and_handle_tp_hits(self, position, trade_info: TradeInfo): # Type hint
        """
        Checks a specific active trade for TP hits and handles based on configured strategy.
        Called periodically by the main monitor task.

        Args:
            position (mt5.PositionInfo): The current position data from MT5.
            trade_info (TradeInfo): The internally tracked trade data object from StateManager.
        """
        # --- Read config dynamically ---
        partial_close_perc = self.config_service.getint('Strategy', 'partial_close_percentage', fallback=50) # Use service
        tp_strategy = self.config_service.get('Strategy', 'tp_execution_strategy', fallback='first_tp_full_close').lower() # Use service
        # --- End Read config ---

        # --- Skip check if strategy doesn't involve monitoring TPs after entry ---
        if tp_strategy == 'first_tp_full_close' or tp_strategy == 'last_tp_full_close':
            return

        # --- Only proceed for sequential_partial_close ---
        if tp_strategy != 'sequential_partial_close':
             # logger.warning(f"TP Check: Unsupported tp_execution_strategy '{tp_strategy}' encountered in check loop.")
             return

        if not position or not trade_info:
            logger.error("Cannot check TPs: Position or trade_info missing.")
            return

        ticket = position.ticket
        symbol = trade_info.symbol # Use attribute access
        all_tps = trade_info.all_tps # Use attribute access
        next_tp_index = trade_info.next_tp_index # Use attribute access
        original_volume = trade_info.original_volume # Use attribute access
        original_msg_id = trade_info.original_msg_id # Use attribute access

        log_prefix_tp = f"[TP Check][Ticket: {ticket}]"

        # Check if we have processed all TPs for this trade or if data is missing
        if next_tp_index >= len(all_tps) or original_volume is None:
            # logger.debug(f"{log_prefix_tp} All TPs processed or missing original volume.")
            return # Nothing more to check

        current_tp_target_str = all_tps[next_tp_index]
        if current_tp_target_str == "N/A":
            # logger.debug(f"{log_prefix_tp} Next TP target is N/A.")
            return # No more valid TPs

        # --- Check for Current TP Hit ---
        current_tp_target = None
        try:
            current_tp_target = float(current_tp_target_str)
        except (ValueError, TypeError):
            logger.warning(f"{log_prefix_tp} Invalid TP value '{current_tp_target_str}' at index {next_tp_index}. Skipping TP check for this level.")
            trade_info.next_tp_index += 1 # Use attribute access
            return

        # Get current market price
        tick = self.mt5_fetcher.get_symbol_tick(symbol)
        if not tick:
            logger.warning(f"{log_prefix_tp} Could not get current tick for {symbol}. Skipping TP check.")
            return

        # Use position data passed in
        pos_data = position
        trade_type = pos_data.type
        current_pos_volume = pos_data.volume
        entry_price = pos_data.price_open

        tp_hit = False
        if trade_type == mt5.ORDER_TYPE_BUY and tick.bid >= current_tp_target:
            tp_hit = True
            logger.info(f"{log_prefix_tp} TP{next_tp_index + 1} ({current_tp_target}) hit for BUY position (Bid: {tick.bid}).")
        elif trade_type == mt5.ORDER_TYPE_SELL and tick.ask <= current_tp_target:
            tp_hit = True
            logger.info(f"{log_prefix_tp} TP{next_tp_index + 1} ({current_tp_target}) hit for SELL position (Ask: {tick.ask}).")

        if tp_hit:
            is_last_tp = (next_tp_index == len(all_tps) - 1) or (len(all_tps) > next_tp_index + 1 and all_tps[next_tp_index + 1] == "N/A")

            volume_to_close = 0.0
            action_desc = ""

            if is_last_tp:
                volume_to_close = current_pos_volume # Close remaining volume
                action_desc = f"Close Full at Final TP{next_tp_index + 1}"
                logger.info(f"{log_prefix_tp} Final TP hit. Closing remaining {volume_to_close} lots...")
            else:
                # Calculate partial volume based on ORIGINAL volume
                volume_to_close = round(original_volume * (partial_close_perc / 100.0), 8)
                action_desc = f"Partial Close at TP{next_tp_index + 1}"
                logger.info(f"{log_prefix_tp} Initiating partial close ({partial_close_perc}% of original)...")

            # Execute close action (partial or full)
            close_success = self.mt5_executor.close_position(
                ticket=ticket,
                volume=volume_to_close,
                comment=f"{action_desc} (SigID {original_msg_id})"
            )

            if close_success:
                logger.info(f"{log_prefix_tp} {action_desc} successful for {volume_to_close} lots.")
                trade_info.next_tp_index += 1 # Use attribute access

                next_tp_value_for_modify = None
                next_tp_display = "<i>None</i>"

                # If not the last TP, modify remaining position to next TP
                if not is_last_tp:
                    next_tp_index_for_modify = trade_info.next_tp_index # Use attribute access
                    if next_tp_index_for_modify < len(all_tps) and all_tps[next_tp_index_for_modify] != "N/A":
                        try:
                            next_tp_value_for_modify = float(all_tps[next_tp_index_for_modify])
                            next_tp_display = f"<code>{next_tp_value_for_modify}</code>"
                            logger.info(f"{log_prefix_tp} Modifying remaining position TP to TP{next_tp_index_for_modify + 1}: {next_tp_value_for_modify}")
                        except (ValueError, TypeError):
                            logger.warning(f"{log_prefix_tp} Invalid next TP value '{all_tps[next_tp_index_for_modify]}'. Removing TP.")
                            next_tp_value_for_modify = 0.0 # Remove TP if next one is invalid
                    else:
                        logger.info(f"{log_prefix_tp} No further valid TPs defined. Removing TP from remaining position.")
                        next_tp_value_for_modify = 0.0 # Remove TP if no more valid TPs

                    # Modify the trade TP
                    modify_tp_success = self.mt5_executor.modify_trade(ticket=ticket, tp=next_tp_value_for_modify)
                    if not modify_tp_success:
                         logger.error(f"{log_prefix_tp} Failed to modify TP to {next_tp_value_for_modify} after partial close.")
                         # Continue anyway, close was successful

                # Send Notification
                symbol_digits = self.mt5_fetcher.get_symbol_info(symbol).digits if self.mt5_fetcher.get_symbol_info(symbol) else 5
                entry_price_str = f"@{entry_price:.{symbol_digits}f}"
                status_msg = f"💰 <b>{action_desc}</b> <code>[MsgID: {original_msg_id}]</code>\n" \
                             f"<b>Ticket:</b> <code>{ticket}</code> (Entry: {entry_price_str})\n" \
                             f"<b>Closed:</b> <code>{volume_to_close}</code> lots at ≈<code>{current_tp_target}</code>\n" \
                             f"<b>Next TP Target:</b> {next_tp_display}"
                await self.telegram_sender.send_message(status_msg, parse_mode='html')
                debug_channel_id = getattr(self.telegram_sender, 'debug_target_channel_id', None)
                if debug_channel_id:
                     await self.telegram_sender.send_message(f"💰 {log_prefix_tp} {action_desc} {volume_to_close} lots at TP {current_tp_target}. Next TP: {next_tp_display}", target_chat_id=debug_channel_id)

            else:
                logger.error(f"{log_prefix_tp} {action_desc} FAILED.")
                # Don't increment next_tp_index, retry might happen next cycle if TP still hit