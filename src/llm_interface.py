import google.generativeai as genai
import logging
import os
import time
import json # Import the missing json module
from PIL import Image # For potential image handling
import io

logger = logging.getLogger('TradeBot')

# Constants for retry logic
MAX_RETRIES = 3
RETRY_DELAY = 5 # seconds

class LLMInterface:
    """Handles communication with the Google Gemini API."""

    def __init__(self, config_service_instance): # Inject service
        """
        Initializes the LLM interface and configures the Gemini client.

        Args:
            config_service_instance (ConfigService): The application config service.
        """
        self.config_service = config_service_instance # Store service instance
        self.api_key = self.config_service.get('Gemini', 'api_key', fallback=None) # Use service
        self.model_name = self.config_service.get('Gemini', 'model_name', fallback='gemini-pro') # Use service
        self.temperature = self.config_service.getfloat('Gemini', 'temperature', fallback=0.2) # Read temperature
        self.use_json_mode = self.config_service.getboolean('Gemini', 'enable_json_mode', fallback=False) # Read JSON mode flag

        if not self.api_key:
            logger.critical("Gemini API key not found in configuration. LLM features will be disabled.")
            self.client = None
            self.model = None
            # Potentially raise an error here if LLM is absolutely critical
            # raise ValueError("Missing Gemini API Key")
        else:
            try:
                genai.configure(api_key=self.api_key)
                # Check if the specified model supports vision for potential image handling
                # This is a basic check; more robust checking might involve listing models
                if 'vision' in self.model_name or 'gemini-1.5' in self.model_name: # Models like gemini-pro-vision or 1.5 support images
                    self.supports_vision = True
                    logger.info(f"Configuring Gemini with vision-capable model: {self.model_name}")
                else:
                    self.supports_vision = False
                    logger.info(f"Configuring Gemini with text-only model: {self.model_name}")

                # Create GenerationConfig
                generation_config_args = {"temperature": self.temperature}
                if self.use_json_mode:
                    # Ensure the prompt explicitly asks for JSON
                    logger.info("Enabling JSON output mode for Gemini.")
                    generation_config_args["response_mime_type"] = "application/json"
                self.generation_config = genai.GenerationConfig(**generation_config_args)

                self.model = genai.GenerativeModel(self.model_name)
                logger.info("Gemini client configured successfully.")
            except Exception as e:
                logger.critical(f"Failed to configure Gemini client: {e}", exc_info=True)
                self.client = None
                self.model = None
                # raise # Optional: re-raise the exception to halt startup

    def _prepare_prompt(self, message_text, context=None, prompt_type="analyze_signal"):
        """
        Prepares the prompt string to send to the LLM based on the type of analysis needed.
        This is where prompt engineering happens.
        """
        import json # Ensure json is imported

        # --- Prepare Context Strings (if available and enabled) ---
        context_str = "\n--- Additional Context ---\n"
        context_added = False

        if context:
            if context.get('current_price'):
                price_info = context['current_price']
                context_str += f"Current Price (XAUUSD): Bid={price_info['bid']}, Ask={price_info['ask']} (as of {price_info['time']})\n"
                context_added = True
            if context.get('active_trades'):
                context_str += "Active Bot Trades:\n"
                for trade_summary in context['active_trades']:
                    context_str += f"- {trade_summary}\n"
                context_added = True
            if context.get('message_history'):
                context_str += "Recent Message History (Oldest first):\n"
                # Iterate through history (which is now a list)
                for msg in context['message_history']:
                    sender = msg.get('sender_id', 'Unknown')
                    text = msg.get('text', '').replace('\n', ' ')[:80] # Truncate/clean
                    ts = msg.get('timestamp', '')
                    edit_flag = " (Edit)" if msg.get('is_edit') else ""
                    # Exclude the current message if it's in the history
                    if msg.get('text') != message_text:
                         context_str += f"- [{ts}] Sender {sender}{edit_flag}: {text}...\n"
                context_added = True

        if not context_added:
            context_str = "" # No context to add

        # --- Get Prompts from Config ---
        base_instructions_template = self.config_service.get('LLMPrompts', 'base_instructions', fallback="ERROR: base_instructions not found in config") # Use service
        analyze_signal_instructions = self.config_service.get('LLMPrompts', 'analyze_signal_instructions', fallback="ERROR: analyze_signal_instructions not found in config")
        analyze_edit_update_instructions = self.config_service.get('LLMPrompts', 'analyze_edit_update_instructions', fallback="ERROR: analyze_edit_update_instructions not found in config") # Prompt for extracting only updated params from edits

        # --- Format Base Prompt ---
        # Use .format() for safe insertion of potentially complex context/message strings
        base_instructions = base_instructions_template.format(context_str=context_str, message_text=message_text)

        if prompt_type == "analyze_signal":
            prompt = base_instructions + "\n" + analyze_signal_instructions
        elif prompt_type == "analyze_edit_update":
             # Instructions specifically for extracting update parameters from an edited signal message
             prompt = base_instructions + "\n" + analyze_edit_update_instructions
        # Removed deprecated analyze_edit_or_reply prompt type
        else:
            logger.warning(f"Unknown prompt type requested: {prompt_type}. Using default analyze_signal.")
            # Fallback to the main analysis prompt
            prompt = base_instructions + "\n" + analyze_signal_instructions

        return prompt.strip()


    def analyze_message(self, message_text, image_data=None, context=None, prompt_type="analyze_signal"):
        """
        Sends message content (and optionally image) to the Gemini API for analysis.

        Args:
            message_text (str): The text content of the Telegram message.
            image_data (bytes, optional): The byte content of an image, if present. Defaults to None.
            context (dict, optional): Additional context (price, history, trades). Defaults to None.
            prompt_type (str): The type of analysis requested ("analyze_signal", "analyze_edit_update").

        Returns:
            dict or None: A dictionary containing the structured analysis results from the LLM,
                          or None if the analysis fails or the client is not configured.
        """
        if not self.model:
            logger.error("Gemini client not initialized. Cannot analyze message.")
            return None

        prompt = self._prepare_prompt(message_text, context, prompt_type) # Pass context
        content_parts = [prompt]

        # Handle image data if the model supports vision and image data is provided
        if self.supports_vision and image_data:
            try:
                img = Image.open(io.BytesIO(image_data))
                # Prepend image part - order might matter depending on model/prompt
                content_parts.insert(0, img)
                logger.debug("Image data added to Gemini request.")
            except Exception as e:
                logger.error(f"Failed to process image data for Gemini: {e}", exc_info=True)
                # Decide whether to proceed without image or fail
                # Proceeding without image for now
                logger.warning("Proceeding with Gemini analysis using text only.")

        retries = 0
        while retries < MAX_RETRIES:
            try:
                logger.debug(f"Sending request to Gemini (Attempt {retries + 1}/{MAX_RETRIES})...")
                # Log the prepared prompt at DEBUG level
                log_prompt = prompt if len(prompt) < 500 else prompt[:500] + "..." # Avoid overly long logs
                logger.debug(f"Gemini Prompt ({prompt_type}):\n{log_prompt}")
                # Use generate_content for potentially mixed text/image input
                response = self.model.generate_content(
                    content_parts,
                    generation_config=self.generation_config # Pass the config here
                )

                # Log raw response text for debugging
                logger.debug(f"Gemini raw response text: {response.text}")

                # Attempt to parse the response text as JSON
                try:
                    # If JSON mode is enabled, response.text should be directly parsable JSON
                    if self.use_json_mode:
                         json_response_str = response.text
                         logger.debug("Attempting to parse JSON directly (JSON mode enabled).")
                    else:
                        # Fallback: Try to extract JSON from markdown if not using JSON mode
                        logger.debug("Attempting to extract JSON from markdown (JSON mode disabled).")
                        json_response_str = response.text.strip()
                        if json_response_str.startswith("```json"):
                            json_response_str = json_response_str[7:]
                        if json_response_str.endswith("```"):
                            json_response_str = json_response_str[:-3]
                        json_response_str = json_response_str.strip()

                    result_dict = json.loads(json_response_str)
                    logger.info(f"Gemini analysis successful. Type: {prompt_type}")
                    # Log the successfully parsed dict at DEBUG level
                    logger.debug(f"Gemini Parsed JSON Result: {result_dict}")
                    return result_dict
                except json.JSONDecodeError as json_err:
                    logger.error(f"Failed to decode Gemini response as JSON: {json_err}")
                    logger.error(f"LLM Raw Response was: {response.text}")
                    # Don't retry on JSON decode error, it's likely a prompt/model issue
                    return None
                except Exception as parse_err:
                     logger.error(f"Unexpected error parsing Gemini JSON response: {parse_err}", exc_info=True)
                     return None


            except Exception as e:
                logger.error(f"Error calling Gemini API (Attempt {retries + 1}/{MAX_RETRIES}): {e}", exc_info=True)
                retries += 1
                if retries < MAX_RETRIES:
                    logger.info(f"Retrying Gemini API call in {RETRY_DELAY} seconds...")
                    time.sleep(RETRY_DELAY)
                else:
                    logger.critical("Max retries reached for Gemini API call. Analysis failed.")
                    return None
        return None # Should not be reached if loop logic is correct

