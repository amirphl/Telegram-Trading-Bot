#!/bin/bash

# Telegram Trading Bot - Systemd Setup Script
# This script sets up the bot as a systemd service

set -e

# Configuration
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_NAME="telegram-trading-bot"
SERVICE_FILE="$PROJECT_DIR/scripts/$SERVICE_NAME.service"
SYSTEMD_DIR="/etc/systemd/system"
USER=$(whoami)

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

# Function to check if running as root
check_root() {
    if [ "$EUID" -eq 0 ]; then
        print_error "This script should not be run as root"
        print_info "Run as regular user, it will use sudo when needed"
        exit 1
    fi
}

# Function to check sudo access
check_sudo() {
    if ! sudo -n true 2>/dev/null; then
        print_warning "This script requires sudo access"
        print_info "You may be prompted for your password"
    fi
}

# Function to setup pyenv detection
setup_pyenv_detection() {
    # Check if pyenv is installed
    if command -v pyenv >/dev/null 2>&1; then
        print_status "Found pyenv, detecting Python installation..."
        
        # Initialize pyenv
        export PYENV_ROOT="$HOME/.pyenv"
        export PATH="$PYENV_ROOT/bin:$PATH"
        
        # Initialize pyenv in current shell
        if command -v pyenv >/dev/null 2>&1; then
            eval "$(pyenv init -)"
            eval "$(pyenv virtualenv-init -)" 2>/dev/null || true
        fi
        
        return 0
    else
        print_warning "pyenv not found, using system Python"
        return 1
    fi
}

# Function to detect Python executable path
detect_python_path() {
    local python_path=""
    local using_pyenv=false
    
    # Setup pyenv if available
    if setup_pyenv_detection; then
        using_pyenv=true
        
        # Check if there's a .python-version file
        if [ -f "$PROJECT_DIR/.python-version" ]; then
            local python_version=$(cat "$PROJECT_DIR/.python-version")
            print_status "Using Python version from .python-version: $python_version"
        fi
    fi
    
    # Try to find Python executable
    for cmd in python3 python; do
        if command -v "$cmd" >/dev/null 2>&1; then
            # Verify it's Python 3
            local version=$($cmd --version 2>&1 | grep -oE '[0-9]+\.[0-9]+' | head -1)
            local major=$(echo "$version" | cut -d'.' -f1)
            
            if [ "$major" = "3" ]; then
                python_path=$(which "$cmd")
                print_status "Using Python: $python_path (version $version)"
                break
            fi
        fi
    done
    
    if [ -z "$python_path" ]; then
        print_error "No suitable Python 3 installation found"
        return 1
    fi
    
    echo "$python_path"
    return 0
}

# Function to detect environment setup
detect_environment() {
    local env_path=""
    local python_path=""
    
    # Detect Python path
    if ! python_path=$(detect_python_path); then
        return 1
    fi
    
    # Check if using virtual environment
    if [ -d "$PROJECT_DIR/venv" ]; then
        local venv_python="$PROJECT_DIR/venv/bin/python"
        if [ -f "$venv_python" ]; then
            python_path="$venv_python"
            env_path="$PROJECT_DIR/venv/bin:/usr/local/bin:/usr/bin:/bin"
            print_status "Using virtual environment: $PROJECT_DIR/venv"
        fi
    else
        # Build PATH with pyenv if available
        if command -v pyenv >/dev/null 2>&1; then
            local pyenv_root="$HOME/.pyenv"
            env_path="$pyenv_root/shims:$pyenv_root/bin:/usr/local/bin:/usr/bin:/bin"
            print_status "Using pyenv environment"
        else
            env_path="/usr/local/bin:/usr/bin:/bin"
            print_status "Using system environment"
        fi
    fi
    
    # Export for use in service file creation
    export DETECTED_PYTHON_PATH="$python_path"
    export DETECTED_ENV_PATH="$env_path"
    
    return 0
}

# Function to create systemd service file
create_service_file() {
    local temp_service="/tmp/$SERVICE_NAME.service"
    
    print_status "Creating systemd service file..."
    
    # Detect environment
    if ! detect_environment; then
        return 1
    fi
    
    local project_path="$PROJECT_DIR"
    local python_path="$DETECTED_PYTHON_PATH"
    local env_path="$DETECTED_ENV_PATH"
    
    print_status "Service configuration:"
    print_info "  User: $USER"
    print_info "  Working Directory: $project_path"
    print_info "  Python Path: $python_path"
    print_info "  Environment PATH: $env_path"
    
    # Create service file with detected configuration
    cat > "$temp_service" << EOF
[Unit]
Description=Telegram Trading Bot
After=network.target
Wants=network.target

[Service]
Type=simple
User=$USER
Group=$USER
WorkingDirectory=$project_path
Environment=PATH=$env_path
ExecStart=$python_path app.py
ExecReload=/bin/kill -HUP \$MAINPID
KillMode=mixed
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

# Security settings
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$project_path

# Resource limits
LimitNOFILE=65536
MemoryMax=1G

[Install]
WantedBy=multi-user.target
EOF
    
    # Copy to systemd directory
    sudo cp "$temp_service" "$SYSTEMD_DIR/$SERVICE_NAME.service"
    sudo chmod 644 "$SYSTEMD_DIR/$SERVICE_NAME.service"
    
    # Clean up temp file
    rm -f "$temp_service"
    
    print_status "Service file created at $SYSTEMD_DIR/$SERVICE_NAME.service"
}

