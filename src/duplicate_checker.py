import logging
from collections import deque

logger = logging.getLogger('TradeBot')

class DuplicateChecker:
    """
    Checks for and records processed Telegram message IDs to prevent duplicates.
    Uses a simple in-memory set with a maximum size to prevent unbounded growth.
    """

    def __init__(self, max_size=10000):
        """
        Initializes the DuplicateChecker.

        Args:
            max_size (int): The maximum number of message IDs to keep in memory.
                            Older IDs will be discarded when the limit is reached.
        """
        self.processed_ids = set()
        # Use a deque to keep track of insertion order for eviction
        self.id_order_queue = deque(maxlen=max_size)
        self.max_size = max_size
        logger.info(f"DuplicateChecker initialized with max size: {max_size}")

    def is_duplicate(self, message_id):
        """
        Checks if a message ID has already been processed.

        Args:
            message_id (int): The Telegram message ID to check.

        Returns:
            bool: True if the message ID has been processed, False otherwise.
        """
        if message_id in self.processed_ids:
            logger.debug(f"Duplicate message ID detected: {message_id}")
            return True
        return False

    def add_processed_id(self, message_id):
        """
        Adds a message ID to the set of processed IDs.
        Handles eviction if max_size is reached.

        Args:
            message_id (int): The Telegram message ID to add.
        """
        if message_id not in self.processed_ids:
            # Check if we need to evict the oldest ID
            if len(self.processed_ids) >= self.max_size:
                try:
                    oldest_id = self.id_order_queue.popleft() # Remove oldest from queue
                    self.processed_ids.remove(oldest_id) # Remove oldest from set
                    logger.debug(f"Evicted oldest message ID {oldest_id} due to max size limit.")
                except KeyError:
                    # Should not happen if queue and set are in sync, but handle defensively
                    logger.warning(f"Attempted to evict ID {oldest_id} from set, but it was not found.")
                except IndexError:
                     # Should not happen if len >= max_size, but handle defensively
                     logger.warning("Attempted to pop from empty deque during eviction.")


            self.processed_ids.add(message_id)
            self.id_order_queue.append(message_id) # Add new ID to queue
            logger.debug(f"Added message ID {message_id} to processed set. Current size: {len(self.processed_ids)}")
        else:
            # This case should ideally not happen if is_duplicate is checked first,
            # but log it if it does.
            logger.warning(f"Attempted to add already existing message ID {message_id} to processed set.")

    def get_processed_count(self):
        """Returns the current number of processed IDs being tracked."""
        return len(self.processed_ids)

# Example usage (optional, for testing)
if __name__ == '__main__':
    import os
    from logger_setup import setup_logging

    # Setup basic logging for test
    test_log_path = os.path.join(os.path.dirname(__file__), '..', 'logs', 'duplicate_checker_test.log')
    setup_logging(log_file_path=test_log_path, log_level_str='DEBUG')

    checker = DuplicateChecker(max_size=5) # Small size for testing eviction

    test_ids = [101, 102, 103, 104, 105, 106, 102, 107]

    print("Testing Duplicate Checker...")
    for msg_id in test_ids:
        print(f"\nChecking ID: {msg_id}")
        if checker.is_duplicate(msg_id):
            print(f"  Result: Duplicate detected.")
        else:
            print(f"  Result: Not a duplicate.")
            checker.add_processed_id(msg_id)
            print(f"  Added ID. Processed count: {checker.get_processed_count()}")
            print(f"  Current Set: {checker.processed_ids}")
            print(f"  Current Queue: {list(checker.id_order_queue)}") # Show queue order

    print("\nFinal State:")
    print(f"  Processed count: {checker.get_processed_count()}")
    print(f"  Processed Set: {checker.processed_ids}")
    print(f"  ID Queue: {list(checker.id_order_queue)}")

    # Expect: 101 evicted, 102 detected as duplicate, final set {103, 104, 105, 106, 107}
    print("\nTest finished.")