import configparser
import logging
import os

logger = logging.getLogger('TradeBot')

class ConfigService:
    """Handles loading and accessing configuration settings."""

    def __init__(self, config_file='config/config.ini'):
        """
        Initializes the ConfigService by loading the configuration file.

        Args:
            config_file (str): Path to the configuration file.
        """
        self.config_file = config_file
        self.config = configparser.ConfigParser()
        self._load_config()

    def _load_config(self):
        """Loads the configuration from the specified file."""
        if not os.path.exists(self.config_file):
            logger.error(f"Configuration file not found: {self.config_file}")
            raise FileNotFoundError(f"Configuration file not found: {self.config_file}")

        try:
            self.config.read(self.config_file)
            logger.info(f"Configuration loaded successfully from {self.config_file}")
        except configparser.Error as e:
            logger.error(f"Error reading configuration file {self.config_file}: {e}")
            raise

    def get(self, section, option, fallback=None):
        """Gets a configuration value as a string."""
        return self.config.get(section, option, fallback=fallback)

    def getint(self, section, option, fallback=None):
        """Gets a configuration value as an integer."""
        try:
            return self.config.getint(section, option, fallback=fallback)
        except (ValueError, TypeError) as e:
            logger.warning(f"Config Error: Could not parse [{section}]{option} as int. Using fallback '{fallback}'. Error: {e}")
            # Ensure fallback is returned if it exists, otherwise re-raise or return None
            if fallback is not None:
                return fallback
            else:
                 # Or raise a more specific error if no fallback is acceptable
                 raise ValueError(f"Invalid integer value for [{section}]{option} and no fallback provided.") from e


    def getfloat(self, section, option, fallback=None):
        """Gets a configuration value as a float."""
        try:
            return self.config.getfloat(section, option, fallback=fallback)
        except (ValueError, TypeError) as e:
            logger.warning(f"Config Error: Could not parse [{section}]{option} as float. Using fallback '{fallback}'. Error: {e}")
            if fallback is not None:
                return fallback
            else:
                 raise ValueError(f"Invalid float value for [{section}]{option} and no fallback provided.") from e

    def getboolean(self, section, option, fallback=None):
        """Gets a configuration value as a boolean."""
        try:
            # Handle potential None fallback explicitly for getboolean
            if fallback is None:
                 return self.config.getboolean(section, option)
            else:
                 return self.config.getboolean(section, option, fallback=fallback)
        except (ValueError, TypeError) as e:
            logger.warning(f"Config Error: Could not parse [{section}]{option} as boolean. Using fallback '{fallback}'. Error: {e}")
            if fallback is not None:
                return fallback
            else:
                 raise ValueError(f"Invalid boolean value for [{section}]{option} and no fallback provided.") from e

    def reload_config(self):
        """Reloads the configuration from the file."""
        logger.info("Reloading configuration...")
        self._load_config()

# --- Singleton Instance ---
# Load the configuration immediately when the module is imported.
# Components can then import and use this instance directly.
try:
    config_service = ConfigService()
except Exception as e:
    logger.critical(f"Failed to initialize ConfigService: {e}", exc_info=True)
    # Depending on application structure, might want to exit or handle differently
    config_service = None # Ensure it's None if initialization fails