# Function to setup systemd service
setup_service() {
    print_status "Setting up systemd service..."
    
    # Reload systemd
    sudo systemctl daemon-reload
    
    # Enable service
    sudo systemctl enable "$SERVICE_NAME"
    
    print_status "Service enabled and will start automatically on boot"
}

# Function to show service commands
show_commands() {
    echo
    print_status "Systemd service setup completed!"
    echo
    
    if command -v pyenv >/dev/null 2>&1; then
        print_info "pyenv integration configured for systemd service"
        if [ -f "$PROJECT_DIR/.python-version" ]; then
            local py_version=$(cat "$PROJECT_DIR/.python-version")
            print_info "Service will use Python version: $py_version"
        fi
        echo
    fi
    
    print_info "Service management commands:"
    print_info "  Start:   sudo systemctl start $SERVICE_NAME"
    print_info "  Stop:    sudo systemctl stop $SERVICE_NAME"
    print_info "  Restart: sudo systemctl restart $SERVICE_NAME"
    print_info "  Status:  sudo systemctl status $SERVICE_NAME"
    print_info "  Logs:    sudo journalctl -u $SERVICE_NAME -f"
    print_info "  Enable:  sudo systemctl enable $SERVICE_NAME"
    print_info "  Disable: sudo systemctl disable $SERVICE_NAME"
    echo
    print_warning "Make sure to configure your .env file before starting the service!"
    echo
    print_info "To start the service now:"
    print_info "  sudo systemctl start $SERVICE_NAME"
}

# Function to remove systemd service
remove_service() {
    print_warning "Removing systemd service..."
    
    # Stop service if running
    if sudo systemctl is-active --quiet "$SERVICE_NAME"; then
        print_status "Stopping service..."
        sudo systemctl stop "$SERVICE_NAME"
    fi
    
    # Disable service
    if sudo systemctl is-enabled --quiet "$SERVICE_NAME"; then
        print_status "Disabling service..."
        sudo systemctl disable "$SERVICE_NAME"
    fi
    
    # Remove service file
    if [ -f "$SYSTEMD_DIR/$SERVICE_NAME.service" ]; then
        sudo rm -f "$SYSTEMD_DIR/$SERVICE_NAME.service"
        print_status "Service file removed"
    fi
    
    # Reload systemd
    sudo systemctl daemon-reload
    
    print_status "Systemd service removed successfully"
}

# Function to check prerequisites
check_prerequisites() {
    print_status "Checking prerequisites..."
    
    # Check if project directory exists
    if [ ! -d "$PROJECT_DIR" ]; then
        print_error "Project directory not found: $PROJECT_DIR"
        exit 1
    fi
    
    # Check if app.py exists
    if [ ! -f "$PROJECT_DIR/app.py" ]; then
        print_error "app.py not found in project directory"
        exit 1
    fi
    
    # Check Python installation
    if ! detect_python_path >/dev/null; then
        print_error "No suitable Python installation found"
        exit 1
    fi
    
    # Check if virtual environment exists (optional)
    if [ -d "$PROJECT_DIR/venv" ]; then
        print_status "Virtual environment found: $PROJECT_DIR/venv"
    else
        print_warning "No virtual environment found. Service will use system/pyenv Python."
    fi
    
    # Check if .env file exists
    if [ ! -f "$PROJECT_DIR/.env" ]; then
        print_warning ".env file not found. Make sure to create it before starting the service"
    fi
    
    # Check if .python-version exists (for pyenv)
    if [ -f "$PROJECT_DIR/.python-version" ]; then
        local py_version=$(cat "$PROJECT_DIR/.python-version")
        print_status "Found .python-version file: $py_version"
    fi
    
    print_status "Prerequisites check passed"
}

# Main setup function
setup() {
    echo "Telegram Trading Bot - Systemd Setup"
    echo "===================================="
    echo
    
    check_root
    check_sudo
    check_prerequisites
    
    create_service_file
    setup_service
    show_commands
}

# Main execution
main() {
    case "${1:-}" in
        --remove|-r)
            remove_service
            ;;
        --help|-h)
            echo "Usage: $0 [OPTIONS]"
            echo
            echo "Options:"
            echo "  --remove, -r    Remove the systemd service"
            echo "  --help, -h      Show this help message"
            echo
            echo "Default action: Setup systemd service"
            echo
            echo "pyenv Integration:"
            echo "  - Automatically detects pyenv installation"
            echo "  - Uses .python-version file if present"
            echo "  - Configures proper PATH for pyenv shims"
            echo "  - Supports both virtual environments and pyenv"
            ;;
        *)
            setup
            ;;
    esac
}

# Run main function
main "$@" 