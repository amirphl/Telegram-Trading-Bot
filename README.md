# Telegram Trading Bot

A Telegram bot that monitors multiple channels for trading signals and can execute trades automatically on multiple cryptocurrency exchanges.

## Features

- **Multi-Channel Support**: Monitor multiple Telegram channels simultaneously
- **Flexible Signal Discovery Policies**: Configure different signal extraction strategies per channel
- **Multi-Exchange Support**: Trade on XT, Bitunix, or LBank exchanges
- **Futures Trading**: Support for leverage, stop losses, and take profits
- **Message Persistence**: Store all messages and media files locally
- **OpenAI Integration**: Use AI to extract trading signals from text and images
- **Optional Proxy Support**: Configure proxy for exchanges that require it
- **Comprehensive Logging**: Structured logging with file rotation
- **Testing Utilities**: Tools for debugging and validating signal extraction

## Signal Discovery Policies

The bot supports two signal discovery policies that can be configured per channel:

### 1. Single Message Policy (`single_message`)
- Processes each message individually
- Suitable for channels where each message contains complete trading information
- Faster processing and lower API costs
- Default policy for backward compatibility

### 2. Windowed Messages Policy (`windowed_messages`)
- Processes the last N messages together for context
- Useful for channels where trading signals are spread across multiple messages
- Configurable window size (default: 5 messages)
- Better signal detection for fragmented information

## Configuration

### Environment Variables

#### Basic Telegram Configuration
```bash
API_ID=your_telegram_api_id
API_HASH=your_telegram_api_hash
SESSION_NAME=tg_session
```

#### Exchange Selection
```bash
# Choose target exchange: 'xt', 'bitunix', or 'lbank'
EXCHANGE=xt
```

#### Proxy Configuration (Optional)
```bash
PROXY_TYPE=SOCKS5  # or HTTP
PROXY_HOST=127.0.0.1
PROXY_PORT=1080
PROXY_USERNAME=username  # optional
PROXY_PASSWORD=password  # optional
```

#### Channel Configuration

##### Legacy Single Channel (Backward Compatible)
```bash
CHANNEL_ID=@your_channel_username
CHANNEL_TITLE=Your Channel Name
CHANNEL_PROMPT="Custom prompt for this channel"  # optional
```

##### Multi-Channel Configuration (Recommended)
```bash
CHANNELS_CONFIG='[
  {
    "channel_id": "@channel1",
    "channel_title": "Trading Signals Channel",
    "policy": "single_message",
    "enabled": true,
    "prompt": "Extract crypto trading signals from this message"
  },
  {
    "channel_id": "@channel2", 
    "channel_title": "Analysis Channel",
    "policy": "windowed_messages",
    "window_size": 10,
    "enabled": true
  }
]'
```

##### File-Based Channel Configuration
```bash
CHANNELS_FILE=./configs/channels.json
```

Create `configs/channels.json`:
```json
[
  {
    "channel_id": "@your_channel",
    "channel_title": "Channel Name",
    "policy": "single_message",
    "enabled": true,
    "prompt": "Custom extraction prompt"
  }
]
```

#### OpenAI Configuration
```bash
OPENAI_API_KEY=your_openai_api_key
OPENAI_MODEL=gpt-4.1-mini  # default model
OPENAI_TIMEOUT_SECS=299
OPENAI_BASE_URL=https://api.openai.com  # optional
```

#### Image Upload Service
```bash
UPLOAD_BASE=http://localhost:8080  # for image processing
```

#### Exchange Configuration

##### XT Exchange (Futures)
```bash
XT_API_KEY=your_xt_api_key
XT_SECRET=your_xt_secret
XT_PASSWORD=your_xt_password  # optional
XT_MARGIN_MODE=cross  # or 'isolated'
```

##### Bitunix Exchange (Futures)
```bash
BITUNIX_API_KEY=your_bitunix_api_key
BITUNIX_SECRET=your_bitunix_secret
BITUNIX_BASE_URL=https://fapi.bitunix.com
BITUNIX_LANGUAGE=en-US
```