# Example usage (optional, for testing)
if __name__ == '__main__':
    # import configparser # No longer needed
    import os
    import sys
    import json
    from logger_setup import setup_logging
    from config_service import ConfigService # Import service for testing

    # Setup basic logging for test
    test_log_path = os.path.join(os.path.dirname(__file__), '..', 'logs', 'llm_interface_test.log')
    setup_logging(log_file_path=test_log_path, log_level_str='DEBUG')

    # --- IMPORTANT: Ensure config/config.ini exists and has REAL Gemini API Key ---
    try:
        # Instantiate ConfigService directly for the test
        test_config_service = ConfigService(config_file='../config/config.ini') # Adjust path if needed
    except Exception as e:
        print(f"ERROR: Failed to load config/config.ini for testing: {e}")
        sys.exit(1)

    # Check if dummy values might still be present (optional check)
    if 'YOUR_' in test_config_service.get('Gemini', 'api_key', fallback=''):
         print("WARNING: Dummy Gemini API key might be present in config.ini. LLM test may fail.")
         print("Please ensure config/config.ini has a real API key.")

    # Instantiate LLMInterface with the test service instance
    llm_interface = LLMInterface(test_config_service)

    if llm_interface.model:
        print("Testing with a sample signal message...")
        # Example from docs/Message Examples.md
        test_message_signal = """
🧑‍💻XAUUSD Buy gold  Zone 3106 - 3108
🔹SL 3103
🔹TP 3112- 3125- open
"""
        result_signal = llm_interface.analyze_message(test_message_signal, prompt_type="analyze_signal")
        print("--- Signal Analysis Result ---")
        if result_signal:
            print(json.dumps(result_signal, indent=2))
        else:
            print("Analysis failed.")

        print("\nTesting with a sample non-signal message...")
        test_message_non_signal = """
Gold Buy running 400 pips from yesterday’s absolute precision analysis:
 • Identified the strong liquidity zone at 3100
 • Pinpointed the exact entry at 3100
"""
        result_non_signal = llm_interface.analyze_message(test_message_non_signal, prompt_type="analyze_signal")
        print("--- Non-Signal Analysis Result ---")
        if result_non_signal:
            print(json.dumps(result_non_signal, indent=2))
        else:
            print("Analysis failed.")

        print("\nTesting with a sample edit message...")
        test_message_edit = "TP updated to 3115"
        result_edit = llm_interface.analyze_message(test_message_edit, prompt_type="analyze_edit_or_reply")
        print("--- Edit Analysis Result ---")
        if result_edit:
            print(json.dumps(result_edit, indent=2))
        else:
            print("Analysis failed.")

    else:
        print("LLM Interface could not be initialized. Check API key and configuration.")

    print("\nTest finished.")