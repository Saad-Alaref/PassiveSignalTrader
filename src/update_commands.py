import logging
from abc import ABC, abstractmethod

from typing import Optional, Type

# Import necessary components
from .state_manager import StateManager
from .mt5_executor import MT5Executor
from .telegram_sender import TelegramSender
from .config_service import config_service
from .models import TradeInfo, UpdateData # Import relevant models
import MetaTrader5 as mt5

logger = logging.getLogger('TradeBot')

# --- Base Command Class ---
class UpdateCommand(ABC):
    """Abstract base class for handling trade update commands."""
    def __init__(self, update_data: UpdateData, target_trade_info: TradeInfo, mt5_executor: MT5Executor, # Use type hints
                 state_manager: StateManager, telegram_sender: TelegramSender,
                 config_service_instance, message_id, log_prefix):
        self.update_data = update_data
        # Store the initially identified trade for context (e.g., entry price for BE)
        self.context_trade_info = target_trade_info
        self.mt5_executor = mt5_executor
        self.state_manager = state_manager
        self.telegram_sender = telegram_sender
        self.config_service = config_service_instance # Store service instance
        self.message_id = message_id # ID of the update message itself
        self.log_prefix = log_prefix
        self.debug_channel_id = getattr(telegram_sender, 'debug_target_channel_id', None)
        # Store the ID of the *original* signal message this update relates to
        self.original_msg_id = target_trade_info.original_msg_id

    @abstractmethod
    async def execute(self):
        """Executes the specific update command."""
        pass

    async def _send_status_message(self, action_description, success_count, failure_count, total_trades, details=""):
        """Helper to send standardized status messages for multi-trade updates."""
        safe_action_desc = str(action_description).replace('&', '&amp;').replace('<', '<').replace('>', '>')
        status_icon = "✅" if success_count > 0 and failure_count == 0 else ("⚠️" if success_count > 0 and failure_count > 0 else "❌")
        status_text = "Successful" if success_count > 0 and failure_count == 0 else ("Partially Successful" if success_count > 0 and failure_count > 0 else "Failed")

        status_message_mod = f"""{status_icon} <b>{safe_action_desc} {status_text}</b> <code>[OrigMsgID: {self.original_msg_id}]</code>
<b>Update MsgID:</b> <code>{self.message_id}</code>
<b>Affected Trades:</b> {success_count}/{total_trades} OK{details}"""
        if failure_count > 0:
             status_message_mod += f"\n({failure_count} failed - check logs)"

        log_level = logging.INFO if failure_count == 0 else (logging.WARNING if success_count > 0 else logging.ERROR)
        logger.log(log_level, f"{self.log_prefix} {action_description} result for OrigMsgID {self.original_msg_id}: {success_count}/{total_trades} OK, {failure_count} Failed.")

        # Send to main channel (replying to original signal message if possible?) - Difficult to get original signal message object here easily.
        # For now, send as a new message.
        await self.telegram_sender.send_message(status_message_mod, parse_mode='html') #, reply_to=self.original_msg_id) # Replying might not work if original msg deleted

        if self.debug_channel_id:
            debug_msg_update_result = f"🔄 {self.log_prefix} Update Action Result (OrigMsgID {self.original_msg_id}):\n{status_message_mod}"
            await self.telegram_sender.send_message(debug_msg_update_result, target_chat_id=self.debug_channel_id, parse_mode='html')


# --- Concrete Command Classes ---

