# Telegram Trading Bot

A safety-focused Python service that monitors one or more Telegram channels,
extracts structured trading signals from text and images with OpenAI, and can
submit orders through supported cryptocurrency exchanges. Automatic execution
is disabled by default.

> **Financial risk:** This software can place real orders when explicitly
> configured. Test in sandbox mode, use least-privilege API credentials with
> withdrawals disabled, and verify exchange behavior before enabling live trading.

## Features

- Monitor multiple Telegram channels with independent extraction policies.
- Process complete signals individually or combine a rolling message window.
- Apply a custom OpenAI extraction prompt per channel.
- Persist messages, validated media, extraction jobs, signals, and order state in SQLite.
- Validate model output, token allowlists, signal age, confidence, and exposure limits.
- Retry durable extraction jobs and recover interrupted backfills and submissions.
- Execute XT and Bitunix futures orders, with an LBank/CCXT execution and
  reconciliation implementation available in the service layer.
- Support leverage, stop losses, take profits, price-deviation checks, and
  protective-order reconciliation where provided by the selected backend.
- Rotate structured logs and optionally connect through an HTTP or SOCKS5 proxy.

## Supported environment

- CPython 3.10 through 3.14
- SQLite
- Telegram API credentials
- OpenAI credentials when signal extraction is enabled
- Credentials for the selected exchange when automatic execution is enabled

## Installation

From a clean checkout:

```bash
git clone https://github.com/amirphl/Telegram-Trading-Bot.git
cd Telegram-Trading-Bot
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install ".[dev]"
cp .env.example .env
```

On Windows PowerShell, activate with `.venv\Scripts\Activate.ps1`. Fill in the
required values in `.env`; never commit that file.

Installing from `pyproject.toml` also installs the dependencies declared through
`requirements.txt` and provides the `telegram-trading-bot` command.

## Validate configuration

Validate every setting without creating the database or media directory and
without contacting Telegram, OpenAI, or an exchange:

```bash
telegram-trading-bot --check-config
```

A default-safe configuration reports `auto_execution=False` and
`signal_extraction=False`. Invalid settings produce an actionable error and a
non-zero exit status.

## Start the bot

The supported installed entry point is:

```bash
telegram-trading-bot
```

The compatibility entry point remains available:

```bash
python app.py
```

The first interactive run may request the Telegram account phone number, login
code, and two-factor password. Later runs reuse the local Telethon session. Stop
the service with `Ctrl+C`.

## Channel configuration

For a single channel, use its marked numeric ID, public username, or title:

```bash
CHANNEL_ID=@signals_channel
CHANNEL_TITLE=Signals Channel
CHANNEL_PROMPT="Extract crypto entries and risk-management levels"
```

For multiple channels, use inline JSON:

```bash
CHANNELS_CONFIG='[
  {
    "channel_id": "@complete_signals",
    "channel_title": "Complete Signals",
    "policy": "single_message",
    "enabled": true,
    "prompt": "Extract one complete trade signal"
  },
  {
    "channel_id": "@fragmented_analysis",
    "channel_title": "Analysis",
    "policy": "windowed_messages",
    "window_size": 10,
    "enabled": true
  }
]'
```

Alternatively, set `CHANNELS_FILE=./configs/channels.json` and place the same JSON
array in that file. Duplicate channel ID/title pairs are ignored.

### Discovery policies

`single_message` processes each message independently. It is appropriate when a
channel publishes the symbol, direction, entry, and protection levels together.

`windowed_messages` combines the most recent `window_size` messages in chronological
order. It is useful when an analysis and its entry or risk levels arrive separately.
The default window size is 5.

## Configuration reference

Blank values in `.env.example` contain no credentials. Percentages are decimal
fractions, so `0.02` means 2%.

### Telegram, storage, and media