##### LBank Exchange (Legacy Support)
```bash
LBANK_API_KEY=your_lbank_api_key
LBANK_SECRET=your_lbank_secret
LBANK_PASSWORD=your_lbank_password
```

#### Trading Configuration
```bash
ORDER_QUOTE=USDT
ORDER_NOTIONAL=10
MAX_PRICE_DEVIATION_PCT=0.02
ENABLE_AUTO_EXECUTION=1  # 0 to disable, 1 to enable
```

#### Database and Storage
```bash
DB_PATH=./tg_channel.db
MEDIA_DIR=./output/media
BACKFILL=3
```

#### Logging Configuration
```bash
LOG_LEVEL=INFO
LOG_FILE=./output/logs/bot.log
LOG_BACKUP_COUNT=14
```

#### System Configuration
```bash
HEARTBEAT_SECS=180
MAX_BACKOFF_SECS=300
SQL_BUSY_RETRIES=10
SQL_BUSY_SLEEP=0.2
```

## Exchange Support

### XT Exchange (Primary)
- **Type**: Futures trading
- **Features**: Leverage, stop losses, take profits, cross/isolated margin
- **Proxy**: Optional
- **Symbol Format**: BTC/USDT:USDT (linear perpetuals)

### Bitunix Exchange
- **Type**: Futures trading  
- **Features**: Leverage, stop losses, take profits
- **Proxy**: Optional (but recommended for reliability)
- **Symbol Format**: BTCUSDT (concatenated)

### LBank Exchange (Legacy)
- **Type**: Spot and futures
- **Status**: Supported but not actively used for order execution
- **Proxy**: Optional

## Order Sizing

The bot automatically calculates order quantities using:
- **Formula**: `quantity = (90% of available balance) / current_price`
- **Minimum Budget**: $10 USD equivalent
- **Validation**: Orders below $10 budget are rejected with error logging

## Usage

### 1. Install Dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure Environment
Create a `.env` file with your configuration:
```bash
cp .env.example .env
# Edit .env with your values
```

### 3. Run the Bot
```bash
python app.py
```

### 4. First Run Authorization
- Enter your phone number when prompted
- Enter the verification code sent to your Telegram
- Enter 2FA password if enabled

## Testing and Debugging

### Live Exchange Testing
```bash
# Test XT exchange (requires XT_API_KEY, XT_SECRET)
pytest -m live cmd/test/test_xt_live.py -p no:debugging

# Test Bitunix exchange (requires BITUNIX_API_KEY, BITUNIX_SECRET)
pytest -m live cmd/test/test_bitunix_live.py -p no:debugging
```

### Signal Extraction Testing
```bash
# Extract signal from a specific message ID
python cmd/test/extract_signal_for_message.py <message_id>

# List available channels
python cmd/test/list_channels.py
```

### Test Configuration
Set these environment variables for testing:
```bash
TEST_TOKEN=ALGO        # Token to test with
TEST_BUDGET_USDT=2     # Budget for test orders
TEST_LEVERAGE=2        # Leverage for test orders
```

## Architecture

The bot follows Clean Architecture principles:

```
telegram-trading-bot/
├── cmd/                    # Application entry points
│   ├── bot/               # Main bot application
│   └── test/              # Testing utilities
├── internal/              # Core business logic
│   ├── types/            # Domain models and context
│   ├── services/         # Business logic and use cases
│   │   ├── exchange_*.py # Exchange implementations
│   │   ├── signal_extraction.py
│   │   ├── order_sizing.py
│   │   └── openai_client.py
│   ├── repositories/     # Data access layer
│   └── db/              # Database utilities
├── api/                  # External interfaces
│   └── telegram/        # Telegram handlers and client
├── configs/             # Configuration management
├── pkg/                 # Shared utilities
└── output/              # Generated files and logs
```

## Database Schema

The bot uses SQLite with the following main tables:

