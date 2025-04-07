import logging
import asyncio
import re # For parsing callback data
import html # For escaping HTML
import uuid # To generate unique IDs for confirmations
import configparser
import sys
from datetime import datetime, timezone, timedelta

import MetaTrader5 as mt5 # Import the MT5 library
from telethon import TelegramClient, events # Import events
from telethon.errors import FloodWaitError, UserDeactivatedBanError, AuthKeyError, MessageNotModifiedError, MessageIdInvalidError
from telethon.tl.custom import Button

# Import component types for type hinting
from .state_manager import StateManager
from .mt5_executor import MT5Executor
from .mt5_connector import MT5Connector
from .mt5_data_fetcher import MT5DataFetcher # Import MT5DataFetcher


logger = logging.getLogger('TradeBot')

class TelegramSender:
    """
    Handles connection to Telegram as a BOT account, sends messages,
    and now also handles callback queries from its own messages.
    """

    def __init__(self, config: configparser.ConfigParser,
                 state_manager: StateManager, mt5_executor: MT5Executor,
                 mt5_connector: MT5Connector, mt5_fetcher: MT5DataFetcher): # Added mt5_fetcher
        """
        Initializes the TelegramSender. Connects as a BOT.

        Args:
            config (configparser.ConfigParser): The application configuration.
            state_manager (StateManager): Instance for managing state.
            mt5_executor (MT5Executor): Instance for executing trades.
            mt5_connector (MT5Connector): Instance for MT5 connection checks.
            mt5_fetcher (MT5DataFetcher): Instance for fetching market data.
        """
        self.config = config
        self.state_manager = state_manager
        self.mt5_executor = mt5_executor
        self.mt5_connector = mt5_connector
        self.mt5_fetcher = mt5_fetcher # Store mt5_fetcher

        # Need api_id/hash even for bot connection via Telethon library
        self.api_id = config.getint('Telegram', 'api_id')
        self.api_hash = config.get('Telegram', 'api_hash')
        self.bot_token = config.get('Telegram', 'bot_token', fallback=None)
        self.channel_id_config = config.get('Telegram', 'channel_id') # Main channel
        self.debug_channel_id_config = config.get('Telegram', 'debug_channel_id', fallback=None) # Optional debug channel

        if not self.bot_token:
            logger.critical("Telegram bot_token not found in configuration. TelegramSender cannot function.")
            # Or raise an error if sender is critical
            # raise ValueError("Missing Telegram Bot Token for Sender")

        # Use a distinct session name for the bot sender account
        self.session_name = f"telegram_sender_session_{self.api_id}" # Use api_id for uniqueness
        self.client = None
        self.sender_bot_id = None # To store the bot's own ID
        self.target_channel_id = None # Main channel ID, resolved after connection
        self.debug_target_channel_id = None # Debug channel ID, resolved after connection

    async def _resolve_target_channel(self):
        """Resolves the channel ID/username from config to a numeric ID."""
        channel_input = self.channel_id_config
        if not channel_input:
            logger.error("Target Telegram channel_id not specified in configuration for sender.")
            return False

        if not self.client or not self.client.is_connected():
             logger.error("Cannot resolve channel, sender client not connected.")
             return False

        try:
            # Try parsing as integer first
            try:
                channel_id_int = int(channel_input)
                # Verify access by getting entity (optional but good practice)
                await self.client.get_input_entity(channel_id_int)
                self.target_channel_id = channel_id_int
                logger.info(f"Sender using channel ID: {self.target_channel_id}")
                return True
            except ValueError:
                # Treat as username/link
                logger.info(f"Sender attempting to resolve channel username/link: {channel_input}")
                entity = await self.client.get_input_entity(channel_input)
                if hasattr(entity, 'id'):
                    self.target_channel_id = entity.id
                    logger.info(f"Sender resolved '{channel_input}' to channel ID: {self.target_channel_id}")
                    return True
                else:
                    logger.error(f"Could not resolve '{channel_input}' to a usable entity ID.")
                    return False
        except FloodWaitError as fwe:
             logger.error(f"Flood wait error when resolving channel for sender: waiting {fwe.seconds} seconds.")
             await asyncio.sleep(fwe.seconds + 1)
             return await self._resolve_target_channel() # Retry
        except Exception as e:
            logger.error(f"Could not find or access channel '{channel_input}' for sender: {e}", exc_info=True)
            return False

    async def _resolve_debug_channel(self):
        """Resolves the debug channel ID/username from config to a numeric ID."""
        channel_input = self.debug_channel_id_config
        if not channel_input:
            logger.info("No debug_channel_id configured. Debug messages via sender disabled.")
            return False # Not an error, just not configured

        if not self.client or not self.client.is_connected():
             logger.error("Cannot resolve debug channel, sender client not connected.")
             return False

        try:
            # Try parsing as integer first
            try:
                channel_id_int = int(channel_input)
                await self.client.get_input_entity(channel_id_int) # Verify access
                self.debug_target_channel_id = channel_id_int
                logger.info(f"Sender using debug channel ID: {self.debug_target_channel_id}")
                return True
            except ValueError:
                # Treat as username/link
                logger.info(f"Sender attempting to resolve debug channel username/link: {channel_input}")
                entity = await self.client.get_input_entity(channel_input)
                if hasattr(entity, 'id'):
                    self.debug_target_channel_id = entity.id
                    logger.info(f"Sender resolved debug channel '{channel_input}' to ID: {self.debug_target_channel_id}")
                    return True
                else:
                    logger.error(f"Could not resolve debug channel '{channel_input}' to a usable entity ID.")
                    return False
        except FloodWaitError as fwe:
             logger.error(f"Flood wait error when resolving debug channel for sender: waiting {fwe.seconds} seconds.")
             await asyncio.sleep(fwe.seconds + 1)
             return await self._resolve_debug_channel() # Retry
        except Exception as e:
            logger.error(f"Could not find or access debug channel '{channel_input}' for sender: {e}", exc_info=True)
            return False


    async def connect(self):
        """Connects and authorizes the bot client and adds callback handler."""
        if not self.bot_token:
            logger.error("Cannot connect sender: Bot token is missing.")
            return False

        logger.info(f"Initializing Telegram SENDER client (Bot Account) for session: {self.session_name}")
        self.client = TelegramClient(self.session_name, self.api_id, self.api_hash)

        try:
            logger.info("Connecting sender client to Telegram...")
            # Use start with bot_token. It handles connect() and authorization.
            sender_client = await self.client.start(bot_token=self.bot_token)
            if not sender_client or not self.client.is_connected():
                 logger.critical("Failed to start or connect the Telegram sender client using token.")
                 return False
            logger.info("Sender client connected and authorized successfully using token.")
            print("Telegram Sender connected.")

            # Get and store the sender bot's own ID
            me = await self.client.get_me()
            if me:
                self.sender_bot_id = me.id
                logger.info(f"Stored Sender Bot ID: {self.sender_bot_id}")
            else:
                logger.error("Could not get sender bot's own ID after connection.")

            # Resolve channel IDs
            if not await self._resolve_target_channel():
                 logger.error("Sender connected but failed to resolve main target channel ID. Sending to main channel will fail.")
                 # Continue if debug channel resolves? For now, require main channel.
                 # return False # Uncomment if main channel is strictly required

            await self._resolve_debug_channel()

            # Fail connection if main channel resolution failed (if strictly required)
            # if not self.target_channel_id:
            #      return False

            # --- Add Callback Query Handler ---
            self.client.add_event_handler(self._handle_callback_query, events.CallbackQuery)
            logger.info("Added callback query handler to sender client.")
            # --- End Handler Addition ---

            return True

        except FloodWaitError as fwe:
            logger.error(f"Flood wait during sender bot authorization: {fwe.seconds}s")
            print(f"Telegram flood wait for sender: {fwe.seconds}s. Please wait and restart.", file=sys.stderr)
            if self.client and self.client.is_connected(): await self.client.disconnect()
            return False
        except (UserDeactivatedBanError, AuthKeyError) as auth_err:
             logger.critical(f"Sender authorization failed: {auth_err}. Check API credentials.")
             print(f"CRITICAL: Telegram sender authorization failed ({auth_err}). Check api_id/api_hash.", file=sys.stderr)
             if self.client and self.client.is_connected(): await self.client.disconnect()
             return False
        except Exception as e:
            logger.critical(f"Failed to start Telegram Sender client: {e}", exc_info=True)
            print(f"Error during sender bot authorization: {e}. Check your bot_token and API keys.", file=sys.stderr)
            if self.client and self.client.is_connected(): await self.client.disconnect()
            return False

    async def disconnect(self):
        """Disconnects the bot client."""
        if self.client and self.client.is_connected():
            logger.info("Disconnecting Telegram Sender client...")
            await self.client.disconnect()
            logger.info("Telegram Sender client disconnected.")
        else:
            logger.info("Telegram Sender client already disconnected or not initialized.")

    async def send_message(self, message_text, parse_mode='html', target_chat_id=None):
        """Sends a text message to the target channel using the bot account."""
        if not self.client or not self.client.is_connected():
            logger.error("Cannot send message, Telegram Sender client not connected.")
            return False
        # Determine the actual target ID
        actual_target_id = target_chat_id if target_chat_id is not None else self.target_channel_id

        if not actual_target_id:
             log_target_desc = "debug" if target_chat_id is not None else "main"
             logger.error(f"Cannot send message, target {log_target_desc} channel ID not resolved or provided.")
             return False

        try:
            log_target_desc = f"channel {actual_target_id}" if actual_target_id == self.target_channel_id else f"debug channel {actual_target_id}"
            logger.info(f"Sender sending message to {log_target_desc} (Mode: {parse_mode}): {message_text[:100]}...")
            await self.client.send_message(
                actual_target_id,
                message_text,
                parse_mode=parse_mode
            )
            logger.debug("Sender message sent successfully.")
            return True
        except FloodWaitError as fwe:
             logger.error(f"Flood wait error when sending message: waiting {fwe.seconds} seconds.")
             print(f"Telegram flood wait on send: {fwe.seconds}s", file=sys.stderr)
             # Don't wait here, just report failure for now
             return False
        except Exception as e:
            # Catch potential formatting errors from Telegram here too
            logger.error(f"Sender failed to send message to channel {actual_target_id}: {e}", exc_info=True)
            # Attempt to send plain text version as fallback? Optional.
            try:
                 logger.warning("Attempting to send message as plain text due to formatting error.")
                 await self.client.send_message(actual_target_id, message_text, parse_mode=None)
                 return True # Sent plain text successfully
            except Exception as fallback_e:
                 logger.error(f"Failed to send plain text fallback message: {fallback_e}", exc_info=True)
                 return False # Both formatted and plain text failed

    async def send_confirmation_message(self, confirmation_id: str, trade_details: dict, message_text: str, target_chat_id=None):
        """
        Sends a message with Yes/No inline buttons for trade confirmation.
        """
        if not self.client or not self.client.is_connected():
            logger.error("Cannot send confirmation message, Telegram Sender client not connected.")
            return None

        actual_target_id = target_chat_id if target_chat_id is not None else self.target_channel_id

        if not actual_target_id:
            log_target_desc = "debug" if target_chat_id is not None else "main"
            logger.error(f"Cannot send confirmation message, target {log_target_desc} channel ID not resolved or provided.")
            return None

        # Define the inline buttons
        buttons = [
            [ # First row
                Button.inline("✅ Yes", data=f"confirm_yes_{confirmation_id}"),
                Button.inline("❌ No", data=f"confirm_no_{confirmation_id}")
            ]
        ]

        try:
            log_target_desc = f"channel {actual_target_id}" if actual_target_id == self.target_channel_id else f"debug channel {actual_target_id}"
            logger.info(f"Sender sending confirmation message (ID: {confirmation_id}) to {log_target_desc}: {message_text[:100]}...")
            logger.debug(f"Trade details for confirmation {confirmation_id}: {trade_details}")

            sent_message = await self.client.send_message(
                actual_target_id,
                message_text,
                buttons=buttons,
                parse_mode='html' # Or None if you don't need formatting
            )
            logger.info(f"Confirmation message (ID: {confirmation_id}) sent successfully to {log_target_desc}. Message ID: {sent_message.id}")
            return sent_message
        except FloodWaitError as fwe:
            logger.error(f"Flood wait error when sending confirmation message (ID: {confirmation_id}): waiting {fwe.seconds} seconds.")
            print(f"Telegram flood wait on send confirmation: {fwe.seconds}s", file=sys.stderr)
            return None
        except Exception as e:
            logger.error(f"Sender failed to send confirmation message (ID: {confirmation_id}) to channel {actual_target_id}: {e}", exc_info=True)
            # Optionally try plain text fallback
            try:
                logger.warning(f"Attempting to send confirmation message (ID: {confirmation_id}) as plain text due to error.")
                sent_message = await self.client.send_message(
                    actual_target_id,
                    message_text,
                    buttons=buttons,
                    parse_mode=None
                )
                logger.info(f"Plain text confirmation message (ID: {confirmation_id}) sent successfully. Message ID: {sent_message.id}")
                return sent_message
            except Exception as fallback_e:
                logger.error(f"Failed to send plain text fallback confirmation message (ID: {confirmation_id}): {fallback_e}", exc_info=True)
                return None

    # --- Internal Callback Query Handler ---
    async def _handle_callback_query(self, event: events.CallbackQuery.Event):
        """Internal handler for callback queries received by this bot client."""
        confirmation_id = "N/A" # Default for logging if parsing fails
        log_prefix = "[CallbackQuery]" # Base prefix
        try:
            callback_data = event.data.decode('utf-8')
            log_prefix_base = f"[Callback User: {event.sender_id}]"
            logger.info(f"{log_prefix_base} Received callback query with data: {callback_data}")

            match = re.match(r"confirm_(yes|no)_([a-f0-9\-]+)", callback_data)

            if not match:
                logger.warning(f"{log_prefix_base} Received callback query with unexpected data format: {callback_data}")
                await event.answer("Unknown request format.", alert=True)
                return

            choice = match.group(1)
            confirmation_id = match.group(2)
            log_prefix = f"[Callback ConfID: {confirmation_id}]"
            logger.info(f"{log_prefix} Parsed confirmation. Choice: '{choice}', User: {event.sender_id}")

            final_message_text = ""
            alert_answer = False
            answer_text = ""

            # 1. Get Pending Confirmation Details
            logger.debug(f"{log_prefix} Attempting to get pending confirmation details...")
            pending_conf = self.state_manager.get_pending_confirmation(confirmation_id)

            if not pending_conf:
                logger.warning(f"{log_prefix} Confirmation ID not found or already processed.")
                answer_text = "This confirmation request is invalid or has expired."
                alert_answer = True
                await event.answer(answer_text, alert=alert_answer)
                return

            conf_timestamp = pending_conf['timestamp']
            conf_message_id = pending_conf['message_id']
            trade_params = pending_conf['trade_details']
            original_signal_msg_id = trade_params.get('original_signal_msg_id', 'N/A')
            logger.debug(f"{log_prefix} Found pending confirmation. MsgID: {conf_message_id}, Timestamp: {conf_timestamp}")

            # Prepare details for status messages (used in multiple outcomes)
            action_str = trade_params.get('action', 'N/A')
            symbol_str = trade_params.get('symbol', 'N/A')
            volume_str = trade_params.get('volume', 'N/A')
            sl_param = trade_params.get('sl')
            tp_param = trade_params.get('tp')
            sl_str_fmt = f"<code>{sl_param}</code>" if sl_param is not None else "<i>None</i>"
            tp_str_fmt = f"<code>{tp_param}</code>" if tp_param is not None else "<i>None</i>"

            # --- Fetch Current Price ---
            current_price_str = "<i>N/A</i>"
            if self.mt5_fetcher and symbol_str != 'N/A':
                tick = self.mt5_fetcher.get_symbol_tick(symbol_str)
                if tick:
                    current_price_str = f"Bid: <code>{tick.bid}</code> Ask: <code>{tick.ask}</code>"
                    logger.debug(f"{log_prefix} Fetched current price: {current_price_str}")
                else:
                    logger.warning(f"{log_prefix} Could not fetch current tick for {symbol_str}.")
                    current_price_str = "<i>Error fetching</i>"
            # --- End Fetch Current Price ---


            # 2. Check Expiry
            logger.debug(f"{log_prefix} Checking expiry...")
            timeout_minutes = self.config.getint('Trading', 'market_confirmation_timeout_minutes', fallback=3)
            expiry_time = conf_timestamp + timedelta(minutes=timeout_minutes)
            now = datetime.now(timezone.utc)

            if now > expiry_time:
                logger.warning(f"{log_prefix} Confirmation request expired (Expiry: {expiry_time}, Now: {now}).")
                answer_text = "This confirmation request has expired."
                alert_answer = True
                expiry_time_str = expiry_time.strftime('%Y-%m-%d %H:%M:%S %Z')
                # Construct Expired message
                final_message_text = f"""⏳ <b>Confirmation Expired</b> <code>[OrigMsgID: {original_signal_msg_id}]</code>

<b>Action:</b> <code>{action_str}</code>
<b>Symbol:</b> <code>{symbol_str}</code>
<b>Volume:</b> <code>{volume_str}</code>
<b>SL:</b> {sl_str_fmt} | <b>TP:</b> {tp_str_fmt}
<b>Price at Expiry:</b> {current_price_str}
<i>(Expired at {expiry_time_str})</i>"""
                logger.debug(f"{log_prefix} Removing expired confirmation from state...")
                self.state_manager.remove_pending_confirmation(confirmation_id)
                try:
                    logger.debug(f"{log_prefix} Attempting to edit message for expiry...")
                    await event.edit(final_message_text, parse_mode='html', buttons=None)
                    logger.debug(f"{log_prefix} Edited message for expiry.")
                except MessageNotModifiedError:
                     logger.warning(f"{log_prefix} Message was not modified (likely already expired/edited).")
                except Exception as edit_err:
                    logger.error(f"{log_prefix} Failed to edit confirmation message after expiry: {edit_err}")
                logger.debug(f"{log_prefix} Answering callback for expiry...")
                await event.answer(answer_text, alert=alert_answer)
                return

            # 3. Process Choice ('yes' or 'no')
            # --- IMPORTANT: Remove pending confirmation *immediately* ---
            logger.debug(f"{log_prefix} Attempting to remove pending confirmation from state...")
            if not self.state_manager.remove_pending_confirmation(confirmation_id):
                logger.warning(f"{log_prefix} Confirmation ID was already removed before processing choice '{choice}'. Ignoring duplicate callback.")
                await event.answer("Request already processed.", alert=True)
                return
            logger.info(f"{log_prefix} Removed pending confirmation from state.")
            # --- End Immediate Removal ---

            if choice == 'yes':
                logger.info(f"{log_prefix} User confirmed trade. Processing execution...")
                answer_text = "Processing trade execution..."
                alert_answer = False
                logger.debug(f"{log_prefix} Answering callback before execution...")
                await event.answer(answer_text, alert=alert_answer) # Answer immediately

                # Ensure MT5 connection
                logger.debug(f"{log_prefix} Ensuring MT5 connection...")
                if not self.mt5_connector.ensure_connection():
                     logger.error(f"{log_prefix} MT5 connection failed. Cannot execute confirmed trade.")
                     answer_text = "Error: Cannot connect to trading platform."
                     alert_answer = True
                     # Construct Connection Failed message
                     final_message_text = f"""❌ <b>Execution Failed</b> (User Confirmed) <code>[OrigMsgID: {original_signal_msg_id}]</code>

<b>Action:</b> <code>{action_str}</code>
<b>Symbol:</b> <code>{symbol_str}</code>
<b>Volume:</b> <code>{volume_str}</code>
<b>SL:</b> {sl_str_fmt} | <b>TP:</b> {tp_str_fmt}
<b>Price at Attempt:</b> {current_price_str}
<b>Reason:</b> Could not connect to MT5."""
                     # State already removed
                else:
                    logger.info(f"{log_prefix} MT5 connection OK. Executing trade...")
                    # Execute the trade
                    execution_args = {
                        "action": trade_params.get('action'), "symbol": trade_params.get('symbol'),
                        "order_type": trade_params.get('order_type'), "volume": trade_params.get('volume'),
                        "price": trade_params.get('price'), "sl": trade_params.get('sl'),
                        "tp": trade_params.get('tp'), "comment": trade_params.get('comment')
                    }
                    logger.debug(f"{log_prefix} Executing trade with filtered args: {execution_args}")
                    trade_result_tuple = self.mt5_executor.execute_trade(**execution_args)
                    trade_result, actual_exec_price = trade_result_tuple if trade_result_tuple else (None, None)
                    logger.info(f"{log_prefix} Trade execution result: {trade_result_tuple}")

                    if trade_result and trade_result.retcode == mt5.TRADE_RETCODE_DONE:
                        ticket = trade_result.order
                        open_time = datetime.now(timezone.utc)
                        final_entry_price = actual_exec_price
                        entry_price_str = f"<code>@{final_entry_price}</code>" if final_entry_price is not None else "<i>(Price not returned by MT5)</i>"
                        logger.info(f"{log_prefix} Confirmed trade executed successfully. Ticket: {ticket}, Actual Entry: {entry_price_str}")

                        # Construct Success message
                        final_message_text = f"""✅ <b>Trade Executed</b> (User Confirmed) <code>[OrigMsgID: {original_signal_msg_id}]</code>

<b>Ticket:</b> <code>{ticket}</code>
<b>Symbol:</b> <code>{symbol_str}</code>
<b>Action:</b> <code>{action_str}</code>
<b>Volume:</b> <code>{volume_str}</code>
<b>Actual Entry:</b> {entry_price_str}
<b>SL:</b> {sl_str_fmt} | <b>TP:</b> {tp_str_fmt}
<b>Price at Execution:</b> {current_price_str}"""
                        answer_text = f"Trade executed! Ticket: {ticket}"
                        alert_answer = False

                        logger.debug(f"{log_prefix} Recording market execution time...")
                        self.state_manager.record_market_execution()

                        logger.debug(f"{log_prefix} Storing active trade info...")
                        trade_info = {
                            'ticket': ticket, 'symbol': trade_params['symbol'], 'open_time': open_time,
                            'original_msg_id': original_signal_msg_id, 'entry_price': final_entry_price,
                            'initial_sl': trade_params.get('sl'), 'original_volume': trade_params['volume'],
                            'all_tps': [], # TODO: Need original TPs here
                            'tp_strategy': self.config.get('Strategy', 'tp_execution_strategy', fallback='first_tp_full_close').lower(),
                            'next_tp_index': 0, 'tsl_active': False
                        }
                        if self.state_manager:
                            auto_tp_was_applied = trade_params.get('auto_tp_applied', False)
                            self.state_manager.add_active_trade(trade_info, auto_tp_applied=auto_tp_was_applied)
                            if self.config.getboolean('AutoSL', 'enable_auto_sl', fallback=False) and trade_params.get('sl') is None:
                                self.state_manager.mark_trade_for_auto_sl(ticket)
                            logger.debug(f"{log_prefix} Active trade info stored.")
                        else:
                            logger.error(f"{log_prefix} Cannot store active trade info: StateManager not available.")

                    else: # Execution failed
                        error_comment = getattr(trade_result, 'comment', 'Unknown Error') if trade_result else 'None Result'
                        error_code = getattr(trade_result, 'retcode', 'N/A') if trade_result else 'N/A'
                        logger.error(f"{log_prefix} Confirmed trade execution FAILED. Result: {trade_result_tuple}")
                        safe_comment = html.escape(str(error_comment))
                        # Construct Execution Failed message
                        final_message_text = f"""❌ <b>Execution Failed</b> (User Confirmed) <code>[OrigMsgID: {original_signal_msg_id}]</code>

<b>Action:</b> <code>{action_str}</code>
<b>Symbol:</b> <code>{symbol_str}</code>
<b>Volume:</b> <code>{volume_str}</code>
<b>SL:</b> {sl_str_fmt} | <b>TP:</b> {tp_str_fmt}
<b>Price at Attempt:</b> {current_price_str}
<b>Reason:</b> {safe_comment} (Code: <code>{error_code}</code>)"""
                        answer_text = "Trade execution failed. Check logs."
                        alert_answer = True
                    # State already removed

            elif choice == 'no':
                logger.info(f"{log_prefix} User rejected trade.")
                answer_text = "Trade rejected by user."
                alert_answer = False
                # Construct Rejected message
                final_message_text = f"""❌ <b>Trade Rejected</b> (User Cancelled) <code>[OrigMsgID: {original_signal_msg_id}]</code>

<b>Action:</b> <code>{action_str}</code>
<b>Symbol:</b> <code>{symbol_str}</code>
<b>Volume:</b> <code>{volume_str}</code>
<b>SL:</b> {sl_str_fmt} | <b>TP:</b> {tp_str_fmt}
<b>Price at Rejection:</b> {current_price_str}"""
                # State already removed
                logger.debug(f"{log_prefix} Answering callback for rejection...")
                await event.answer(answer_text, alert=alert_answer) # Answer before editing

            else: # Should not happen
                logger.warning(f"{log_prefix} Unknown choice '{choice}' received.")
                answer_text = "Unknown choice received."
                alert_answer = True
                await event.answer(answer_text, alert=alert_answer)
                # State already removed
                return # Don't edit message

            # 4. Edit Original Confirmation Message (if text was set)
            if final_message_text:
                try:
                    logger.debug(f"{log_prefix} Attempting to edit message with final status...")
                    await event.edit(final_message_text, parse_mode='html', buttons=None) # Remove buttons after editing
                    logger.info(f"{log_prefix} Edited original confirmation message (ID: {conf_message_id}).")
                except MessageNotModifiedError:
                     logger.warning(f"{log_prefix} Message was not modified (likely already edited).")
                except Exception as edit_err:
                    logger.error(f"{log_prefix} Failed to edit confirmation message (ID: {conf_message_id}): {edit_err}")
                    # If edit fails, maybe try answering again with the final status?
                    if choice == 'yes': # Only for 'yes' path where initial answer was temporary
                         try:
                              logger.debug(f"{log_prefix} Edit failed, attempting to answer callback again with final status...")
                              await event.answer(answer_text, alert=alert_answer)
                         except Exception as answer_again_err:
                              logger.error(f"{log_prefix} Failed to answer callback again after edit error: {answer_again_err}")


        except Exception as callback_err:
            logger.error(f"{log_prefix} Unhandled error in _handle_callback_query: {callback_err}", exc_info=True)
            try:
                logger.debug(f"{log_prefix} Answering callback due to unhandled error...")
                await event.answer("An internal error occurred processing the confirmation.", alert=True)
            except Exception as final_answer_err:
                logger.error(f"{log_prefix} Failed to answer callback query after unhandled error: {final_answer_err}")