| Variable | Default | Meaning |
| --- | --- | --- |
| `API_ID`, `API_HASH` | required | Telegram application credentials from `my.telegram.org`. |
| `SESSION_NAME` | `tg_session` | Local Telethon session name or path. |
| `CHANNEL_ID`, `CHANNEL_TITLE` | blank | Legacy single-channel selector; set at least one. |
| `CHANNEL_PROMPT` | blank | Optional prompt for the legacy channel. |
| `CHANNELS_CONFIG`, `CHANNELS_FILE` | blank | Inline or file-based multi-channel configuration. |
| `PROXY_TYPE` | blank | `SOCKS5` or `HTTP`; blank disables proxy use. |
| `PROXY_HOST`, `PROXY_PORT` | blank | Required together when a proxy is enabled. |
| `PROXY_USERNAME`, `PROXY_PASSWORD` | blank | Optional proxy authentication. |
| `DB_PATH` | `./tg_channel.db` | SQLite database file. |
| `MEDIA_DIR` | `./output/media` | Root for validated downloaded images. |
| `BACKFILL` | `3` | Recent messages recovered at startup. |
| `MEDIA_MAX_BYTES` | `10485760` | Maximum size of one image. |
| `MEDIA_MAX_TOTAL_BYTES` | `20971520` | Maximum aggregate image size per message or album. |
| `MEDIA_MAX_PIXELS`, `MEDIA_MAX_IMAGES` | `25000000`, `4` | Decode and image-count limits. |
| `MEDIA_MAX_DISK_BYTES` | `1073741824` | Managed-media storage quota. |
| `MEDIA_RETENTION_DAYS` | `0` | Age cleanup threshold; `0` disables age-based removal. |

### OpenAI extraction and approval

| Variable | Default | Meaning |
| --- | --- | --- |
| `SIGNAL_EXTRACTION_ENABLED` | `false` | Enables OpenAI extraction and its durable worker. |
| `OPENAI_API_KEY` | blank | Required when extraction is enabled. |
| `OPENAI_MODEL` | `gpt-4o-mini` | Model identifier for the configured endpoint. |
| `OPENAI_TIMEOUT_SECS`, `OPENAI_BASE_URL` | `30`, blank | Request timeout and optional compatible endpoint. |
| `UPLOAD_BASE` | `http://localhost:8080` | Optional image-upload service used before inline fallback. |
| `SIGNAL_APPROVAL_MODE` | `manual` | `manual` stores for review; `automatic` permits eligible execution. |
| `SIGNAL_TOKEN_ALLOWLIST` | blank | Comma-separated uppercase assets eligible for execution. |
| `SIGNAL_MAX_AGE_SECS` | `300` | Maximum age for execution eligibility. |
| `SIGNAL_MIN_CONFIDENCE` | `0.75` | Required model confidence from 0 through 1. |
| `SIGNAL_MAX_OPEN_POSITIONS` | `3` | Portfolio-wide open-position limit. |
| `SIGNAL_MAX_TOTAL_NOTIONAL` | `100` | Maximum aggregate configured quote notional. |
| `EXTRACTION_MAX_ATTEMPTS` | `3` | Bounded attempts per durable extraction job. |
| `EXTRACTION_RETRY_BASE_SECS` | `15` | Base delay for retryable extraction failures. |
| `EXTRACTION_WORKER_INTERVAL_SECS` | `5` | Durable queue polling interval. |
| `HISTORICAL_SIGNAL_POLICY` | `store_only` | `store_only` or `extract_no_execute`; history never auto-executes. |

The structured result includes the token, long/short direction, entry price,
leverage, stop losses, take profits, signal classification, and confidence.

## Exchange configuration

Select the active backend with `EXCHANGE=xt` or `EXCHANGE=bitunix`.

### XT futures

```bash
EXCHANGE=xt
XT_API_KEY=
XT_SECRET=
XT_PASSWORD=
XT_MARGIN_MODE=cross
```

### Bitunix futures

```bash
EXCHANGE=bitunix
BITUNIX_API_KEY=
BITUNIX_SECRET=
BITUNIX_BASE_URL=https://fapi.bitunix.com
BITUNIX_LANGUAGE=en-US
```

The repository also contains the LBank/CCXT execution path and its reconciliation
logic. Only select an exchange accepted by the current configuration validator.

### Execution safety

