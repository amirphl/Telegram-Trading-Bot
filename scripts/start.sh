#!/bin/bash

# Telegram Trading Bot - Start Script
# This script starts the bot in the background and manages the process

set -e

# Configuration
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="$PROJECT_DIR/bot.pid"
LOG_FILE="$PROJECT_DIR/output/logs/bot.log"
ERROR_LOG="$PROJECT_DIR/output/logs/bot_error.log"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Function to print colored output
print_status() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

print_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

# Function to setup pyenv if available
setup_pyenv() {
    # Check if pyenv is installed
    if command -v pyenv >/dev/null 2>&1; then
        print_status "Found pyenv, initializing..."
        
        # Initialize pyenv
        export PYENV_ROOT="$HOME/.pyenv"
        export PATH="$PYENV_ROOT/bin:$PATH"
        
        # Initialize pyenv in current shell
        if command -v pyenv >/dev/null 2>&1; then
            eval "$(pyenv init -)"
            eval "$(pyenv virtualenv-init -)" 2>/dev/null || true
        fi
        
        # Check if there's a .python-version file
        if [ -f "$PROJECT_DIR/.python-version" ]; then
            local python_version=$(cat "$PROJECT_DIR/.python-version")
            print_status "Using Python version from .python-version: $python_version"
        fi
        
        return 0
    else
        print_warning "pyenv not found, using system Python"
        return 1
    fi
}

# Function to detect Python command
detect_python() {
    local python_cmd=""
    local python_version=""
    
    # Setup pyenv first
    setup_pyenv
    
    # Try different Python commands in order of preference
    for cmd in python3 python; do
        if command -v "$cmd" >/dev/null 2>&1; then
            # Verify it's Python 3
            local version=$($cmd --version 2>&1 | grep -oE '[0-9]+\.[0-9]+' | head -1)
            local major=$(echo "$version" | cut -d'.' -f1)
            
            if [ "$major" = "3" ]; then
                python_cmd="$cmd"
                python_version="$version"
                break
            fi
        fi
    done
    
    if [ -z "$python_cmd" ]; then
        print_error "No suitable Python 3 installation found"
        return 1
    fi
    
    # Print status after we've determined the command (not in the capture)
    print_status "Using Python command: $python_cmd (version $python_version)"
    
    # Only echo the command name for capture
    echo "$python_cmd"
    return 0
}

# Function to check if bot is running
is_running() {
    if [ -f "$PID_FILE" ]; then
        local pid=$(cat "$PID_FILE")
        if ps -p "$pid" > /dev/null 2>&1; then
            return 0
        else
            # PID file exists but process is dead, remove stale PID file
            rm -f "$PID_FILE"
            return 1
        fi
    fi
    return 1
}

# Function to start the bot
start_bot() {
    cd "$PROJECT_DIR"
    
    # Check if already running
    if is_running; then
        local pid=$(cat "$PID_FILE")
        print_warning "Bot is already running with PID $pid"
        return 1
    fi
    
    # Detect Python command
    local python_cmd
    if ! python_cmd=$(detect_python); then
        return 1
    fi
    
    # Create output directories
    mkdir -p "$(dirname "$LOG_FILE")"
    mkdir -p "$(dirname "$ERROR_LOG")"
    
    # Check if .env file exists
    if [ ! -f "$PROJECT_DIR/.env" ]; then
        print_error ".env file not found. Please create it with your configuration."
        return 1
    fi
    
    # Check Python dependencies
    print_status "Checking Python dependencies..."
    if ! $python_cmd -c "import telethon, ccxt, openai" 2>/dev/null; then
        print_error "Missing Python dependencies. Run: pip install -r requirements.txt"
        print_info "Or if using pyenv: pyenv exec pip install -r requirements.txt"
        return 1
    fi
    
    print_status "Starting Telegram Trading Bot..."
    
    # Start the bot in background
    nohup $python_cmd app.py >> "$LOG_FILE" 2>> "$ERROR_LOG" &
    local pid=$!
    
    # Save PID
    echo $pid > "$PID_FILE"
    
    # Wait a moment to check if process started successfully
    sleep 2
    
    if ps -p "$pid" > /dev/null 2>&1; then
        print_status "Bot started successfully with PID $pid"
        print_status "Python command: $python_cmd"
        print_status "Logs: $LOG_FILE"
        print_status "Error logs: $ERROR_LOG"
        print_status "Use './scripts/stop.sh' to stop the bot"
        print_status "Use './scripts/status.sh' to check status"
        return 0
    else
        print_error "Failed to start bot. Check error log: $ERROR_LOG"
        rm -f "$PID_FILE"
        return 1
    fi
}

# Main execution
main() {
    echo "Telegram Trading Bot - Start Script"
    echo "=================================="
    
    start_bot
    exit $?
}

# Run main function
main "$@" 