- **`messages`**: Stores all Telegram messages with metadata
- **`media_files`**: Stores downloaded media file information
- **`trade_signals`**: Stores extracted trading signals from OpenAI
- **`positions_submitted`**: Tracks submitted trading positions and results

## Signal Extraction

The bot uses OpenAI's API to extract trading signals from messages and images. 

### Extracted Information
- **Token/Symbol**: Cryptocurrency symbol (e.g., BTC, ETH)
- **Position Type**: Long or short position
- **Entry Price**: Target entry price
- **Leverage**: Position leverage (default: 2x if not specified)
- **Stop Losses**: Array of stop loss prices
- **Take Profits**: Array of take profit prices

### Special Token Handling
- **Gold Symbols**: `GOLD`, `XAU`, `XAUUSD` are automatically converted to `PAXG` for crypto trading

### Custom Prompts
Each channel can have a custom prompt to improve signal extraction accuracy:
```json
{
  "channel_id": "@signals_channel",
  "prompt": "Extract trading signals from crypto analysis. Focus on entry points and risk management."
}
```

## Error Handling

The bot includes robust error handling with:

- **Automatic Reconnection**: Exponential backoff for connection failures
- **Flood Wait Handling**: Automatic delays for Telegram rate limits  
- **Database Retry Logic**: Handles concurrent access and busy database
- **Exchange Error Handling**: Retry logic for transient exchange errors
- **Graceful Degradation**: Continues operation when optional services fail

## Logging

The bot provides comprehensive structured logging:

### Log Levels
- **INFO**: Normal operation, signal extraction, order execution
- **WARNING**: Non-critical issues, retries, degraded functionality  
- **ERROR**: Failed operations, invalid configurations
- **DEBUG**: Detailed debugging information

### Log Rotation
- **File**: `./output/logs/bot.log`
- **Rotation**: Daily rotation with 14-day retention
- **Format**: Structured JSON logs for easy parsing

### Key Log Events
- Message processing and signal extraction
- Order execution and results
- Exchange API interactions
- Error conditions and recovery attempts
- Channel monitoring status

## Security Considerations

### API Keys and Secrets
- Store API keys securely using environment variables
- Never commit secrets to version control
- Use separate API keys for testing and production
- Consider using encrypted environment files

### Data Security
- Database files contain sensitive trading information
- Media files may contain private channel content
- Log files may contain API responses with sensitive data
- Consider encryption for production deployments

### Network Security
- Use HTTPS for all external API calls
- Configure proxy settings for enhanced privacy
- Validate all external inputs and API responses
- Implement rate limiting to prevent abuse

## Production Deployment

### Recommended Setup
1. **Environment**: Use Docker or virtual environment
2. **Process Management**: Use systemd, supervisor, or PM2
3. **Monitoring**: Set up log monitoring and alerting
4. **Backups**: Regular database and configuration backups
5. **Security**: Firewall, encrypted storage, secure API keys

### Performance Considerations
- **Database**: Regular VACUUM and optimization
- **Logging**: Configure appropriate log levels for production
- **Memory**: Monitor memory usage for long-running processes
- **API Limits**: Respect exchange and OpenAI rate limits

## Troubleshooting

### Common Issues

#### "No channels configured" Error
- Ensure `CHANNELS_CONFIG` or `CHANNELS_FILE` is properly set
- Check JSON syntax in channel configuration

#### "Proxy required" Warning (Bitunix)
- Proxy is now optional for Bitunix
- Configure proxy if experiencing connection issues

#### "Budget below minimum threshold" Error
- Ensure account has more than $10 equivalent balance
- Check `ORDER_QUOTE` currency matches exchange balance

#### Signal Extraction Failures
- Verify `OPENAI_API_KEY` is valid and has credits
- Check `UPLOAD_BASE` service is running for image processing
- Review custom channel prompts for clarity

### Debug Tools
```bash
# Check signal extraction for specific message
python cmd/test/extract_signal_for_message.py <message_id>

# Test exchange connectivity
pytest -m live cmd/test/test_bitunix_live.py::test_fetch_price -s

# View detailed logs
tail -f output/logs/bot.log
``` 