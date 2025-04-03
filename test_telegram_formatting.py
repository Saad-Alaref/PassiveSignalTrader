import asyncio
import configparser
import logging
import sys
import os
from telethon import TelegramClient
from telethon.errors import FloodWaitError

# Basic logging setup for the test script
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

async def test_formatting():
    # --- Load Config ---
    config_path = os.path.join(os.path.dirname(__file__), 'config', 'config.ini')
    if not os.path.exists(config_path):
        logger.error(f"Configuration file not found at {config_path}")
        print(f"ERROR: Configuration file not found at {config_path}", file=sys.stderr)
        return

    config = configparser.ConfigParser()
    config.read(config_path)

    try:
        api_id = config.getint('Telegram', 'api_id')
        api_hash = config.get('Telegram', 'api_hash')
        bot_token = config.get('Telegram', 'bot_token')
        channel_id_str = config.get('Telegram', 'channel_id')
        target_channel_id = int(channel_id_str) # Assume it's a valid integer ID
    except (configparser.NoSectionError, configparser.NoOptionError, ValueError) as e:
        logger.error(f"Error reading Telegram configuration: {e}")
        print(f"ERROR: Could not read necessary Telegram settings from {config_path}: {e}", file=sys.stderr)
        return

    if not bot_token or 'YOUR_' in bot_token:
        logger.error("Valid Telegram bot_token not found in configuration.")
        print("ERROR: Valid Telegram bot_token not found in configuration.", file=sys.stderr)
        return

    # Use a unique session name for the test sender
    session_name = f"telegram_format_test_session_{api_id}"
    client = TelegramClient(session_name, api_id, api_hash)

    logger.info(f"Attempting to connect using Bot Token for session: {session_name}")
    sender_client = None
    try:
        # Connect and authorize using bot token
        sender_client = await client.start(bot_token=bot_token)
        if not sender_client:
             raise Exception("Failed to start client with bot token.")
        logger.info("Connected successfully.")

        # --- Define Test Messages ---
        test_messages = [
            {
                "label": "Plain Text (parse_mode=None)",
                "text": "Test 1: Plain text. *Bold* `Code` _Italic_ should NOT be formatted.",
                "mode": None
            },
            {
                "label": "MarkdownV1 (parse_mode='md')",
                "text": "Test 2: MarkdownV1. *Bold* `Code` _Italic_ [Link](https://core.telegram.org/bots/api#markdown-style). Special chars: . + - = | { } ( ) ! should be escaped if needed by sender.",
                "mode": "md"
            },
             {
                "label": "MarkdownV2 (parse_mode='md' - Telethon maps this)",
                "text": "Test 3: MarkdownV2 \\(via 'md'\\)\\. *Bold* `Code` _Italic_ __Underline__ ~Strikethrough~ ||Spoiler|| [Link](https://core\\.telegram\\.org/bots/api#markdownv2\\-style)\\. Special chars: \\. \\+ \\- \\= \\| \\{ \\} \\( \\) \\! need escaping\\.",
                "mode": "md" # Telethon maps 'md' to MarkdownV2 internally
            },
            {
                "label": "HTML (parse_mode='html')",
                "text": "Test 4: HTML. <b>Bold</b> <code>Code</code> <i>Italic</i> <u>Underline</u> <strike>Strikethrough</strike> <span class=\"tg-spoiler\">Spoiler</span> <a href=\"https://core.telegram.org/bots/api#html-style\">Link</a>. Special chars: < > & need escaping.",
                "mode": "html"
            }
        ]

        # --- Send Messages ---
        logger.info(f"Sending test messages to channel ID: {target_channel_id}")
        for msg_data in test_messages:
            logger.info(f"Sending: {msg_data['label']}")
            try:
                await client.send_message(
                    target_channel_id,
                    f"--- {msg_data['label']} ---\n{msg_data['text']}",
                    parse_mode=msg_data['mode']
                )
                await asyncio.sleep(2) # Short delay between messages
            except FloodWaitError as fwe:
                logger.warning(f"Flood wait encountered ({fwe.seconds}s), sleeping...")
                await asyncio.sleep(fwe.seconds + 1)
                # Optionally retry sending the specific message here
            except Exception as send_err:
                logger.error(f"Failed to send message '{msg_data['label']}': {send_err}", exc_info=True)
                # Send a plain text error message instead
                try:
                    await client.send_message(target_channel_id, f"ERROR sending test '{msg_data['label']}'. Check logs.")
                except Exception:
                    pass # Ignore error on error message

        logger.info("Finished sending test messages.")

    except Exception as e:
        logger.critical(f"An error occurred during the test script: {e}", exc_info=True)
        print(f"ERROR: {e}", file=sys.stderr)
    finally:
        if client and client.is_connected():
            logger.info("Disconnecting client...")
            await client.disconnect()
            logger.info("Client disconnected.")

if __name__ == "__main__":
    asyncio.run(test_formatting())
