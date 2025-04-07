import asyncio
from datetime import datetime, timedelta, timezone
import logging

logger = logging.getLogger('TradeBot')

async def daily_summary_task(state_manager, telegram_sender, summary_hour=23, summary_minute=59):
    """
    Sends a daily summary of closed trades at a fixed time, skipping weekends.
    """
    closed_trades_log = []

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

        # Compose summary
        total_profit = 0.0
        total_trades = len(closed_trades_log)
        wins = 0
        losses = 0
        canceled = 0

        for trade in closed_trades_log:
            profit = trade.get('profit', 0.0)
            total_profit += profit
            reason = trade.get('reason', '')
            if reason == 'Canceled':
                canceled += 1
            elif profit > 0:
                wins += 1
            else:
                losses += 1

        msg = f"📊 <b>Daily Trade Summary</b>\n"
        msg += f"<b>Date:</b> {next_run.date()}\n"
        msg += f"<b>Total Trades Closed:</b> {total_trades}\n"
        msg += f"<b>Wins:</b> {wins}\n"
        msg += f"<b>Losses:</b> {losses}\n"
        msg += f"<b>Canceled:</b> {canceled}\n"
        msg += f"<b>Total Profit:</b> <code>{total_profit:.2f}</code>\n"

        await telegram_sender.send_message(msg, parse_mode='html')

        # Reset log for next day
        closed_trades_log.clear()