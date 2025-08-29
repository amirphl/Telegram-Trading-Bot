#!/bin/bash

# Telegram Trading Bot - Stop Script
# This script stops the bot gracefully

set -e

# Configuration
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="$PROJECT_DIR/bot.pid"

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

# Function to stop the bot
stop_bot() {
    if ! is_running; then
        print_warning "Bot is not running"
        return 1
    fi
    
    local pid=$(cat "$PID_FILE")
    print_status "Stopping bot with PID $pid..."
    
    # Try graceful shutdown first (SIGTERM)
    kill -TERM "$pid" 2>/dev/null || {
        print_error "Failed to send SIGTERM to process $pid"
        return 1
    }
    
    # Wait for graceful shutdown (up to 30 seconds)
    local count=0
    while [ $count -lt 30 ]; do
        if ! ps -p "$pid" > /dev/null 2>&1; then
            print_status "Bot stopped gracefully"
            rm -f "$PID_FILE"
            return 0
        fi
        sleep 1
        count=$((count + 1))
        if [ $((count % 5)) -eq 0 ]; then
            print_status "Waiting for graceful shutdown... ($count/30)"
        fi
    done
    
    # If still running, force kill
    if ps -p "$pid" > /dev/null 2>&1; then
        print_warning "Graceful shutdown timeout, forcing kill..."
        kill -KILL "$pid" 2>/dev/null || {
            print_error "Failed to force kill process $pid"
            return 1
        }
        
        # Wait a bit more
        sleep 2
        
        if ps -p "$pid" > /dev/null 2>&1; then
            print_error "Failed to stop bot process $pid"
            return 1
        else
            print_status "Bot force stopped"
            rm -f "$PID_FILE"
            return 0
        fi
    fi
}

# Function to force stop (immediate kill)
force_stop() {
    if ! is_running; then
        print_warning "Bot is not running"
        return 1
    fi
    
    local pid=$(cat "$PID_FILE")
    print_warning "Force stopping bot with PID $pid..."
    
    kill -KILL "$pid" 2>/dev/null || {
        print_error "Failed to force kill process $pid"
        return 1
    }
    
    sleep 1
    
    if ps -p "$pid" > /dev/null 2>&1; then
        print_error "Failed to force stop bot process $pid"
        return 1
    else
        print_status "Bot force stopped"
        rm -f "$PID_FILE"
        return 0
    fi
}

# Main execution
main() {
    echo "Telegram Trading Bot - Stop Script"
    echo "================================="
    
    case "${1:-}" in
        --force|-f)
            force_stop
            ;;
        *)
            stop_bot
            ;;
    esac
    
    exit $?
}

# Run main function
main "$@" 