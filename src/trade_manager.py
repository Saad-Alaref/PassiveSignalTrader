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
        enable_auto_sl = self.config_service.getboolean('AutoSL', 'enable_auto_sl', fallback=False)
        if not enable_auto_sl:
            logger.info("[AutoSL] Feature disabled.")
            return
        if not position or not trade_info:
            logger.error("[AutoSL] Missing position or trade_info.")
            return
        ticket = position.ticket
        log_prefix_auto_sl = f"[AutoSL][Ticket: {ticket}]"
        current_sl = getattr(position, 'sl', None)
        if current_sl not in [None, 0.0]:
            logger.info(f"{log_prefix_auto_sl} SL already set (current SL: {current_sl}). No action.")
            return  # Already has SL
        # Calculate SL using trade_calculator (assuming such method exists)
        try:
            sl_distance = self.config_service.getfloat('AutoSL', 'auto_sl_pips', fallback=40.0)
            symbol = position.symbol
            order_type = position.type
            entry_price = getattr(position, 'price_open', None)
            if not entry_price:
                logger.error(f"{log_prefix_auto_sl} Entry price missing, cannot calculate SL.")
                return
            sl_price = self.trade_calculator.calculate_sl_price(symbol, order_type, entry_price, sl_distance)
            if sl_price is None:
                logger.error(f"{log_prefix_auto_sl} Failed to calculate SL price.")
                return
            modify_success = self.mt5_executor.modify_trade(ticket=ticket, sl=sl_price)
            if modify_success:
                logger.info(f"{log_prefix_auto_sl} Successfully applied AutoSL: {sl_price}")
                if self.telegram_sender:
                    await self.telegram_sender.send_message(f"🛡️ {log_prefix_auto_sl} SL set to {sl_price}")
            else:
                logger.error(f"{log_prefix_auto_sl} Failed to set SL via modify_trade.")
        except Exception as e:
            logger.error(f"{log_prefix_auto_sl} Exception during AutoSL application: {e}")

    async def check_and_apply_auto_be(self, position, trade_info: TradeInfo): # Type hint
        """
        Checks if a trade's profit meets the threshold and moves SL to breakeven.
        Called periodically by the main monitor task.

        Args:
            position (mt5.PositionInfo): The current position data from MT5.
            trade_info (TradeInfo): The internally tracked trade data object from StateManager.
        """
        enable_auto_be = self.config_service.getboolean('AutoBE', 'enable_auto_be', fallback=False)
        if not enable_auto_be:
            logger.info("[AutoBE] Feature disabled.")
            return
        if not position or not trade_info or not hasattr(position, 'profit'):
            logger.error("[AutoBE] Missing position, trade_info, or profit attribute.")
            return
        ticket = position.ticket
        log_prefix_auto_be = f"[AutoBE][Ticket: {ticket}]"
        current_sl = getattr(position, 'sl', None)
        entry_price = getattr(position, 'price_open', None)
        profit = getattr(position, 'profit', None)
        if profit is None:
            logger.error(f"{log_prefix_auto_be} No profit attribute on position. Skipping BE check.")
            return
        # Add your BE logic here (example: set SL to entry if profit > threshold)
        try:
            be_profit_threshold = self.config_service.getfloat('AutoBE', 'be_profit_threshold', fallback=5.0)
            if profit >= be_profit_threshold:
                if current_sl != entry_price:
                    modify_success = self.mt5_executor.modify_trade(ticket=ticket, sl=entry_price)
                    if modify_success:
                        logger.info(f"{log_prefix_auto_be} Successfully moved SL to BE: {entry_price}")
                        if self.telegram_sender:
                            await self.telegram_sender.send_message(f"🟩 {log_prefix_auto_be} SL moved to BE: {entry_price}")
                    else:
                        logger.error(f"{log_prefix_auto_be} Failed to move SL to BE.")
                else:
                    logger.info(f"{log_prefix_auto_be} SL already at BE. No action.")
            else:
                logger.info(f"{log_prefix_auto_be} Profit {profit} below threshold {be_profit_threshold}. No BE action.")
        except Exception as e:
            logger.error(f"{log_prefix_auto_be} Exception during AutoBE application: {e}")

    # Add a calculate_sl_price helper to TradeCalculator if missing
    # NOTE: Implement calculate_sl_price in TradeCalculator if missing

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
                logger.info(f"{log_prefix_tsl} New TSL ({new_tsl_price}) is better than SL at start of check ({current_sl}). Updating...")
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

    async def check_and_apply_auto_tp(self, position, trade_info):
        """
        Checks if AutoTP should be applied to a trade and applies TP if conditions are met.
        Args:
            position (mt5.PositionInfo): The current position data from MT5.
            trade_info (TradeInfo): The internally tracked trade data object from StateManager.
        """
        enable_auto_tp = self.config_service.getboolean('AutoTP', 'enable_auto_tp', fallback=False)
        if not enable_auto_tp:
            return
        if not position or not trade_info:
            return
        ticket = position.ticket
        log_prefix_auto_tp = f"[AutoTP][Ticket: {ticket}]"
        current_tp = getattr(position, 'tp', None)
        if current_tp not in [None, 0.0]:
            return  # Already has TP
        # Calculate TP using trade_calculator (assuming such method exists)
        try:
            tp_distance = self.config_service.getfloat('AutoTP', 'auto_tp_pips', fallback=100.0)
            symbol = position.symbol
            order_type = position.type
            entry_price = getattr(position, 'price_open', None)
            if not entry_price:
                logger.error(f"{log_prefix_auto_tp} Entry price missing, cannot calculate TP.")
                return
            tp_price = self.trade_calculator.calculate_tp_price(symbol, order_type, entry_price, tp_distance)
            if tp_price is None:
                logger.error(f"{log_prefix_auto_tp} Failed to calculate TP price.")
                return
            modify_success = self.mt5_executor.modify_trade(ticket=ticket, tp=tp_price)
            if modify_success:
                logger.info(f"{log_prefix_auto_tp} Successfully applied AutoTP: {tp_price}")
                # Optionally notify via Telegram
                if self.telegram_sender:
                    await self.telegram_sender.send_message(f"🎯 {log_prefix_auto_tp} TP set to {tp_price}")
            else:
                logger.error(f"{log_prefix_auto_tp} Failed to set TP via modify_trade.")
        except Exception as e:
            logger.error(f"{log_prefix_auto_tp} Exception during AutoTP application: {e}")