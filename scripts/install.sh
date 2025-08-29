#!/bin/bash

# Telegram Trading Bot - Installation Script
# This script sets up the environment and installs dependencies

set -e

# Configuration
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_CMD="python3"
PIP_CMD="pip3"

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

# Function to check if command exists
command_exists() {
    command -v "$1" >/dev/null 2>&1
}

# Function to check Python version
check_python() {
    print_status "Checking Python installation..."
    
    if ! command_exists "$PYTHON_CMD"; then
        print_error "Python 3 is not installed. Please install Python 3.8 or higher."
        return 1
    fi
    
    local python_version=$($PYTHON_CMD --version 2>&1 | cut -d' ' -f2)
    print_status "Found Python $python_version"
    
    # Check if version is 3.8 or higher
    local major_version=$(echo "$python_version" | cut -d'.' -f1)
    local minor_version=$(echo "$python_version" | cut -d'.' -f2)
    
    if [ "$major_version" -lt 3 ] || ([ "$major_version" -eq 3 ] && [ "$minor_version" -lt 8 ]); then
        print_error "Python 3.8 or higher is required. Found: $python_version"
        return 1
    fi
    
    return 0
}

# Function to check pip
check_pip() {
    print_status "Checking pip installation..."
    
    if ! command_exists "$PIP_CMD"; then
        print_error "pip3 is not installed. Please install pip3."
        return 1
    fi
    
    local pip_version=$($PIP_CMD --version 2>&1 | cut -d' ' -f2)
    print_status "Found pip $pip_version"
    
    return 0
}

# Function to create virtual environment
create_venv() {
    local venv_dir="$PROJECT_DIR/venv"
    
    if [ -d "$venv_dir" ]; then
        print_warning "Virtual environment already exists at $venv_dir"
        read -p "Do you want to recreate it? (y/N): " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            print_status "Removing existing virtual environment..."
            rm -rf "$venv_dir"
        else
            print_status "Using existing virtual environment"
            return 0
        fi
    fi
    
    print_status "Creating virtual environment..."
    $PYTHON_CMD -m venv "$venv_dir"
    
    print_status "Virtual environment created at $venv_dir"
    print_info "To activate: source venv/bin/activate"
    
    return 0
}

# Function to install dependencies
install_dependencies() {
    print_status "Installing Python dependencies..."
    
    cd "$PROJECT_DIR"
    
    # Check if requirements.txt exists
    if [ ! -f "requirements.txt" ]; then
        print_error "requirements.txt not found in project directory"
        return 1
    fi
    
    # Install dependencies
    $PIP_CMD install -r requirements.txt
    
    print_status "Dependencies installed successfully"
    return 0
}

# Function to create directories
create_directories() {
    print_status "Creating required directories..."
    
    local dirs=(
        "output/logs"
        "output/media" 
        "configs"
    )
    
    for dir in "${dirs[@]}"; do
        local full_path="$PROJECT_DIR/$dir"
        if [ ! -d "$full_path" ]; then
            mkdir -p "$full_path"
            print_status "Created directory: $dir"
        else
            print_info "Directory already exists: $dir"
        fi
    done
    
    return 0
}

