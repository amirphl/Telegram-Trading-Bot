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

# Function to create systemd service file
create_service_file() {
    local temp_service="/tmp/$SERVICE_NAME.service"
    
    print_status "Creating systemd service file..."
    
    # Get the current user's home directory
    local user_home=$(eval echo ~$USER)
    local project_path="$PROJECT_DIR"
    
    # Create service file with correct paths
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
Environment=PATH=$project_path/venv/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=$project_path/venv/bin/python app.py
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
    
    # Check if virtual environment exists
    if [ ! -d "$PROJECT_DIR/venv" ]; then
        print_error "Virtual environment not found. Run ./scripts/install.sh first"
        exit 1
    fi
    
    # Check if app.py exists
    if [ ! -f "$PROJECT_DIR/app.py" ]; then
        print_error "app.py not found in project directory"
        exit 1
    fi
    
    # Check if .env file exists
    if [ ! -f "$PROJECT_DIR/.env" ]; then
        print_warning ".env file not found. Make sure to create it before starting the service"
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
            ;;
        *)
            setup
            ;;
    esac
}

# Run main function
main "$@" 