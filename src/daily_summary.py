import asyncio
import MetaTrader5 as mt5
from datetime import datetime, timedelta, timezone
import logging

logger = logging.getLogger('TradeBot')

async def daily_summary_task(state_manager, telegram_sender, summary_hour=23, summary_minute=59):
    """
    Sends a daily summary of closed trades at a fixed time, skipping weekends.
    """

    while True:
        now = datetime.now(timezone.utc)
        # Calculate next summary time
        next_run = now.replace(hour=summary_hour, minute=summary_minute, second=0, microsecond=0)
        if next_run <= now:
            next_run += timedelta(days=1)

        # Sleep until next run
        sleep_seconds = (next_run - now).total_seconds()
        logger.info(f"Daily summary task sleeping for {sleep_seconds/60:.1f} minutes until {next_run}")
        await asyncio.sleep(sleep_seconds)

        # Skip weekends
        weekday = next_run.weekday()  # Monday=0, Sunday=6
        if weekday >= 5:
            logger.info("Weekend detected, skipping daily summary.")
            continue

        # Ensure MT5 connection is initialized
        if not mt5.initialize():
            logger.error(f"MT5 initialize() failed, error code = {mt5.last_error()}")
            continue

        # Fetch today's deals from MetaTrader5
        date_from = datetime.combine(next_run.date(), datetime.min.time()).replace(tzinfo=timezone.utc)
        date_to = next_run

        deals = mt5.history_deals_get(date_from, date_to)

        if deals is None or len(deals) == 0:
            msg = f"📊 <b>Daily Trade Summary</b>\n"
            msg += f"<b>Date:</b> {next_run.date()}\n"
            msg += "No closed trades found for today."
            await telegram_sender.send_message(msg, parse_mode='html')
            continue

        total_profit = 0.0
        wins = 0
        losses = 0
        symbols_stats = {}
        largest_win = float('-inf')
        largest_loss = float('inf')

        for deal in deals:
            profit = deal.profit
            symbol = deal.symbol
            total_profit += profit

            if profit > 0:
                wins += 1
                if profit > largest_win:
                    largest_win = profit
            else:
                losses += 1
                if profit < largest_loss:
                    largest_loss = profit

            if symbol not in symbols_stats:
                symbols_stats[symbol] = {'profit': 0.0, 'count': 0}
            symbols_stats[symbol]['profit'] += profit
            symbols_stats[symbol]['count'] += 1

        msg = f"📊 <b>Daily Trade Summary</b>\n"
        msg += f"<b>Date:</b> {next_run.date()}\n"
        msg += f"<b>Total Trades Closed:</b> {len(deals)}\n"
        msg += f"<b>Wins:</b> {wins}\n"
        msg += f"<b>Losses:</b> {losses}\n"
        msg += f"<b>Total Profit:</b> <code>{total_profit:.2f}</code>\n"

        if largest_win != float('-inf'):
            msg += f"<b>Largest Win:</b> <code>{largest_win:.2f}</code>\n"
        if largest_loss != float('inf'):
            msg += f"<b>Largest Loss:</b> <code>{largest_loss:.2f}</code>\n"

        msg += "\n<b>Performance by Symbol:</b>\n"
        for symbol, stats in symbols_stats.items():
            msg += f"{symbol}: {stats['count']} trades, PnL: <code>{stats['profit']:.2f}</code>\n"

        await telegram_sender.send_message(msg, parse_mode='html')