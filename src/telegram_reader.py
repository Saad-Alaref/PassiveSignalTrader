import logging
import asyncio
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError, FloodWaitError, UserDeactivatedBanError, AuthKeyError # Removed RpcError
from telethon.sessions import StringSession # Keep for potential future use if needed
import configparser
import sys
import getpass # For password input if needed

logger = logging.getLogger('TradeBot')

# Note: Removed the raw_update_handler as it's no longer needed for this approach

class TelegramReader:
    """Handles connection to Telegram as a USER account to monitor a specific channel."""

    def __init__(self, config: configparser.ConfigParser, message_handler_callback):
        """
        Initializes the TelegramReader. Connects as a USER.

        Args:
            config (configparser.ConfigParser): The application configuration.
            message_handler_callback (callable): An async function to call when a new
                                                 message or relevant edit is received.
                                                 It should accept the Telethon event object.
        """
        self.config = config
        # Read required fields - validation ensures they exist
        # self.bot_token = config.get('Telegram', 'bot_token') # No longer needed for reader
        self.channel_id_config = config.get('Telegram', 'channel_id')
        try:
            # api_id must be int, api_hash is string
            self.api_id = config.getint('Telegram', 'api_id')
            self.api_hash = config.get('Telegram', 'api_hash')
        except ValueError as e:
             logger.critical(f"Invalid api_id in config (must be an integer): {e}")
             raise ValueError("Invalid api_id in config") from e

        # Session name based on api_id for potential multi-instance distinction
        # Use a distinct session name for the user account reader
        self.session_name = f"telegram_reader_session_{self.api_id}"
        self.client = None
        self.target_channel_id = None
        self.message_handler = message_handler_callback # Store the callback for received messages

        # Validation for api_id/api_hash should be handled by config_loader

    async def _get_channel_entity(self):
        """Resolves the channel ID/username from config to a Telethon entity."""
        channel_input = self.channel_id_config # Use stored value
        if not channel_input:
            logger.critical("Target Telegram channel_id not specified in configuration.")
            return None

        try:
            # Try parsing as integer first (for channel IDs like -100...)
            try:
                channel_id_int = int(channel_input)
                self.target_channel_id = channel_id_int
                logger.info(f"Attempting to use channel ID: {self.target_channel_id}")
                # For IDs, we might not need get_entity if we use it directly in events.NewMessage
                # However, fetching it verifies access.
                # Use get_input_entity for potentially better type handling
                entity = await self.client.get_input_entity(self.target_channel_id)
                return entity
            except ValueError:
                # If not an integer, treat as username or invite link
                logger.info(f"Attempting to resolve channel username/link: {channel_input}")
                self.target_channel_id = channel_input # Store the username/link
                # Use get_input_entity here as well
                entity = await self.client.get_input_entity(channel_input)
                # Store the resolved numeric ID if possible
                if hasattr(entity, 'id'):
                     # Telethon might add 100 prefix, ensure it matches typical ID format if needed
                     # For channel IDs, they are usually negative. PeerChannel might be positive.
                     # Let's store the raw ID Telethon gives.
                     self.target_channel_id = entity.id
                     logger.info(f"Resolved '{channel_input}' to channel ID: {self.target_channel_id}")
                return entity

        except FloodWaitError as fwe:
             logger.error(f"Flood wait error when getting channel entity: waiting {fwe.seconds} seconds.")
             print(f"Telegram flood wait: {fwe.seconds}s", file=sys.stderr)
             await asyncio.sleep(fwe.seconds + 1)
             return await self._get_channel_entity() # Retry after waiting
        except Exception as e:
            logger.critical(f"Could not find or access channel '{channel_input}': {e}", exc_info=True)
            print(f"CRITICAL: Could not find or access Telegram channel '{channel_input}'. Please check the channel_id in config and ensure your account has access.", file=sys.stderr)
            return None

    async def start(self):
        """Connects to Telegram and starts listening for messages."""
        logger.info(f"Initializing Telegram READER client (User Account) for session: {self.session_name}")
        # Pass the configured api_id and api_hash to the constructor
        # Use a file session based on the session_name
        self.client = TelegramClient(self.session_name, self.api_id, self.api_hash,
                                     system_version="4.16.30-vxCUSTOM")

        try:
            logger.info("Connecting to Telegram as USER account...")
            # Ensure client is connected before authorization attempt
            if not self.client.is_connected():
                 await self.client.connect()
            if not self.client.is_connected():
                 logger.critical("Failed to connect the Telegram client.")
                 return False
            logger.info("Client Connected.")

            # --- User Account Authorization ---
            if await self.client.is_user_authorized():
                logger.info("User account already authorized.")
            else:
                logger.info("User account not authorized. Attempting authorization...")
                try:
                    # Attempt to sign in using api_id/hash. May require code/password interactively.
                    await self.client.start() # No args needed for user auth if session exists or interactive
                    logger.info("User account authorized successfully.")
                    print("User account authorized successfully.")
                    # Consider adding phone/code/password callbacks if needed for first run
                except FloodWaitError as fwe:
                    logger.error(f"Flood wait during user authorization: {fwe.seconds}s")
                    print(f"Telegram flood wait: {fwe.seconds}s. Please wait and restart.", file=sys.stderr)
                    await self.client.disconnect()
                    return False
                except (UserDeactivatedBanError, AuthKeyError) as auth_err:
                     logger.critical(f"Authorization failed: {auth_err}. Check API credentials or account status.")
                     print(f"CRITICAL: Telegram authorization failed ({auth_err}). Check api_id/api_hash.", file=sys.stderr)
                     await self.client.disconnect()
                     return False
                # RpcError was previously handled here, but removed due to import issues.
                # The generic Exception handler below will catch any remaining RPC or other errors.

                except Exception as e: # This should be at the same indentation level as the previous except
                    logger.critical(f"Failed to authorize user account: {e}", exc_info=True)
                    print(f"Error during user authorization: {e}. Check credentials and network.", file=sys.stderr)
                    await self.client.disconnect()
                    return False
            # --- End User Account Authorization ---

            # Resolve target channel using the configured ID/username
            target_entity = await self._get_channel_entity()
            if not target_entity:
                await self.client.disconnect()
                return False # Cannot proceed without valid channel

            logger.info(f"Successfully resolved target channel '{self.channel_id_config}' to ID: {self.target_channel_id}")

            # Add event handlers - use the resolved numeric ID if possible
            # Use `chats` argument to filter events only for the target channel
            self.client.add_event_handler(
                self.message_handler,
                events.NewMessage(chats=[self.target_channel_id]) # Restore chat filter
            )
            self.client.add_event_handler(
                self.message_handler,
                events.MessageEdited(chats=[self.target_channel_id]) # Restore chat filter
            )
            # Note: Handling replies might require broader event scope or checking event.reply_to_msg_id
            # For now, rely on the handler checking event type and reply status if needed.

            logger.info(f"Listening for messages and edits in channel ID: {self.target_channel_id}...")
            print(f"Telegram Reader (User Account) started. Listening to channel ID: {self.target_channel_id}")
            # Keep the client running until disconnected externally
            # await self.client.run_until_disconnected()
            # Instead of blocking here, let the main loop manage the lifecycle
            return True

        except FloodWaitError as fwe:
             logger.error(f"Flood wait during connection/startup: {fwe.seconds}s")
             print(f"Telegram flood wait: {fwe.seconds}s. Please wait and restart.", file=sys.stderr)
             if self.client and self.client.is_connected():
                 await self.client.disconnect()
             return False
        except ConnectionError as ce:
             logger.critical(f"Telegram connection error: {ce}", exc_info=True)
             print(f"CRITICAL: Failed to connect to Telegram. Check network and API keys.", file=sys.stderr)
             return False
        except Exception as e:
            logger.critical(f"Failed to start Telegram Reader: {e}", exc_info=True)
            if self.client and self.client.is_connected():
                await self.client.disconnect()
            return False

    async def stop(self):
        """Disconnects the Telegram client."""
        if self.client and self.client.is_connected():
            logger.info("Disconnecting Telegram Reader client...")
            await self.client.disconnect()
            logger.info("Telegram Reader client disconnected.")
        else:
            logger.info("Telegram Reader client already disconnected or not initialized.")
