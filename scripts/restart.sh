#!/bin/bash

# Telegram Trading Bot - Restart Script
# This script restarts the bot (stop + start)

set -e

# Configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
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

# Main restart function
restart_bot() {
    echo "Telegram Trading Bot - Restart Script"
    echo "===================================="
    
    print_status "Restarting bot..."
    
    # Stop the bot first
    print_status "Step 1: Stopping bot..."
    if "$SCRIPT_DIR/stop.sh"; then
        print_status "Bot stopped successfully"
    else
        print_warning "Stop script returned non-zero exit code, continuing..."
    fi
    
    # Wait a moment
    sleep 2
    
    # Start the bot
    print_status "Step 2: Starting bot..."
    if "$SCRIPT_DIR/start.sh"; then
        print_status "Bot restarted successfully"
        return 0
    else
        print_error "Failed to start bot"
        return 1
    fi
}

# Main execution
main() {
    restart_bot
    exit $?
}

# Run main function
main "$@" 