class ModifySLTPCommand(UpdateCommand):
    """Handles modify_sltp and move_sl updates."""
    async def execute(self):
        action_description = "Modify SL/TP"
        new_sl = None
        new_tp = None
        new_sl_val = self.update_data.new_stop_loss # Use attribute access
        new_tp_list = self.update_data.new_take_profits # Use attribute access
        success_count = 0
        failure_count = 0
        details = ""
        related_trades = []

        try:
            if new_sl_val != "N/A": new_sl = float(new_sl_val)
            # Use first TP from list for modification
            if new_tp_list and new_tp_list[0] != "N/A":
                new_tp = float(new_tp_list[0])
        except (ValueError, TypeError):
             logger.warning(f"{self.log_prefix} Invalid numeric SL/TP value provided for {action_description}: SL='{new_sl_val}', TPs='{new_tp_list}'")
             # Send a general failure message for the original signal ID
             await self._send_status_message(action_description, 0, 1, 1, details="\n<b>Reason:</b> Invalid SL/TP value in update message.")
             return # Stop execution if values are invalid

        if new_sl is None and new_tp is None:
            logger.info(f"{self.log_prefix} No valid new SL or TP found for {action_description} update.")
            await self._send_status_message(action_description, 0, 0, 0, details="\n<b>Info:</b> No valid SL/TP values found in update message.")
            return

        # Find all related active trades
        all_active_trades = self.state_manager.get_active_trades()
        related_trades = [t for t in all_active_trades if t.original_msg_id == self.original_msg_id]
        total_trades = len(related_trades)

        if not related_trades:
             logger.warning(f"{self.log_prefix} No active trades found for original message ID {self.original_msg_id}.")
             await self._send_status_message(action_description, 0, 0, 0, details="\n<b>Info:</b> No active trades found for the original signal.")
             return

        logger.info(f"{self.log_prefix} Found {total_trades} related trade(s) for OrigMsgID {self.original_msg_id}. Applying {action_description}...")

        sl_update_str = f"New SL: <code>{new_sl}</code>" if new_sl is not None else "<i>SL Unchanged</i>"
        tp_update_str = f"New TP: <code>{new_tp}</code>" if new_tp is not None else "<i>TP Unchanged</i>"
        details = f"\n<b>Details:</b> {sl_update_str}, {tp_update_str}"

        for trade in related_trades:
            logger.info(f"{self.log_prefix} Attempting to modify ticket {trade.ticket} with new SL={new_sl}, TP={new_tp}")
            # Pass SL/TP values correctly (use None if not provided in update)
            mod_success = self.mt5_executor.modify_trade(trade.ticket, sl=new_sl, tp=new_tp)
            if mod_success:
                success_count += 1
            else:
                failure_count += 1
                logger.error(f"{self.log_prefix} Failed to modify ticket {trade.ticket}.")

        # Send summary status message
        await self._send_status_message(action_description, success_count, failure_count, total_trades, details)


class SetBECommand(UpdateCommand):
    """Handles set_be updates."""
    async def execute(self):
        action_description = "Set SL to Breakeven"
        success_count = 0
        failure_count = 0
        details = ""

        # Find all related active trades
        all_active_trades = self.state_manager.get_active_trades()
        related_trades = [t for t in all_active_trades if t.original_msg_id == self.original_msg_id and not t.is_pending] # Only apply BE to non-pending
        total_trades = len(related_trades)

        if not related_trades:
             logger.warning(f"{self.log_prefix} No active, non-pending trades found for original message ID {self.original_msg_id} to set BE.")
             await self._send_status_message(action_description, 0, 0, 0, details="\n<b>Info:</b> No active trades found for the original signal.")
             return

        logger.info(f"{self.log_prefix} Found {total_trades} related active trade(s) for OrigMsgID {self.original_msg_id}. Applying {action_description}...")
        details = "\n<b>Details:</b> SL to BE attempted"

        for trade in related_trades:
             # Use context_trade_info's entry price for BE calculation if needed,
             # but modify_sl_to_breakeven fetches the actual entry price from the position.
             logger.info(f"{self.log_prefix} Attempting to set SL to Breakeven for ticket {trade.ticket}")
             mod_success = self.mt5_executor.modify_sl_to_breakeven(trade.ticket)
             if mod_success:
                 success_count += 1
             else:
                 failure_count += 1
                 logger.error(f"{self.log_prefix} Failed to set BE for ticket {trade.ticket}.")

        # Send summary status message
        await self._send_status_message(action_description, success_count, failure_count, total_trades, details)