# Removed send_message method - this class only reads.

# Example usage (optional, for testing)
if __name__ == '__main__':
    import configparser
    import os
    from datetime import datetime # Import needed for test handler
    from logger_setup import setup_logging

    # Setup basic logging for test
    test_log_path = os.path.join(os.path.dirname(__file__), '..', 'logs', 'telegram_test.log')
    # setup_logging(log_file_path=test_log_path, log_level_str='DEBUG') # Logging setup moved to main app

    # Load dummy config
    example_config_path = os.path.join(os.path.dirname(__file__), '..', 'config', 'config.example.ini')
    if not os.path.exists(example_config_path):
        print(f"ERROR: config.example.ini not found at {example_config_path}. Cannot run test.")
        sys.exit(1)

    config = configparser.ConfigParser()
    config.read(example_config_path)
    # --- IMPORTANT: Fill in REAL Telegram API details and a valid channel_id in config.example.ini ---

    # Check for user API credentials now
    if 'YOUR_' in config.get('Telegram', 'api_id', fallback='') or 'YOUR_' in config.get('Telegram', 'api_hash', fallback=''):
         print("WARNING: Dummy Telegram API ID/Hash found in config. Reader test will likely require interactive login.")
         # Don't exit, allow interactive login attempt

    async def test_handler(event):
        """A simple callback function for testing."""
        if isinstance(event, events.NewMessage.Event):
            event_type = "New Message"
        elif isinstance(event, events.MessageEdited.Event):
            event_type = "Message Edited"
        else:
            event_type = type(event).__name__

        sender = await event.get_sender()
        sender_name = getattr(sender, 'username', None) or getattr(sender, 'first_name', 'Unknown')
        chat = await event.get_chat()
        chat_name = getattr(chat, 'title', None) or getattr(chat, 'username', 'Unknown Chat')

        print("-" * 20)
        # Need to import datetime for the test handler
        from datetime import datetime
        print(f"[{datetime.now()}] Event Received: {event_type}")
        print(f"  Chat: {chat_name} (ID: {event.chat_id})")
        print(f"  Sender: {sender_name} (ID: {event.sender_id})")
        print(f"  Message ID: {event.id}")
        if hasattr(event, 'text'):
             print(f"  Text: {event.text[:150]}...") # Print snippet
        if event.photo:
             print("  Contains Photo: Yes")
        if event.reply_to_msg_id:
             print(f"  Is Reply To: {event.reply_to_msg_id}")
        print("-" * 20)


    async def main_test():
        monitor = None
        try:
            reader = TelegramReader(config, test_handler)
            success = await reader.start()
            if success:
                print("Reader started successfully. Listening for events... Press Ctrl+C to stop.")
                # Keep running until interrupted
                await reader.client.run_until_disconnected()
            else:
                print("Failed to start reader.")
        except KeyboardInterrupt:
            print("\nStopping reader...")
        except Exception as e:
             print(f"\nAn error occurred: {e}")
        finally:
            if reader:
                await reader.stop()
            print("Reader stopped.")

    # Run the async test function
    try:
        asyncio.run(main_test())
    except RuntimeError as e:
         # Handle cases where asyncio loop might already be running (e.g., in some IDEs)
         if "Cannot run the event loop while another loop is running" in str(e):
              print("Asyncio loop already running. Test might not run correctly in this environment.")
         else:
              raise e