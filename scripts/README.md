# Telegram Trading Bot - Server Scripts

This directory contains scripts for managing the Telegram Trading Bot on a server with full **pyenv integration**.

## 🐍 pyenv Integration

All scripts automatically detect and work with pyenv:

- **Automatic Detection**: Scripts detect if pyenv is installed and initialize it
- **Version Management**: Uses `.python-version` file for project-specific Python versions
- **Virtual Environment Support**: Works with both pyenv and traditional virtual environments
- **Fallback Support**: Falls back to system Python if pyenv is not available

## 📁 Available Scripts

### Core Management Scripts

| Script | Purpose | pyenv Features |
|--------|---------|----------------|
| `install.sh` | Setup environment and dependencies | Detects pyenv, creates `.python-version`, uses `pyenv exec pip` |
| `start.sh` | Start bot in background | Auto-detects Python via pyenv, shows version info |
| `stop.sh` | Stop bot gracefully | N/A |
| `restart.sh` | Restart bot | Combines stop + start with pyenv detection |
| `status.sh` | Show bot status and info | Shows pyenv versions, virtual env status |
| `setup-systemd.sh` | Setup systemd service | Configures service with pyenv paths and shims |

## 🚀 Quick Start with pyenv

### 1. Install Python with pyenv (if needed)
```bash
# Install specific Python version
pyenv install 3.11.0

# Set global version
pyenv global 3.11.0

# Or set local version for this project
cd /path/to/telegram-trading-bot
pyenv local 3.11.0
```

### 2. Install and Setup
```bash
# Run installation script (detects pyenv automatically)
./scripts/install.sh

# Configure your settings
cp .env.example .env
nano .env
```

### 3. Start the Bot
```bash
# Start with automatic pyenv detection
./scripts/start.sh

# Check status (shows pyenv info)
./scripts/status.sh
```

## 🔧 pyenv Detection Features

### Automatic Environment Setup
- Initializes pyenv if found: `eval "$(pyenv init -)"`
- Sets up virtualenv integration: `eval "$(pyenv virtualenv-init -)"`
- Uses `PYENV_ROOT` and updates `PATH` automatically

### Python Version Management
- Reads `.python-version` file for project-specific versions
- Shows current pyenv version in status output
- Falls back to system Python if pyenv not available

### Package Management
- Prefers `pyenv exec pip` when pyenv is detected
- Falls back to system `pip3`/`pip` if needed
- Automatically uses correct pip for the active Python version

### Systemd Integration
- Configures systemd service with pyenv shims in PATH
- Uses correct Python executable path for the service
- Supports both virtual environments and pyenv

## 📋 Script Usage Examples

### Installation with pyenv
```bash
# Install dependencies using pyenv
./scripts/install.sh

# Output shows:
# [INFO] Found pyenv, initializing...
# [INFO] Current pyenv Python version: 3.11.0
# [INFO] Using Python command: python3 (version 3.11.0)
# [INFO] Using pip command: pyenv exec pip
```

### Status with pyenv Information
```bash
./scripts/status.sh

# Shows Python environment details:
# Python Environment:
# ==================
# [INFO] pyenv detected
#   Current pyenv version: 3.11.0
#   Project Python version: 3.11.0
#   Available versions: 3.9.0 3.10.0 3.11.0 system
#   python3: /home/user/.pyenv/shims/python3 (version 3.11.0)
#   pip: pyenv exec pip (version 23.0.1)
```

### Systemd Service with pyenv
```bash
./scripts/setup-systemd.sh

# Automatically configures:
# - Python path: /home/user/.pyenv/shims/python3
# - Environment PATH: /home/user/.pyenv/shims:/home/user/.pyenv/bin:...
# - Uses .python-version if present
```

## 🔄 Migration from System Python

If you're migrating from system Python to pyenv:

1. **Install pyenv and Python version**:
   ```bash
   pyenv install 3.11.0
   pyenv local 3.11.0
   ```

2. **Reinstall dependencies**:
   ```bash
   pyenv exec pip install -r requirements.txt
   ```

3. **Restart services**:
   ```bash
   ./scripts/restart.sh
   # or for systemd:
   sudo systemctl restart telegram-trading-bot
   ```

## 🛠️ Troubleshooting pyenv Issues

### pyenv Not Detected
```bash
# Check if pyenv is in PATH
which pyenv

# Add to shell profile if needed
echo 'export PATH="$HOME/.pyenv/bin:$PATH"' >> ~/.bashrc
echo 'eval "$(pyenv init -)"' >> ~/.bashrc
echo 'eval "$(pyenv virtualenv-init -)"' >> ~/.bashrc
```

### Wrong Python Version
```bash
# Check current version
pyenv version

# Set project version
pyenv local 3.11.0

# Verify .python-version file
cat .python-version
```

### pip Issues
```bash
# Use pyenv exec explicitly
pyenv exec pip install -r requirements.txt

# Or rehash shims
pyenv rehash
```

### Systemd Service Issues
```bash
# Check service configuration
sudo systemctl cat telegram-trading-bot

# Restart systemd service
sudo systemctl daemon-reload
sudo systemctl restart telegram-trading-bot
```

## 🔍 Environment Detection Logic

The scripts use this detection order:

1. **Check for pyenv**: `command -v pyenv`
2. **Initialize pyenv**: Set `PYENV_ROOT`, update `PATH`, run `pyenv init`
3. **Detect Python**: Try `python3`, then `python`, verify version 3.x
4. **Detect pip**: Try `pyenv exec pip`, then `pip3`, then `pip`
5. **Check virtual env**: Look for `venv/` directory
6. **Configure paths**: Set appropriate `PATH` for systemd service

## 📝 Configuration Files

### `.python-version`
Created automatically by install script when using pyenv:
```
3.11.0
```

### Systemd Service Environment
Automatically configured with pyenv paths:
```ini
Environment=PATH=/home/user/.pyenv/shims:/home/user/.pyenv/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/home/user/.pyenv/shims/python3 app.py
```

## 🎯 Best Practices

1. **Use project-local Python versions**: `pyenv local 3.11.0`
2. **Keep `.python-version` in git**: Ensures consistent Python across environments
3. **Use virtual environments**: Even with pyenv, virtual environments provide isolation
4. **Test after Python version changes**: Run `./scripts/status.sh` to verify setup
5. **Monitor systemd logs**: `sudo journalctl -u telegram-trading-bot -f`

This pyenv integration makes Python version management seamless while maintaining compatibility with traditional setups! 