import asyncio
import MetaTrader5 as mt5
from datetime import datetime, timezone
import logging

logger = logging.getLogger('TradeBot')

closed_trades_log = []

async def periodic_trade_closure_monitor_task(state_manager, telegram_sender, mt5_executor, interval_seconds=60):
    """
    Periodically checks for closed or canceled trades and sends status updates.
    """
    while True:
        try:
            await asyncio.sleep(interval_seconds)

            # Fetch current open positions and orders
            open_positions = mt5.positions_get()
            open_orders = mt5.orders_get()

            open_tickets = set()
            if open_positions:
                open_tickets.update(pos.ticket for pos in open_positions)
            if open_orders:
                open_tickets.update(ord.ticket for ord in open_orders)

            # Copy to avoid modification during iteration
            active_trades = list(state_manager.get_active_trades())

            for trade in active_trades:
                ticket = trade.ticket
                if ticket in open_tickets:
                    continue  # Still active

                # Trade is closed or canceled
                logger.info(f"[ClosureMonitor] Tracked ticket {ticket} no longer active. Fetching history...")
                profit = 0.0
                close_price = None
                close_time = None
                close_reason = "Unknown"
                is_canceled_pending = False

                # --- Check Order History First for Cancellation ---
                from datetime import datetime, timezone, timedelta # Ensure import
                from_time = datetime.now(timezone.utc) - timedelta(days=7) # Check last 7 days
                to_time = datetime.now(timezone.utc) + timedelta(days=1)
                orders = mt5.history_orders_get(ticket=ticket) # Fetch specific order by ticket

                if orders:
                    # Sort by time_done descending to get the latest state
                    orders_sorted = sorted(orders, key=lambda o: o.time_done, reverse=True)
                    last_order_state = orders_sorted[0]
                    if last_order_state.state == mt5.ORDER_STATE_CANCELED:
                        is_canceled_pending = True
                        close_time = datetime.fromtimestamp(last_order_state.time_done, tz=timezone.utc)
                        close_reason = "Canceled"
                        logger.info(f"[ClosureMonitor] Ticket {ticket} identified as CANCELED pending order.")
                        # Format Canceled message
                        msg = f"🚫 <b>Pending Order Canceled</b>\n"
                        msg += f"<b>Ticket:</b> <code>{ticket}</code>\n"
                        msg += f"<b>Symbol:</b> <code>{trade.symbol}</code>\n"
                        # Use trade_info.entry_price which holds the pending price for pending orders
                        pending_price_str = f"<code>{trade.entry_price}</code>" if trade.entry_price is not None else "<i>N/A</i>"
                        msg += f"<b>Pending Price:</b> {pending_price_str}\n"
                        msg += f"<b>Canceled At:</b> {close_time.strftime('%Y-%m-%d %H:%M:%S')}\n"

                        original_msg_id = getattr(trade, 'original_msg_id', None)
                        await telegram_sender.send_message(msg, parse_mode='html', reply_to=original_msg_id)
                        # Log for daily summary (optional, maybe filter out canceled?)
                        closed_trades_log.append({
                            'ticket': ticket, 'symbol': trade.symbol, 'profit': 0.0, # No profit for canceled
                            'close_time': close_time, 'reason': close_reason
                        })
                        # Remove from active trades
                        state_manager.bot_active_trades = [t for t in state_manager.bot_active_trades if t.ticket != ticket]
                        continue # Skip deal fetching for canceled orders
                else:
                     logger.warning(f"[ClosureMonitor] Could not find order history for inactive ticket {ticket}. Proceeding to check deals.")
                # --- End Cancellation Check ---


                # --- If not canceled, fetch deal history for closure details ---
                logger.debug(f"[ClosureMonitor] Ticket {ticket} not canceled or order history missing. Fetching deal history...")
                # Fetch deals for close price and time
                deals = mt5.history_deals_get(ticket=ticket) # Fetch specific deals by order ticket
                if deals:
                    closing_deal = max(deals, key=lambda d: d.time)
                    profit = closing_deal.profit
                    close_price = closing_deal.price
                    close_time = datetime.fromtimestamp(closing_deal.time, tz=timezone.utc)
    
                    # Determine reason based on the closing deal's reason code
                    reason_code = closing_deal.reason
                    if reason_code == mt5.DEAL_REASON_TP:
                        close_reason = "Take Profit"
                    elif reason_code == mt5.DEAL_REASON_SL:
                        if profit == 0:
                            close_reason = "Break Even"
                        elif profit > 0:
                            close_reason = "Trailing Stop Loss"
                        else:
                            close_reason = "Stop Loss"
                    elif reason_code == mt5.DEAL_REASON_SO:
                        close_reason = "Stop Out"
                    elif reason_code == mt5.DEAL_REASON_MOBILE:
                        close_reason = "Manual Close (Mobile)"
                    elif reason_code == mt5.DEAL_REASON_WEB:
                        close_reason = "Manual Close (Web)"
                    elif reason_code == mt5.DEAL_REASON_CLIENT:
                        close_reason = "Manual Close (Desktop)"
                    elif reason_code == mt5.DEAL_REASON_EXPERT:
                        close_reason = "Expert Advisor"
                    else:
                        close_reason = f"Other ({reason_code})"
    
                else: # No closing deal found
                    logger.warning(f"[ClosureMonitor] No closing deal found for ticket {ticket}. Reason remains '{close_reason}'.")
                    # Attempt to get close time from order history if not already set (e.g., from cancellation check)
                    if close_time is None and orders: # Use orders fetched earlier
                         orders_sorted = sorted(orders, key=lambda o: o.time_done, reverse=True)
                         last_order_state = orders_sorted[0]
                         close_time = datetime.fromtimestamp(last_order_state.time_done, tz=timezone.utc)
                         logger.info(f"[ClosureMonitor] Using order time_done {close_time} as close time for ticket {ticket}.")
                    elif close_time is None:
                         close_time = datetime.now(timezone.utc) # Fallback close time
                         logger.warning(f"[ClosureMonitor] Could not determine close time for ticket {ticket}. Using current time.")


                # --- Compose Closed Trade Message ---
                # Calculate pips
                digits = 2
                trade_type = getattr(trade, 'trade_type', None)
                try:
                    symbol_info = mt5.symbol_info(trade.symbol)
                    if symbol_info:
                        digits = symbol_info.digits
                except:
                    pass
                pips = 0.0
                if close_price and trade.entry_price:
                    pips = (close_price - trade.entry_price) * (10 ** digits)
                    if trade_type == mt5.ORDER_TYPE_SELL:
                        pips = -pips

                # Use HTML escaping for safety, especially for reason
                import html
                safe_reason = html.escape(str(close_reason))
                entry_price_display = f"<code>{trade.entry_price}</code>" if trade.entry_price is not None else "<i>N/A</i>"
                close_price_display = f"<code>{close_price}</code>" if close_price is not None else "<i>N/A</i>"
                close_time_display = close_time.strftime('%Y-%m-%d %H:%M:%S') if close_time else "<i>N/A</i>"

                msg = f"📉 <b>Trade Closed</b>\n"
                msg += f"<b>Ticket:</b> <code>{ticket}</code>\n"
                msg += f"<b>Symbol:</b> <code>{trade.symbol}</code>\n"
                msg += f"<b>Entry Price:</b> {entry_price_display}\n"
                msg += f"<b>Close Price:</b> {close_price_display}\n"
                msg += f"<b>Profit:</b> <code>{profit:.2f}</code>\n"
                msg += f"<b>Pips:</b> <code>{pips:.1f}</code>\n"
                msg += f"<b>Reason:</b> {safe_reason}\n"
                msg += f"<b>Closed At:</b> {close_time_display}\n"

                original_msg_id = getattr(trade, 'original_msg_id', None)
                await telegram_sender.send_message(msg, parse_mode='html', reply_to=original_msg_id)

                # Log for daily summary
                closed_trades_log.append({
                    'ticket': ticket,
                    'symbol': trade.symbol,
                    'profit': profit,
                    'close_time': close_time, # Log the datetime object
                    'reason': close_reason # Log the original reason string
                })
                # --- End Compose Closed Trade Message ---

                # --- Cancel remaining distributed orders if TP hit ---
                # --- Cancel remaining distributed orders if TP hit ---
                if close_reason == "Take Profit" and trade.sequence_info and trade.sequence_info.startswith("Dist"):
                    logger.info(f"TP hit for distributed trade {ticket}. Canceling remaining pending orders for OrigMsgID {original_msg_id}...")
                    canceled_count = 0
                    # Iterate over a copy for safe removal
                    for other_trade in list(state_manager.get_active_trades()):
                        if other_trade.original_msg_id == original_msg_id and other_trade.is_pending and other_trade.ticket != ticket:
                            logger.info(f"Attempting to cancel pending order {other_trade.ticket} from distributed set...")
                            cancel_success = mt5_executor.delete_pending_order(other_trade.ticket)
                            if cancel_success:
                                canceled_count += 1
                                # Remove from state manager immediately after successful cancellation
                                state_manager.bot_active_trades = [t for t in state_manager.bot_active_trades if t.ticket != other_trade.ticket]
                                logger.info(f"Successfully canceled pending order {other_trade.ticket} and removed from state.")
                            else:
                                logger.error(f"Failed to cancel pending order {other_trade.ticket}.")
                    if canceled_count > 0:
                        cancel_msg = f"ℹ️ Canceled {canceled_count} remaining pending order(s) for OrigMsgID {original_msg_id} due to TP hit on ticket {ticket}."
                        await telegram_sender.send_message(cancel_msg, parse_mode='html')
                # --- End Cancel Logic ---

                # Remove the closed trade from active trades
                state_manager.bot_active_trades = [t for t in state_manager.bot_active_trades if t.ticket != ticket]

        except asyncio.CancelledError:
            logger.info("Trade closure monitor task cancelled.")
            break
        except Exception as e:
            logger.error(f"Error in trade closure monitor task: {e}", exc_info=True)
            await asyncio.sleep(interval_seconds * 2)