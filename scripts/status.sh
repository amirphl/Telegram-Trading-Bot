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

# Function to setup pyenv if available
setup_pyenv() {
    # Check if pyenv is installed
    if command -v pyenv >/dev/null 2>&1; then
        # Initialize pyenv
        export PYENV_ROOT="$HOME/.pyenv"
        export PATH="$PYENV_ROOT/bin:$PATH"
        
        # Initialize pyenv in current shell
        if command -v pyenv >/dev/null 2>&1; then
            eval "$(pyenv init -)" 2>/dev/null || true
            eval "$(pyenv virtualenv-init -)" 2>/dev/null || true
        fi
        
        return 0
    else
        return 1
    fi
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

# Function to show Python environment info
show_python_info() {
    echo "Python Environment:"
    echo "=================="
    
    # Setup pyenv if available
    local using_pyenv=false
    if setup_pyenv; then
        using_pyenv=true
        print_status "pyenv detected"
        
        # Show current pyenv version
        local current_version=$(pyenv version-name 2>/dev/null || echo "system")
        echo "  Current pyenv version: $current_version"
        
        # Show project-specific version if .python-version exists
        if [ -f "$PROJECT_DIR/.python-version" ]; then
            local project_version=$(cat "$PROJECT_DIR/.python-version")
            echo "  Project Python version: $project_version"
        fi
        
        # Show available versions
        local versions=$(pyenv versions --bare 2>/dev/null | head -5 | tr '\n' ' ')
        if [ -n "$versions" ]; then
            echo "  Available versions: $versions"
        fi
    else
        print_info "Using system Python (pyenv not found)"
    fi
    
    # Show Python executable info
    for cmd in python3 python; do
        if command -v "$cmd" >/dev/null 2>&1; then
            local python_path=$(which "$cmd" 2>/dev/null)
            local python_version=$($cmd --version 2>&1 | cut -d' ' -f2)
            echo "  $cmd: $python_path (version $python_version)"
            break
        fi
    done
    
    # Show pip info
    local pip_cmd=""
    if [ "$using_pyenv" = true ] && pyenv exec pip --version >/dev/null 2>&1; then
        pip_cmd="pyenv exec pip"
    elif command -v pip3 >/dev/null 2>&1; then
        pip_cmd="pip3"
    elif command -v pip >/dev/null 2>&1; then
        pip_cmd="pip"
    fi
    
    if [ -n "$pip_cmd" ]; then
        local pip_version=$($pip_cmd --version 2>&1 | cut -d' ' -f2)
        echo "  pip: $pip_cmd (version $pip_version)"
    fi
    
    # Check virtual environment
    if [ -d "$PROJECT_DIR/venv" ]; then
        local venv_python="$PROJECT_DIR/venv/bin/python"
        if [ -f "$venv_python" ]; then
            local venv_version=$($venv_python --version 2>&1 | cut -d' ' -f2)
            print_status "Virtual environment: $PROJECT_DIR/venv (Python $venv_version)"
        fi
    else
        print_warning "No virtual environment found"
    fi
    
    echo
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
    
    # Check .python-version file
    if [ -f "$PROJECT_DIR/.python-version" ]; then
        local py_version=$(cat "$PROJECT_DIR/.python-version")
        print_status ".python-version file exists: $py_version"
    else
        print_info "No .python-version file (using system/global Python)"
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
    
    show_python_info
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