| Variable | Default | Meaning |
| --- | --- | --- |
| `ENABLE_AUTO_EXECUTION` | `false` | Master execution switch. |
| `TRADING_MODE` | `sandbox` | `sandbox` or `live`. |
| `LIVE_TRADING_CONFIRMATION` | blank | Must equal `I_UNDERSTAND_REAL_MONEY_TRADING` for live automation. |
| `EXECUTION_MARKET_TYPE` | `spot` | `spot` or `swap`; set explicitly for automatic execution. |
| `MARGIN_MODE` | `isolated` | `isolated` or `cross` where supported. |
| `ORDER_QUOTE`, `ORDER_NOTIONAL` | `USDT`, `10` | Quote asset and positive quote amount per entry. |
| `MAX_PRICE_DEVIATION_PCT` | `0.02` | Maximum signal-entry versus fresh-price deviation. |
| `REQUIRE_PROTECTIVE_ORDERS` | `true` | Require protection and safe compensation where supported. |
| `TICKER_MAX_AGE_SECS` | `15` | Maximum acceptable ticker age. |
| `BALANCE_BUFFER_PCT`, `MAX_LEVERAGE` | `0.01`, `5` | Balance reserve and leverage ceiling. |
| `EXCHANGE_TIMEOUT_SECS` | `30` | Exchange request timeout. |

Automatic mode additionally requires automatic signal approval, a non-empty token
allowlist, matching exchange credentials, and the live confirmation when applicable.

## Runtime, database, and logging

| Variable | Default | Meaning |
| --- | --- | --- |
| `HEARTBEAT_SECS`, `MAX_BACKOFF_SECS` | `180`, `300` | Health interval and reconnect ceiling. |
| `SQL_BUSY_RETRIES`, `SQL_BUSY_SLEEP` | `10`, `0.2` | SQLite lock retries and delay. |
| `BLOCKING_WORKERS`, `BLOCKING_QUEUE_LIMIT` | `4`, `16` | Bounded worker threads and queue size. |
| `BLOCKING_SUBMIT_TIMEOUT_SECS` | `5` | Maximum wait to enter the worker queue. |
| `BLOCKING_OPERATION_TIMEOUT_SECS` | `60` | Blocking operation timeout. |
| `BACKFILL_MAX_ATTEMPTS`, `BACKFILL_PAGE_SIZE` | `3`, `100` | Recovery attempts and page size. |
| `BACKFILL_RETRY_BASE_SECS` | `1` | Backfill retry base delay. |
| `BACKFILL_FAILURE_POLICY` | `continue_live` | Continue monitoring or stop after exhausted retries. |
| `HEARTBEAT_FAILURE_THRESHOLD` | `3` | Failures before reconnecting. |
| `AUTH_RETRY_MAX_ATTEMPTS` | `3` | Transient authentication retry limit. |
| `LOG_LEVEL` | `INFO` | Console and file log threshold. |
| `LOG_FILE` | `./output/logs/bot.log` | Rotating log destination. |
| `LOG_BACKUP_COUNT` | `14` | Retained rotated log files. |

Main SQLite tables include `messages`, `media_files`, `trade_signals`,
`signal_extraction_jobs`, `positions_submitted`, position events, and protective
orders. See [`internal/db/MIGRATIONS.md`](internal/db/MIGRATIONS.md) for backup and
rollback guidance.

## Tests and diagnostics

Run the offline automated suite with:

```bash
python -m unittest discover -s tests -v
```

Tests use mocked external services. `cmd/test/openai_sample.py` is an explicitly
manual network diagnostic and is not part of CI.

Live exchange checks require credentials and explicit test selection. Never use a
production account for development diagnostics.

## Security and operations

- Keep `.env`, Telegram session files, the database, media, and logs private.
- Disable withdrawals on exchange API keys and use separate sandbox credentials.
- Back up the SQLite database before upgrades and retain migration rollback copies.
- Monitor disk usage, extraction retries, reconciliation/manual-review states, and
  exchange/OpenAI rate limits.
- Do not automatically resubmit an order whose exchange outcome is ambiguous.

## License

MIT. See [`LICENSE`](LICENSE).
