#!/bin/bash

# Telegram Trading Bot - Status Script
# This script checks the bot status and shows relevant information

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

# Function to get process info
get_process_info() {
    local pid=$(cat "$PID_FILE")
    
    # Get process start time and CPU/memory usage
    if command -v ps >/dev/null 2>&1; then
        echo "Process Information:"
        ps -p "$pid" -o pid,ppid,user,start,etime,pcpu,pmem,comm 2>/dev/null || echo "  Could not retrieve process details"
        echo
    fi
}

# Function to show log tail
show_recent_logs() {
    echo "Recent Log Entries (last 10 lines):"
    echo "===================================="
    
    if [ -f "$LOG_FILE" ]; then
        tail -n 10 "$LOG_FILE" 2>/dev/null || echo "Could not read log file"
    else
        echo "Log file not found: $LOG_FILE"
    fi
    
    echo
    
    # Show recent errors if any
    if [ -f "$ERROR_LOG" ] && [ -s "$ERROR_LOG" ]; then
        echo "Recent Errors (last 5 lines):"
        echo "=============================="
        tail -n 5 "$ERROR_LOG" 2>/dev/null || echo "Could not read error log"
        echo
    fi
}

# Function to show disk usage
show_disk_usage() {
    echo "Disk Usage:"
    echo "==========="
    
    # Database size
    if [ -f "$PROJECT_DIR/tg_channel.db" ]; then
        local db_size=$(du -h "$PROJECT_DIR/tg_channel.db" 2>/dev/null | cut -f1)
        echo "  Database: $db_size"
    fi
    
    # Log directory size
    if [ -d "$PROJECT_DIR/output/logs" ]; then
        local log_size=$(du -sh "$PROJECT_DIR/output/logs" 2>/dev/null | cut -f1)
        echo "  Logs: $log_size"
    fi
    
    # Media directory size
    if [ -d "$PROJECT_DIR/output/media" ]; then
        local media_size=$(du -sh "$PROJECT_DIR/output/media" 2>/dev/null | cut -f1)
        echo "  Media: $media_size"
    fi
    
    echo
}

# Function to check configuration
check_config() {
    echo "Configuration Check:"
    echo "==================="
    
    # Check .env file
    if [ -f "$PROJECT_DIR/.env" ]; then
        print_status ".env file exists"
    else
        print_error ".env file missing"
    fi
    
    # Check required directories
    local dirs=("output/logs" "output/media")
    for dir in "${dirs[@]}"; do
        if [ -d "$PROJECT_DIR/$dir" ]; then
            print_status "Directory $dir exists"
        else
            print_warning "Directory $dir missing"
        fi
    done
    
    echo
}

# Main status function
show_status() {
    echo "Telegram Trading Bot - Status"
    echo "============================"
    echo
    
    if is_running; then
        local pid=$(cat "$PID_FILE")
        print_status "Bot is RUNNING with PID $pid"
        echo
        
        get_process_info
        
        # Show uptime
        if [ -f "$PID_FILE" ]; then
            local start_time=$(stat -c %Y "$PID_FILE" 2>/dev/null || echo "unknown")
            if [ "$start_time" != "unknown" ]; then
                local current_time=$(date +%s)
                local uptime=$((current_time - start_time))
                local uptime_formatted=$(date -u -d @$uptime +"%H:%M:%S" 2>/dev/null || echo "${uptime}s")
                echo "Uptime: $uptime_formatted"
                echo
            fi
        fi
        
    else
        print_error "Bot is NOT RUNNING"
        echo
        
        # Check if PID file exists but process is dead
        if [ -f "$PID_FILE" ]; then
            print_warning "Stale PID file found and removed"
        fi
    fi
    
    check_config
    show_disk_usage
    
    # Show logs if requested
    case "${1:-}" in
        --logs|-l)
            show_recent_logs
            ;;
        --verbose|-v)
            show_recent_logs
            ;;
    esac
}

# Function to monitor logs in real-time
monitor_logs() {
    echo "Monitoring bot logs (Ctrl+C to exit)..."
    echo "======================================="
    
    if [ -f "$LOG_FILE" ]; then
        tail -f "$LOG_FILE"
    else
        print_error "Log file not found: $LOG_FILE"
        exit 1
    fi
}

# Main execution
main() {
    case "${1:-}" in
        --monitor|-m)
            monitor_logs
            ;;
        *)
            show_status "$@"
            ;;
    esac
}

# Run main function
main "$@" 