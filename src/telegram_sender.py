import logging
import asyncio
from telethon import TelegramClient
from telethon.errors import FloodWaitError, UserDeactivatedBanError, AuthKeyError
from telethon.tl.custom import Button
import configparser
import sys
import uuid # To generate unique IDs for confirmations

logger = logging.getLogger('TradeBot')

class TelegramSender:
    """Handles connection to Telegram as a BOT account specifically for sending messages."""

    def __init__(self, config: configparser.ConfigParser):
        """
        Initializes the TelegramSender. Connects as a BOT.

        Args:
            config (configparser.ConfigParser): The application configuration.
        """
        self.config = config
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
        """Connects and authorizes the bot client."""
        if not self.bot_token:
            logger.error("Cannot connect sender: Bot token is missing.")
            return False

        logger.info(f"Initializing Telegram SENDER client (Bot Account) for session: {self.session_name}")
        self.client = TelegramClient(self.session_name, self.api_id, self.api_hash)

        try:
            logger.info("Connecting sender client to Telegram...")
            # Use start with bot_token. It handles connect() and authorization.
            # The start() method returns the client itself upon success.
            sender_client = await self.client.start(bot_token=self.bot_token)
            logger.info("Sender client connected and authorized successfully using token.")
            print("Telegram Sender connected.")

            # Get and store the sender bot's own ID
            if sender_client:
                me = await sender_client.get_me()
                if me:
                    self.sender_bot_id = me.id
                    logger.info(f"Stored Sender Bot ID: {self.sender_bot_id}")
                else:
                    logger.error("Could not get sender bot's own ID after connection.")
            else:
                 logger.error("Sender client object not returned from start(). Cannot get bot ID.")


            # Resolve channel ID after successful connection
            if not await self._resolve_target_channel():
                 logger.error("Sender connected but failed to resolve target channel ID. Sending will fail.")
                 logger.error("Sender connected but failed to resolve main target channel ID. Sending to main channel will fail.")
                 # Allow connection if debug channel resolves, otherwise fail
                 # return True # Connected, but main channel resolution failed

            # Resolve debug channel ID if configured
            await self._resolve_debug_channel() # Log messages inside this method

            # Fail connection if main channel resolution failed
            if not self.target_channel_id:
                 return False

            return True

            return True

        except FloodWaitError as fwe:
            logger.error(f"Flood wait during sender bot authorization: {fwe.seconds}s")
            print(f"Telegram flood wait for sender: {fwe.seconds}s. Please wait and restart.", file=sys.stderr)
            if self.client and self.client.is_connected(): await self.client.disconnect()
            return False
        except (UserDeactivatedBanError, AuthKeyError) as auth_err:
             # These might occur if api_id/hash are invalid, even with a valid bot token sometimes
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

        Args:
            confirmation_id (str): A unique identifier for this confirmation request.
            trade_details (dict): Dictionary containing details about the trade (e.g., symbol, action, volume). Used for logging/context.
            message_text (str): The main text of the message asking for confirmation.
            target_chat_id (int, optional): Specific chat ID to send to. Defaults to the main configured channel.

        Returns:
            telethon.tl.custom.message.Message or None: The sent message object if successful, otherwise None.
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
            # Can add more rows if needed
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