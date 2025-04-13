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
        auto_sl_distance = self.config_service.getfloat('AutoSL', 'auto_sl_risk_pips', fallback=40.0) # Use service, now in pips
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
                # Calculate SL price using the fixed pip distance from config
                # auto_sl_distance variable holds the pips value read from config
                # No conversion needed here, pass pips directly to the correct calculator method
                # sl_price_distance = auto_sl_distance * 0.1 # REMOVED - Incorrect conversion
                auto_sl_price = self.trade_calculator.calculate_sl_from_pips( # Use correct method name
                    symbol=symbol,
                    order_type=trade_type,
                    entry_price=entry_price,
                    sl_distance_pips=auto_sl_distance # Pass the pips value directly
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
                        status_msg_auto_sl = f"🤖 <b>Auto StopLoss Applied</b>\n<b>Ticket:</b> <code>{ticket}</code> (Entry: {entry_price_str})\n<b>New SL:</b> <code>{auto_sl_price}</code> (Distance: {auto_sl_distance} pips)"
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

        profit_pips_threshold_config = self.config_service.getfloat('AutoBE', 'auto_be_profit_pips', fallback=30.0) # Use service, read pips

        if profit_pips_threshold_config <= 0:
            logger.warning("AutoBE profit threshold (auto_be_profit_pips) is zero or negative, disabling check.")
            return
        # base_lot_size no longer needed for pip-based activation
        # --- End Read AutoBE config ---

        # current_profit = position.profit # No longer needed for activation check
        current_sl = position.sl
        entry_price = position.price_open
        trade_type = position.type
        symbol = position.symbol # Need symbol for calculations

        # Get symbol info for pip calculations
        logger.debug(f"[AutoBE Test Debug] entry_price={entry_price}, trade_type={trade_type}, symbol={symbol}")
        symbol_info = self.mt5_fetcher.get_symbol_info(symbol)
        if not symbol_info:
            logger.error(f"{log_prefix_auto_be} Cannot get symbol info for {symbol}. Skipping AutoBE check.")
            return
        point = symbol_info.point
        digits = symbol_info.digits
        logger.debug(f"[AutoBE Test Debug] symbol_info.point={point}, symbol_info.digits={digits}")

        # Calculate required profit distance in price units
        required_price_distance = round(profit_pips_threshold_config * (point * 10), digits) # Assuming 1 pip = 10 points

        # Get current market price to calculate current profit distance
        logger.debug(f"[AutoBE Test Debug] profit_pips_threshold_config={profit_pips_threshold_config}, required_price_distance={required_price_distance}")
        tick = self.mt5_fetcher.get_symbol_tick(symbol)
        if not tick:
            logger.warning(f"{log_prefix_auto_be} Could not get current tick for {symbol}. Skipping AutoBE check.")
            return
        relevant_market_price = tick.bid if trade_type == mt5.ORDER_TYPE_BUY else tick.ask

        # Calculate current profit distance in price units
        current_price_distance_profit = 0.0
        if trade_type == mt5.ORDER_TYPE_BUY:
            current_price_distance_profit = relevant_market_price - entry_price # Bid - Entry
        elif trade_type == mt5.ORDER_TYPE_SELL:
            current_price_distance_profit = entry_price - relevant_market_price # Entry - Ask

        logger.debug(f"{log_prefix_auto_be} ConfigPips={profit_pips_threshold_config}, RequiredPriceDist={required_price_distance:.{digits}f}, CurrentPriceDist={current_price_distance_profit:.{digits}f}")
        logger.debug(f"[AutoBE Test Debug] current_price_distance_profit={current_price_distance_profit}, required_price_distance={required_price_distance}")
        logger.debug(f"[AutoBE Test Debug] Should trigger BE? {current_price_distance_profit >= required_price_distance}")

        # Check if profit distance threshold is met
        if current_price_distance_profit < required_price_distance:
            # logger.debug(f"{log_prefix_auto_be} Current distance {current_price_distance_profit:.{digits}f} < Required distance {required_price_distance:.{digits}f}. No action.")
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
        logger.info(f"{log_prefix_auto_be} Profit Distance {current_price_distance_profit:.{digits}f} >= Required Distance {required_price_distance:.{digits}f}. Attempting to move SL to Breakeven ({entry_price}).")

        # Calculate BE SL directly, accounting for spread and offset
        symbol_info = self.mt5_fetcher.get_symbol_info(position.symbol)
        point = symbol_info.point if symbol_info else 0.00001 # Default point size if info fails
        digits = symbol_info.digits if symbol_info else 5 # Default digits if info fails
        sl_offset_pips = self.config_service.getfloat('Trading', 'sl_offset_pips', fallback=0.0)
        offset_price = round(sl_offset_pips * (point * 10), digits) # Assuming 1 pip = 10 points for offset config

        tick = self.mt5_fetcher.get_symbol_tick(position.symbol)
        spread = 0.0
        if tick and tick.ask > 0 and tick.bid > 0: # Ensure valid tick data
            spread = round(tick.ask - tick.bid, digits)
        else:
            logger.warning(f"{log_prefix_auto_be} Could not get valid tick for spread calculation. Using offset only for BE SL.")

        # Use the adjusted entry price stored in trade_info as the base for BE
        base_entry_for_be = trade_info.entry_price
        if base_entry_for_be is None:
             logger.error(f"{log_prefix_auto_be} Cannot calculate BE SL: Adjusted entry price not found in trade_info.")
             return # Cannot proceed without the adjusted entry

        be_sl = None
        if trade_type == mt5.ORDER_TYPE_BUY:
            # For BUY, BE SL should be slightly ABOVE adjusted entry
            be_sl = round(base_entry_for_be + spread + offset_price, digits)
            logger.debug(f"{log_prefix_auto_be} Calculating BUY BE SL: AdjEntry={base_entry_for_be}, Spread={spread}, Offset={offset_price} -> BE_SL={be_sl}")
        elif trade_type == mt5.ORDER_TYPE_SELL:
            # For SELL, BE SL should be slightly BELOW adjusted entry
            be_sl = round(base_entry_for_be - spread - offset_price, digits)
            logger.debug(f"{log_prefix_auto_be} Calculating SELL BE SL: AdjEntry={base_entry_for_be}, Spread={spread}, Offset={offset_price} -> BE_SL={be_sl}")
        else:
            logger.error(f"{log_prefix_auto_be} Unknown trade type {trade_type}. Cannot calculate BE SL.")
            return # Abort if type is unknown

        # Sanity check: Ensure BE SL is actually better than entry after adjustments
        # Sanity check: Ensure BE SL is actually better than the adjusted entry after adjustments
        if (trade_type == mt5.ORDER_TYPE_BUY and be_sl <= base_entry_for_be) or \
           (trade_type == mt5.ORDER_TYPE_SELL and be_sl >= base_entry_for_be):
            logger.warning(f"{log_prefix_auto_be} Calculated BE SL ({be_sl}) is not better than adjusted entry ({base_entry_for_be}) after spread/offset. Setting SL exactly to adjusted entry price as fallback.")
            be_sl = base_entry_for_be # Fallback to exact adjusted entry if adjustment goes wrong way

        if be_sl is None: # Should not happen if type check passed, but safety check
             logger.error(f"{log_prefix_auto_be} Failed to determine valid BE SL price. Aborting AutoBE.")
             return

        logger.debug(f"{log_prefix_auto_be} Conditions met. Calling modify_trade with ticket={ticket}, sl={be_sl}") # Add log before call
        modify_success = self.mt5_executor.modify_trade(ticket=ticket, sl=be_sl)

        if modify_success:
            logger.info(f"{log_prefix_auto_be} Successfully moved SL to Breakeven: {entry_price}")
            trade_info.auto_be_applied = True
            # Send notifications
            entry_price_str = f"@{entry_price:.{digits}f}" # Use digits for formatting
            status_msg_auto_be = f"🛡️ <b>Auto Breakeven Applied</b>\n<b>Ticket:</b> <code>{ticket}</code> (Entry: {entry_price_str})\n<b>New SL:</b> <code>{be_sl}</code> (Profit Trigger: ≥ {profit_pips_threshold_config} pips)"
            await self.telegram_sender.send_message(status_msg_auto_be, parse_mode='html')
            debug_channel_id = getattr(self.telegram_sender, 'debug_target_channel_id', None)
            if debug_channel_id:
                 await self.telegram_sender.send_message(f"🛡️ {log_prefix_auto_be} Applied AutoBE: SL moved to {be_sl}", target_chat_id=debug_channel_id)
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

        activation_profit_pips_config = self.config_service.getfloat('TrailingStop', 'activation_profit_pips', fallback=60.0) # Use service (e.g., 60 pips)
        trail_distance_pips_config = self.config_service.getfloat('TrailingStop', 'trail_distance_pips', fallback=20.0) # Use service (e.g., 20 pips)
        # base_lot_size is no longer needed for TSL activation based on pips

        if activation_profit_pips_config <= 0 or trail_distance_pips_config <= 0:
            logger.warning("TrailingStop activation_profit_pips or trail_distance_pips is zero or negative. TSL disabled.")
            return
        # Removed base_lot_size check as it's not used for pip-based activation
        # Removed check comparing trail distance and activation profit as they are now in different units (pips vs pips)
        # A check like trail_distance_pips >= activation_profit_pips could be added if desired.
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

        # Get symbol info for pip calculations
        symbol_info = self.mt5_fetcher.get_symbol_info(symbol)
        if not symbol_info:
            logger.error(f"{log_prefix_tsl} Cannot get symbol info for {symbol}. Skipping TSL check.")
            return
        point = symbol_info.point
        digits = symbol_info.digits

        # Calculate activation distance in price units
        activation_price_distance = self.trade_calculator.pips_to_price_distance(symbol, activation_profit_pips_config)
        if activation_price_distance is None:
            logger.error(f"{log_prefix_tsl} Failed to calculate activation price distance from pips. Skipping TSL check.")
            return

        logger.debug(f"{log_prefix_tsl} ConfigActivationPips={activation_profit_pips_config}, ActivationPriceDistance={activation_price_distance}")
        logger.debug(f"{log_prefix_tsl} ConfigTrailDistancePips={trail_distance_pips_config}")


        # --- TSL Activation Logic ---
        if not tsl_active:
            # Calculate current profit distance in price units
            # Use adjusted entry price from trade_info for profit calculation
            base_entry_for_tsl = trade_info.entry_price
            if base_entry_for_tsl is None:
                 logger.error(f"{log_prefix_tsl} Cannot calculate profit distance: Adjusted entry price not found in trade_info.")
                 return # Cannot proceed

            current_price_distance_profit = 0.0
            if trade_type == mt5.ORDER_TYPE_BUY:
                current_price_distance_profit = relevant_market_price - base_entry_for_tsl # Bid - AdjustedEntry
            elif trade_type == mt5.ORDER_TYPE_SELL:
                current_price_distance_profit = base_entry_for_tsl - relevant_market_price # AdjustedEntry - Ask

            # Compare current price distance profit with activation price distance threshold
            if current_price_distance_profit >= activation_price_distance:
                logger.info(f"{log_prefix_tsl} Price Distance Profit {current_price_distance_profit:.{digits}f} >= Activation Distance {activation_price_distance:.{digits}f}. Attempting TSL activation...")

                # Calculate initial TSL price based on current price and FIXED trail price distance
                # Calculate initial TSL price based on current price and CONFIGURED trail pips distance
                new_tsl_price = self.trade_calculator.calculate_trailing_sl_price(
                    symbol=symbol,
                    order_type=trade_type,
                    current_price=relevant_market_price, # Use Bid for BUY, Ask for SELL
                    trail_distance_pips=trail_distance_pips_config # Pass pips distance
                )

                if new_tsl_price is None:
                    logger.error(f"{log_prefix_tsl} Failed to calculate initial TSL price.")
                    return # Cannot proceed without calculated price

                # --- Sanity Check: Ensure initial TSL locks in *some* profit ---
                # (i.e., SL is better than entry price)
                initial_tsl_locks_profit = False
                # Calculate the actual breakeven point (adjusted entry +/- spread +/- sl_offset) to compare against
                # Use the adjusted entry price from trade_info as the base
                adjusted_entry_sl = self.mt5_executor._adjust_sl_for_spread_offset(base_entry_for_tsl, trade_type, symbol)

                if trade_type == mt5.ORDER_TYPE_BUY and new_tsl_price > adjusted_entry_sl:
                    initial_tsl_locks_profit = True
                elif trade_type == mt5.ORDER_TYPE_SELL and new_tsl_price < adjusted_entry_sl:
                    initial_tsl_locks_profit = True

                if not initial_tsl_locks_profit:
                     logger.warning(f"{log_prefix_tsl} Calculated initial TSL price ({new_tsl_price}) does not lock profit relative to adjusted entry BE point ({adjusted_entry_sl}). Activation condition might be too tight or market moved unfavorably. Will retry next cycle.")
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
                    entry_price_str = f"@{entry_price:.{digits}f}"
                    status_msg_tsl_act = f"📈 <b>Trailing Stop Activated</b>\n<b>Ticket:</b> <code>{ticket}</code> (Entry: {entry_price_str})\n<b>Initial SL:</b> <code>{new_tsl_price}</code> (Profit ≥ {activation_profit_pips_config} pips, Trail: {trail_distance_pips_config} pips)"
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
            # Calculate the new potential TSL price based on the current market price and CONFIGURED trail pips distance
            new_tsl_price = self.trade_calculator.calculate_trailing_sl_price(
                symbol=symbol,
                order_type=trade_type,
                current_price=relevant_market_price, # Use Bid for BUY, Ask for SELL
                trail_distance_pips=trail_distance_pips_config # Pass pips distance
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
                # Calculate the actual breakeven point (adjusted entry +/- spread +/- sl_offset)
                # Use the adjusted entry price from trade_info as the base
                base_entry_for_tsl_update = trade_info.entry_price # Re-fetch adjusted entry
                if base_entry_for_tsl_update is None:
                     logger.error(f"{log_prefix_tsl} Cannot check profit lock: Adjusted entry price not found in trade_info.")
                     # Decide how to handle: maybe skip applying SL? For now, log and continue comparison with current_sl
                else:
                     adjusted_entry_sl = self.mt5_executor._adjust_sl_for_spread_offset(base_entry_for_tsl_update, trade_type, symbol)

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



    # Removed obsolete check_and_handle_tp_hits function.
    # TP hits should be handled by monitoring the assigned_tp for each individual trade/position,
    # or potentially by a separate, more advanced monitoring component if complex multi-TP
    # strategies beyond simple assignment are needed in the future.