class CloseTradeCommand(UpdateCommand):
    """Handles close_trade updates."""
    async def execute(self):
        # NOTE: Closing multiple trades based on a single "close" message might be risky.
        # Current implementation targets only the initially identified trade.
        # Consider if multi-close is desired and how to specify it clearly (e.g., "close all XAUUSD").
        # For now, keeping CloseTradeCommand targeting single ticket.
        action_description = "Close Trade"
        ticket_to_close = self.context_trade_info.ticket # Use the specific ticket identified initially
        entry_price_str = f"@{self.context_trade_info.entry_price}" if self.context_trade_info.entry_price is not None else "Market"

        # Check if position is already closed before attempting
        pos_info = mt5.positions_get(ticket=ticket_to_close)
        if not pos_info or len(pos_info) == 0:
            logger.info(f"{self.log_prefix} Position {ticket_to_close} already closed before close attempt.")
            # Use base class _send_status_message format for consistency
            await self._send_status_message(action_description, 1, 0, 1, details=f"\n<b>Info:</b> Position {ticket_to_close} already closed.")
            return

        logger.info(f"{self.log_prefix} Attempting to close trade for ticket {ticket_to_close}")
        # Assumes full close
        success = self.mt5_executor.close_position(ticket=ticket_to_close)
        details = f"\n<b>Ticket:</b> <code>{ticket_to_close}</code>"

        if not success:
            # Check if position is already closed after failed attempt
            pos_info = mt5.positions_get(ticket=ticket_to_close)
            if not pos_info or len(pos_info) == 0:
                logger.info(f"{self.log_prefix} Position {ticket_to_close} already closed after failed attempt.")
                # Report as success since the desired state (closed) is achieved
                await self._send_status_message(action_description, 1, 0, 1, details=f"\n<b>Info:</b> Position {ticket_to_close} already closed.")
                return

        # Use base class _send_status_message format
        await self._send_status_message(action_description, 1 if success else 0, 1 if not success else 0, 1, details=details)


class CancelPendingCommand(UpdateCommand):
    """Handles cancel_pending updates."""
    async def execute(self):
        # NOTE: Similar to CloseTrade, canceling multiple pending orders from one message might be risky.
        # Keeping CancelPendingCommand targeting single ticket for now.
        action_description = "Cancel Pending Order"
        ticket_to_cancel = self.context_trade_info.ticket # Use the specific ticket identified initially
        details = f"\n<b>Ticket:</b> <code>{ticket_to_cancel}</code>"

        logger.info(f"{self.log_prefix} Attempting to cancel pending order for ticket {ticket_to_cancel}")
        mod_success = self.mt5_executor.delete_pending_order(ticket_to_cancel)
        # Use base class _send_status_message format
        await self._send_status_message(action_description, 1 if mod_success else 0, 1 if not mod_success else 0, 1, details=details)


class UnknownUpdateCommand(UpdateCommand):
    """Handles unknown update types."""
    async def execute(self):
        action_description = "Unknown Update"
        logger.warning(f"{self.log_prefix} Update type classified as 'unknown' for OrigMsgID {self.original_msg_id}. No action taken.")
        # Use base class _send_status_message format
        details = "\n<b>Reason:</b> Could not determine specific action from message."
        await self._send_status_message(action_description, 0, 0, 0, details=details)


# --- Command Mapping ---
COMMAND_MAP = {
    "modify_sltp": ModifySLTPCommand,
    "move_sl": ModifySLTPCommand, # Handled by ModifySLTPCommand
    "set_be": SetBECommand,
    "close_trade": CloseTradeCommand,
    "cancel_pending": CancelPendingCommand,
    # "partial_close": PartialCloseCommand, # TODO: Implement if needed
    "unknown": UnknownUpdateCommand,
}

def get_command(update_type: str) -> Optional[Type[UpdateCommand]]:
    """Returns the command class for a given update type string."""
    return COMMAND_MAP.get(update_type, UnknownUpdateCommand) # Default to Unknown