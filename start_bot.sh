#!/bin/bash

# === Configuration ===
LOG_FILE="startup.log" # Log file in the script's directory
PYTHON_APP_DIR="/root/mt5-app/AI-Trader/PassiveSignalTrader"
PYTHON_MODULE="src.main"
MT5_PATH="/root/.wine64/drive_c/Program Files/MetaTrader 5/terminal64.exe"
WINEPREFIX_PATH="/root/.wine64"
WINEARCH_TYPE="win64"
XVFB_DISPLAY=":99"
MT5_LOAD_WAIT_SECONDS=45 # Default wait time, adjust as needed

# === Variables ===
XVFB_PID=""
MT5_PID=""

# === Logging Function ===
log_message() {
    local message="$1"
    echo "$(date '+%Y-%m-%d %H:%M:%S') - ${message}" | tee -a "${LOG_FILE}"
}

# === Cleanup Function ===
cleanup() {
    log_message "Shutdown signal received or script exiting. Cleaning up..."

    # Kill MetaTrader 5
    if [[ -n "$MT5_PID" ]] && ps -p $MT5_PID > /dev/null; then
        log_message "Attempting to kill MetaTrader 5 (PID: $MT5_PID)..."
        kill $MT5_PID
        sleep 2 # Give it a moment to exit gracefully
        # Force kill if still running
        if ps -p $MT5_PID > /dev/null; then
             log_message "Force killing MetaTrader 5 (PID: $MT5_PID)..."
             kill -9 $MT5_PID
        fi
    else
        log_message "Attempting to kill MetaTrader 5 (terminal64.exe) by name..."
        pkill -f "terminal64.exe" || log_message "pkill terminal64.exe failed (maybe not running?)."
    fi
    MT5_PID="" # Clear PID

    # Kill wineserver
    log_message "Attempting to kill wineserver..."
    WINEPREFIX="$WINEPREFIX_PATH" WINEARCH="$WINEARCH_TYPE" wineserver -k || log_message "Wineserver kill command failed (maybe not running?)."
    sleep 2

    # Kill Xvfb
    if [[ -n "$XVFB_PID" ]] && ps -p $XVFB_PID > /dev/null; then
        log_message "Attempting to kill Xvfb (PID: $XVFB_PID)..."
        kill $XVFB_PID
        sleep 1
        if ps -p $XVFB_PID > /dev/null; then
            log_message "Force killing Xvfb (PID: $XVFB_PID)..."
            kill -9 $XVFB_PID
        fi
    else
        log_message "Attempting to kill Xvfb by name..."
        pkill Xvfb || log_message "pkill Xvfb failed (maybe not running?)."
    fi
    XVFB_PID="" # Clear PID

    log_message "Cleanup finished."
}

# === Trap Signals ===
# Call cleanup function on script exit or interruption
trap cleanup SIGINT SIGTERM EXIT

# === Main Script Logic ===
log_message "--- Starting Bot Script ---"

# Initial cleanup of any previous instances
log_message "Attempting to kill previous instances..."
cleanup

# Start Xvfb (Virtual Framebuffer)
log_message "Starting Xvfb on display ${XVFB_DISPLAY}..."
Xvfb ${XVFB_DISPLAY} -screen 0 1024x768x24 &
XVFB_PID=$!
sleep 2 # Give Xvfb time to start

if ! ps -p $XVFB_PID > /dev/null; then
    log_message "ERROR: Xvfb failed to start."
    exit 1
fi
log_message "Xvfb started with PID ${XVFB_PID}."
sleep 3 # Extra wait just in case

# Set environment variables for Wine
export DISPLAY="${XVFB_DISPLAY}"
log_message "DISPLAY set to ${DISPLAY}"
export WINEPREFIX="${WINEPREFIX_PATH}"
log_message "WINEPREFIX set to ${WINEPREFIX}"
export WINEARCH="${WINEARCH_TYPE}"
log_message "WINEARCH set to ${WINEARCH}"

# Launch MetaTrader 5 terminal using Wine
log_message "Launching MetaTrader 5 terminal (${MT5_PATH})..."
WINEPREFIX="$WINEPREFIX_PATH" WINEARCH="$WINEARCH_TYPE" wine "${MT5_PATH}" >/dev/null 2>&1 &
MT5_PID=$!
sleep 0.5 # Small delay before checking PID

if ! ps -p $MT5_PID > /dev/null; then
    log_message "ERROR: Failed to launch MetaTrader 5."
    exit 1
fi
log_message "MetaTrader 5 launched with PID ${MT5_PID} (running in background)."

# Wait for MT5 to load
log_message "Waiting ${MT5_LOAD_WAIT_SECONDS} seconds for MT5 to load..."
sleep ${MT5_LOAD_WAIT_SECONDS}

# Change to the Python application directory
log_message "Changing directory to ${PYTHON_APP_DIR}"
cd "${PYTHON_APP_DIR}" || { log_message "ERROR: Failed to change directory to ${PYTHON_APP_DIR}"; exit 1; }

# Check for required environment variables (Placeholder based on logs)
# Add actual checks here if needed, e.g.:
# if [[ -z "$SOME_VAR" ]]; then log_message "ERROR: SOME_VAR not set"; exit 1; fi
log_message "Required environment variables seem to be set." # Assuming check passed

# Launch the Python application using Wine's Python environment
# NOTE: Ensure 'python' is correctly mapped within your Wine environment,
# or specify the full path like 'wine C:/Python39/python.exe'
log_message "Launching Python application (${PYTHON_MODULE})..."
WINEPREFIX="$WINEPREFIX_PATH" WINEARCH="$WINEARCH_TYPE" wine python -m ${PYTHON_MODULE}
PYTHON_EXIT_CODE=$?

log_message "Python application exited with code ${PYTHON_EXIT_CODE}."

# Cleanup will be triggered automatically by the trap on EXIT

exit ${PYTHON_EXIT_CODE}