# Function to create .env template
create_env_template() {
    local env_file="$PROJECT_DIR/.env"
    local env_example="$PROJECT_DIR/.env.example"
    
    if [ -f "$env_file" ]; then
        print_warning ".env file already exists"
        return 0
    fi
    
    print_status "Creating .env template..."
    
    cat > "$env_example" << 'EOF'
# Telegram API Configuration
API_ID=your_telegram_api_id
API_HASH=your_telegram_api_hash
SESSION_NAME=tg_session

# Exchange Selection (xt, bitunix, or lbank)
EXCHANGE=xt

# Proxy Configuration (Optional)
PROXY_TYPE=SOCKS5
PROXY_HOST=127.0.0.1
PROXY_PORT=1080
PROXY_USERNAME=
PROXY_PASSWORD=

# Channel Configuration (Legacy - single channel)
CHANNEL_ID=@your_channel
CHANNEL_TITLE=Your Channel Name
CHANNEL_PROMPT=

# Multi-Channel Configuration (Recommended)
# CHANNELS_CONFIG=[{"channel_id":"@channel1","channel_title":"Channel 1","policy":"single_message","enabled":true}]
# CHANNELS_FILE=./configs/channels.json

# OpenAI Configuration
OPENAI_API_KEY=your_openai_api_key
OPENAI_MODEL=gpt-4.1-mini
OPENAI_TIMEOUT_SECS=299
OPENAI_BASE_URL=

# Image Upload Service
UPLOAD_BASE=http://localhost:8080

# XT Exchange (Futures)
XT_API_KEY=
XT_SECRET=
XT_PASSWORD=
XT_MARGIN_MODE=cross

# Bitunix Exchange (Futures)
BITUNIX_API_KEY=
BITUNIX_SECRET=
BITUNIX_BASE_URL=https://fapi.bitunix.com
BITUNIX_LANGUAGE=en-US

# LBank Exchange (Legacy)
LBANK_API_KEY=
LBANK_SECRET=
LBANK_PASSWORD=

# Trading Configuration
ORDER_QUOTE=USDT
ORDER_NOTIONAL=10
MAX_PRICE_DEVIATION_PCT=0.02
ENABLE_AUTO_EXECUTION=0

# Database and Storage
DB_PATH=./tg_channel.db
MEDIA_DIR=./output/media
BACKFILL=3

# Logging
LOG_LEVEL=INFO
LOG_FILE=./output/logs/bot.log
LOG_BACKUP_COUNT=14

# System Configuration
HEARTBEAT_SECS=180
MAX_BACKOFF_SECS=300
SQL_BUSY_RETRIES=10
SQL_BUSY_SLEEP=0.2
EOF
    
    print_status "Created .env.example template"
    print_warning "Please copy .env.example to .env and configure your settings:"
    print_info "  cp .env.example .env"
    print_info "  nano .env"
    
    return 0
}

# Function to make scripts executable
make_scripts_executable() {
    print_status "Making scripts executable..."
    
    local scripts=(
        "scripts/start.sh"
        "scripts/stop.sh"
        "scripts/restart.sh"
        "scripts/status.sh"
        "scripts/install.sh"
    )
    
    for script in "${scripts[@]}"; do
        local script_path="$PROJECT_DIR/$script"
        if [ -f "$script_path" ]; then
            chmod +x "$script_path"
            print_status "Made executable: $script"
        fi
    done
    
    return 0
}

# Function to show post-install instructions
show_instructions() {
    echo
    print_status "Installation completed successfully!"
    echo
    print_info "Next steps:"
    print_info "1. Configure your settings:"
    print_info "   cp .env.example .env"
    print_info "   nano .env"
    echo
    print_info "2. Start the bot:"
    print_info "   ./scripts/start.sh"
    echo
    print_info "3. Check status:"
    print_info "   ./scripts/status.sh"
    echo
    print_info "4. Stop the bot:"
    print_info "   ./scripts/stop.sh"
    echo
    print_info "5. View logs:"
    print_info "   ./scripts/status.sh --logs"
    print_info "   ./scripts/status.sh --monitor"
    echo
    print_warning "Important: Configure your .env file before starting the bot!"
}

# Main installation function
install() {
    echo "Telegram Trading Bot - Installation Script"
    echo "========================================="
    echo
    
    cd "$PROJECT_DIR"
    
    # Check system requirements
    if ! check_python; then
        exit 1
    fi
    
    if ! check_pip; then
        exit 1
    fi
    
    # Ask about virtual environment
    read -p "Do you want to create a virtual environment? (Y/n): " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Nn]$ ]]; then
        if ! create_venv; then
            exit 1
        fi
        
        print_warning "Please activate the virtual environment and run this script again:"
        print_info "  source venv/bin/activate"
        print_info "  ./scripts/install.sh"
        exit 0
    fi
    
    # Install dependencies
    if ! install_dependencies; then
        exit 1
    fi
    
    # Create directories
    if ! create_directories; then
        exit 1
    fi
    
    # Create .env template
    if ! create_env_template; then
        exit 1
    fi
    
    # Make scripts executable
    if ! make_scripts_executable; then
        exit 1
    fi
    
    # Show instructions
    show_instructions
}

# Main execution
main() {
    install
}

# Run main function
main "$@" 