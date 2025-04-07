import logging
from abc import ABC, abstractmethod

from typing import Optional, Type

# Import necessary components
from .state_manager import StateManager
from .mt5_executor import MT5Executor
from .telegram_sender import TelegramSender
from .config_service import config_service
from .models import TradeInfo, UpdateData # Import relevant models

logger = logging.getLogger('TradeBot')

# --- Base Command Class ---
class UpdateCommand(ABC):
    """Abstract base class for handling trade update commands."""
    def __init__(self, update_data: UpdateData, target_trade_info: TradeInfo, mt5_executor: MT5Executor, # Use type hints
                 state_manager: StateManager, telegram_sender: TelegramSender,
                 config_service_instance, message_id, log_prefix):
        self.update_data = update_data
        self.target_trade_info = target_trade_info # This is now a TradeInfo object
        self.mt5_executor = mt5_executor
        self.state_manager = state_manager
        self.telegram_sender = telegram_sender
        self.config_service = config_service_instance # Store service instance
        self.message_id = message_id
        self.log_prefix = log_prefix
        self.debug_channel_id = getattr(telegram_sender, 'debug_target_channel_id', None)
        self.ticket_to_update = target_trade_info.ticket # Use attribute access
        self.entry_price_str = f"@{target_trade_info.entry_price}" if target_trade_info.entry_price is not None else "Market" # Use attribute access

    @abstractmethod
    async def execute(self):
        """Executes the specific update command."""
        pass

    async def _send_status_message(self, action_description, mod_success, details=""):
        """Helper to send standardized status messages."""
        safe_action_desc = str(action_description).replace('&', '&amp;').replace('<', '<').replace('>', '>')
        if mod_success:
            status_message_mod = f"""✅ <b>{safe_action_desc} Successful</b> <code>[MsgID: {self.message_id}]</code>
<b>Ticket:</b> <code>{self.ticket_to_update}</code> (Entry: {self.entry_price_str}){details}"""
            logger.info(f"{self.log_prefix} {action_description} successful for ticket {self.ticket_to_update}.")
        else:
            status_message_mod = f"❌ <b>{safe_action_desc} FAILED</b> <code>[MsgID: {self.message_id}]</code> (Ticket: <code>{self.ticket_to_update}</code>, Entry: {self.entry_price_str}). Check logs."
            logger.error(f"{self.log_prefix} {action_description} failed for ticket {self.ticket_to_update}.")

        await self.telegram_sender.send_message(status_message_mod, parse_mode='html')
        if self.debug_channel_id:
            debug_msg_update_result = f"🔄 {self.log_prefix} Update Action Result:\n{status_message_mod}"
            await self.telegram_sender.send_message(debug_msg_update_result, target_chat_id=self.debug_channel_id)


# --- Concrete Command Classes ---

class ModifySLTPCommand(UpdateCommand):
    """Handles modify_sltp and move_sl updates."""
    async def execute(self):
        action_description = "Modify SL/TP"
        new_sl = None
        new_tp = None
        new_sl_val = self.update_data.new_stop_loss # Use attribute access
        new_tp_list = self.update_data.new_take_profits # Use attribute access
        mod_success = False
        status_message_mod = ""
        details = ""

        try:
            if new_sl_val != "N/A": new_sl = float(new_sl_val)
            # Use first TP from list for modification
            if new_tp_list and new_tp_list[0] != "N/A":
                new_tp = float(new_tp_list[0])
        except (ValueError, TypeError):
             logger.warning(f"{self.log_prefix} Invalid numeric SL/TP value provided for modify_sltp/move_sl: SL='{new_sl_val}', TPs='{new_tp_list}'")
             status_message_mod = f"⚠️ <b>Update Warning</b> <code>[MsgID: {self.message_id}]</code> (Ticket: <code>{self.ticket_to_update}</code>, Entry: {self.entry_price_str}). Invalid SL/TP value provided."
             await self.telegram_sender.send_message(status_message_mod, parse_mode='html')
             return # Stop execution if values are invalid

        if new_sl is not None or new_tp is not None:
            logger.info(f"{self.log_prefix} Attempting to modify MT5 order/position {self.ticket_to_update} with new SL={new_sl}, TP={new_tp}")
            mod_success = self.mt5_executor.modify_trade(self.ticket_to_update, sl=new_sl, tp=new_tp)
            sl_update_str = f"New SL: <code>{new_sl}</code>" if new_sl is not None else "<i>SL Unchanged</i>"
            tp_update_str = f"New TP: <code>{new_tp}</code>" if new_tp is not None else "<i>TP Unchanged</i>"
            details = f"\n<b>Details:</b> {sl_update_str}, {tp_update_str}"
            await self._send_status_message(action_description, mod_success, details)
        else:
            logger.info(f"{self.log_prefix} No valid new SL or TP found for modify_sltp/move_sl update.")
            status_message_mod = f"ℹ️ <b>Update Info</b> <code>[MsgID: {self.message_id}]</code> (Ticket: <code>{self.ticket_to_update}</code>, Entry: {self.entry_price_str}). No valid SL/TP values found in message."
            await self.telegram_sender.send_message(status_message_mod, parse_mode='html')


class SetBECommand(UpdateCommand):
    """Handles set_be updates."""
    async def execute(self):
        action_description = "Set SL to Breakeven"
        logger.info(f"{self.log_prefix} Attempting to set SL to Breakeven for ticket {self.ticket_to_update}")
        mod_success = self.mt5_executor.modify_sl_to_breakeven(self.ticket_to_update)
        details = "\n<b>Details:</b> SL to BE" if mod_success else ""
        await self._send_status_message(action_description, mod_success, details)


class CloseTradeCommand(UpdateCommand):
    """Handles close_trade updates."""
    async def execute(self):
        action_description = "Close Trade"
        logger.info(f"{self.log_prefix} Attempting to close trade for ticket {self.ticket_to_update}")
        # TODO: Handle partial close volume/percentage if provided in update_data
        # close_vol = self.update_data.get('close_volume', 'N/A')
        # close_perc = self.update_data.get('close_percentage', 'N/A')
        # For now, assumes full close
        mod_success = self.mt5_executor.close_position(self.ticket_to_update) # Close full position
        await self._send_status_message(action_description, mod_success)


class CancelPendingCommand(UpdateCommand):
    """Handles cancel_pending updates."""
    async def execute(self):
        action_description = "Cancel Pending Order"
        logger.info(f"{self.log_prefix} Attempting to cancel pending order for ticket {self.ticket_to_update}")
        mod_success = self.mt5_executor.delete_pending_order(self.ticket_to_update)
        await self._send_status_message(action_description, mod_success)


class UnknownUpdateCommand(UpdateCommand):
    """Handles unknown update types."""
    async def execute(self):
        logger.warning(f"{self.log_prefix} Update type classified as 'unknown'. No action taken.")
        status_message_mod = f"❓ <b>Update Unclear</b> <code>[MsgID: {self.message_id}]</code> (Ticket: <code>{self.ticket_to_update}</code>, Entry: {self.entry_price_str}). Could not determine specific action from message."
        await self.telegram_sender.send_message(status_message_mod, parse_mode='html')


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