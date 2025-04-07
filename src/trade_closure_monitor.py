import asyncio
import MetaTrader5 as mt5
from datetime import datetime, timezone
import logging

logger = logging.getLogger('TradeBot')

closed_trades_log = []

async def periodic_trade_closure_monitor_task(state_manager, telegram_sender, interval_seconds=60):
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
                profit = 0.0
                close_price = None
                close_time = None
                close_reason = "Unknown"

                # Fetch deal history for this ticket
                from datetime import datetime, timezone, timedelta
                from_time = datetime.now(timezone.utc) - timedelta(days=7)
                to_time = datetime.now(timezone.utc) + timedelta(days=1)
                # Fetch orders by position ID (ticket)
                orders = mt5.history_orders_get(from_time, to_time, position=ticket)
                profit = 0.0
                if orders:
                    last_order = max(orders, key=lambda o: o.time_done)
                    profit = getattr(last_order, 'profit', 0.0)
                # Also fetch deals for close price and time
                deals = mt5.history_deals_get(from_time, to_time, ticket=ticket)
                profit = 0.0
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
    
                else:
                    # No deal found, check order history
                    orders = mt5.history_orders_get(from_time, to_time, ticket=ticket)
                    if orders:
                        last_order = max(orders, key=lambda o: o.time_done)
                        close_time = datetime.fromtimestamp(last_order.time_done, tz=timezone.utc)
                        profit = getattr(last_order, 'profit', 0.0)
                        if last_order.state == mt5.ORDER_STATE_CANCELED:
                            close_reason = "Canceled"
                        else:
                            close_reason = "Closed"
                    else:
                        close_reason = "Unknown"

                # Compose message
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
    
                msg = f"📉 <b>Trade Closed</b>\n"
                msg += f"<b>Ticket:</b> <code>{ticket}</code>\n"
                msg += f"<b>Symbol:</b> <code>{trade.symbol}</code>\n"
                msg += f"<b>Entry Price:</b> <code>{trade.entry_price}</code>\n"
                msg += f"<b>Close Price:</b> <code>{close_price}</code>\n"
                msg += f"<b>Profit:</b> <code>{profit:.2f}</code>\n"
                msg += f"<b>Pips:</b> <code>{pips:.1f}</code>\n"
                msg += f"<b>Reason:</b> {close_reason}\n"
                msg += f"<b>Closed At:</b> {close_time.strftime('%Y-%m-%d %H:%M:%S')}\n"

                original_msg_id = getattr(trade, 'original_msg_id', None)
                await telegram_sender.send_message(msg, parse_mode='html', reply_to=original_msg_id)

                # Log for daily summary
                closed_trades_log.append({
                    'ticket': ticket,
                    'symbol': trade.symbol,
                    'profit': profit,
                    'close_time': close_time,
                    'reason': close_reason
                })
    
                # Remove from active trades
                state_manager.bot_active_trades = [t for t in state_manager.bot_active_trades if t.ticket != ticket]

        except asyncio.CancelledError:
            logger.info("Trade closure monitor task cancelled.")
            break
        except Exception as e:
            logger.error(f"Error in trade closure monitor task: {e}", exc_info=True)
            await asyncio.sleep(interval_seconds * 2)