import logging
import os
import sys
from logging.handlers import RotatingFileHandler

def setup_logging(log_file_path='logs/bot.log', log_level_str='INFO'):
    """
    Configures logging for the application.

    Sets up logging to both console and a rotating file.

    Args:
        log_file_path (str): The path to the log file.
        log_level_str (str): The logging level (e.g., 'INFO', 'DEBUG').
    """
    log_level = getattr(logging, log_level_str.upper(), logging.INFO)

    # Ensure log directory exists
    log_dir = os.path.dirname(log_file_path)
    if log_dir and not os.path.exists(log_dir):
        try:
            os.makedirs(log_dir)
        except OSError as e:
            print(f"Error creating log directory {log_dir}: {e}", file=sys.stderr)
            # Fallback to current directory if creation fails
            log_file_path = os.path.basename(log_file_path)
            if not log_file_path: # Handle edge case where only dir was given
                log_file_path = 'bot.log'
            print(f"Logging to fallback file: {log_file_path}", file=sys.stderr)


    # Create logger
    logger = logging.getLogger('TradeBot')
    logger.setLevel(log_level)

    # Prevent adding multiple handlers if called again
    if logger.hasHandlers():
        logger.handlers.clear()

    # Formatter
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s'
    )

    # Console Handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # File Handler (Rotating)
    try:
        # Rotate logs: 5 files, 5MB each
        file_handler = RotatingFileHandler(
            log_file_path, maxBytes=5*1024*1024, backupCount=5, encoding='utf-8'
        )
        file_handler.setLevel(log_level)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except Exception as e:
        logger.error(f"Failed to set up file logging to {log_file_path}: {e}", exc_info=True)
        print(f"Error setting up file logging to {log_file_path}: {e}", file=sys.stderr)

    logger.info(f"Logging setup complete. Level: {log_level_str}, File: {log_file_path}")

# Example usage (optional, for testing)
if __name__ == '__main__':
    # Example of setting up logging directly if this script is run
    # In the main app, you'd import setup_logging and call it
    test_log_path = os.path.join(os.path.dirname(__file__), '..', 'logs', 'test_bot.log')
    setup_logging(log_file_path=test_log_path, log_level_str='DEBUG')
    logging.getLogger('TradeBot').debug("Logger test: DEBUG message.")
    logging.getLogger('TradeBot').info("Logger test: INFO message.")
    logging.getLogger('TradeBot').warning("Logger test: WARNING message.")
    logging.getLogger('TradeBot').error("Logger test: ERROR message.")
    logging.getLogger('TradeBot').critical("Logger test: CRITICAL message.")
    print(f"Test log written to: {test_log_path}")