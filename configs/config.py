from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

LIVE_TRADING_CONFIRMATION = "I_UNDERSTAND_REAL_MONEY_TRADING"


class ConfigValidationError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        self.errors = tuple(errors)
        super().__init__("Invalid configuration:\n- " + "\n- ".join(errors))


def _env_int(name: str, default: int | None = None, *, required: bool = False) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        if required:
            raise ConfigValidationError([f"{name} is required and must be an integer"])
        if default is None:
            raise ConfigValidationError([f"{name} must be an integer"])
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigValidationError([f"{name} must be an integer"]) from exc


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigValidationError([f"{name} must be a number"]) from exc
    if not math.isfinite(value):
        raise ConfigValidationError([f"{name} must be finite"])
    return value


def _parse_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(
        f"{name} must be one of: 1, true, yes, on, 0, false, no, off"
    )


def validate_execution_authorization(cfg: Config) -> None:
    signal_enabled = getattr(cfg, "signal_extraction_enabled", True)
    if signal_enabled and not getattr(cfg, "openai_api_key", "legacy-config"):
        raise ValueError("Signal extraction requires OPENAI_API_KEY")
    if not cfg.enable_auto_execution:
        return
    if not signal_enabled:
        raise ValueError("Automatic execution requires SIGNAL_EXTRACTION_ENABLED=true")
    if getattr(cfg, "signal_approval_mode", "automatic") != "automatic":
        raise ValueError("Automatic execution requires SIGNAL_APPROVAL_MODE=automatic")
    if not getattr(cfg, "signal_token_allowlist", ("legacy-config",)):
        raise ValueError("Automatic execution requires a non-empty SIGNAL_TOKEN_ALLOWLIST")
    exchange = getattr(cfg, "exchange", "lbank")
    if exchange == "lbank" and (not cfg.lbank_api_key or not cfg.lbank_secret):
        raise ValueError("Automatic execution requires LBANK_API_KEY and LBANK_SECRET")
    if exchange == "xt" and (
        not getattr(cfg, "xt_api_key", None) or not getattr(cfg, "xt_secret", None)
    ):
        raise ValueError("Automatic execution on XT requires XT_API_KEY and XT_SECRET")
    if exchange == "bitunix" and (
        not getattr(cfg, "bitunix_api_key", None)
        or not getattr(cfg, "bitunix_secret", None)
    ):
        raise ValueError(
            "Automatic execution on Bitunix requires BITUNIX_API_KEY and BITUNIX_SECRET"
        )
    if cfg.trading_mode == "live" and cfg.live_trading_confirmation != LIVE_TRADING_CONFIRMATION:
        raise ValueError(
            "Live execution requires LIVE_TRADING_CONFIRMATION="
            f"{LIVE_TRADING_CONFIRMATION}"
        )


@dataclass(frozen=True)
class ChannelPolicyConfig:
    channel_id: str
    channel_title: str
    policy: str = "single_message"
    window_size: int = 5
    enabled: bool = True
    prompt: str | None = None


@dataclass(frozen=True)
class Config:
    api_id: int
    api_hash: str
    session_name: str
    exchange: str

    proxy_type: str
    proxy_host: str | None
    proxy_port: int | None
    proxy_username: str | None
    proxy_password: str | None

    channel_title: str
    channel_id: str
    channels: tuple[ChannelPolicyConfig, ...]

    backfill: int
    db_path: str
    media_dir: Path

    heartbeat_secs: int
    max_backoff_secs: int

    sql_busy_retries: int
    sql_busy_sleep: float

    log_level: str
    log_file: str
    log_backup_count: int

    # OpenAI settings
    openai_api_key: str | None
    openai_model: str
    openai_timeout_secs: int
    openai_base_url: str | None
    upload_base: str

    # Exchange/LBank settings
    lbank_api_key: str | None
    lbank_secret: str | None
    lbank_password: str | None

    xt_api_key: str | None
    xt_secret: str | None
    xt_password: str | None
    xt_margin_mode: str

    bitunix_api_key: str | None
    bitunix_secret: str | None
    bitunix_base_url: str
    bitunix_language: str

    order_quote: str
    order_notional: float
    max_price_deviation_pct: float
    enable_auto_execution: bool

    # Telegram media safety settings
    media_max_bytes: int = 10 * 1024 * 1024
    media_max_total_bytes: int = 20 * 1024 * 1024
    media_max_pixels: int = 25_000_000
    media_max_images: int = 4
    media_max_disk_bytes: int = 1024 * 1024 * 1024
    media_retention_days: int = 0

    # Financial execution safety settings
    trading_mode: str = "sandbox"
    execution_market_type: str = "spot"
    margin_mode: str = "isolated"
    require_protective_orders: bool = True
    ticker_max_age_secs: int = 15
    balance_buffer_pct: float = 0.01
    max_leverage: float = 5.0
    live_trading_confirmation: str | None = None

    # AI extraction, validation, and approval settings
    signal_extraction_enabled: bool = False
    signal_approval_mode: str = "manual"
    signal_token_allowlist: tuple[str, ...] = ()
    signal_max_age_secs: int = 300
    signal_min_confidence: float = 0.75
    signal_max_open_positions: int = 3
    signal_max_total_notional: float = 100.0
    extraction_max_attempts: int = 3
    extraction_retry_base_secs: int = 15
    extraction_worker_interval_secs: int = 5
    historical_signal_policy: str = "store_only"

    # Async runtime reliability settings
    blocking_workers: int = 4
    blocking_queue_limit: int = 16
    blocking_submit_timeout_secs: float = 5.0
    blocking_operation_timeout_secs: float = 60.0
    exchange_timeout_secs: int = 30
    backfill_max_attempts: int = 3
    backfill_page_size: int = 100
    backfill_retry_base_secs: float = 1.0
    backfill_failure_policy: str = "continue_live"
    heartbeat_failure_threshold: int = 3
    auth_retry_max_attempts: int = 3


def validate_config(
    cfg: Config,
    *,
    execution_market_type_explicit: bool = True,
) -> None:
    errors: list[str] = []

    def require(condition: bool, message: str) -> None:
        if not condition:
            errors.append(message)

    require(isinstance(cfg.api_id, int) and 0 < cfg.api_id <= 2_147_483_647,
            "API_ID must be an integer between 1 and 2147483647")
    require(bool(cfg.api_hash.strip()), "API_HASH is required and cannot be empty")
    require(bool(cfg.session_name.strip()), "SESSION_NAME cannot be empty")
    require(bool(cfg.channels), "Configure at least one Telegram channel")
    for index, channel in enumerate(cfg.channels, start=1):
        label = f"Channel {index}"
        require(
            bool(channel.channel_id.strip() or channel.channel_title.strip()),
            f"{label} must set channel_id or channel_title",
        )
        if channel.channel_id.strip() and not channel.channel_id.startswith("@"):
            try:
                require(
                    int(channel.channel_id) != 0,
                    f"{label} channel_id must be a non-zero integer or username",
                )
            except ValueError:
                require(
                    any(character.isalpha() for character in channel.channel_id),
                    f"{label} channel_id must be a non-zero integer or username",
                )
        require(
            channel.policy in {"single_message", "windowed_messages"},
            f"{label} policy must be 'single_message' or 'windowed_messages'",
        )
        require(
            1 <= channel.window_size <= 1_000,
            f"{label} window_size must be between 1 and 1000",
        )

    allowed_proxy_types = {"", "SOCKS5", "HTTP"}
    require(cfg.proxy_type in allowed_proxy_types,
            "PROXY_TYPE must be empty, SOCKS5, or HTTP")
    proxy_configured = bool(cfg.proxy_type or cfg.proxy_host or cfg.proxy_port)
    if proxy_configured:
        require(cfg.proxy_type in {"SOCKS5", "HTTP"},
                "PROXY_TYPE is required when proxy settings are provided")
        require(bool(cfg.proxy_host), "PROXY_HOST is required when a proxy is configured")
        require(cfg.proxy_port is not None and 1 <= cfg.proxy_port <= 65535,
                "PROXY_PORT must be between 1 and 65535")
    require(not cfg.proxy_password or bool(cfg.proxy_username),
            "PROXY_USERNAME is required when PROXY_PASSWORD is set")
    require(bool(cfg.db_path.strip()), "DB_PATH cannot be empty")
    require(str(cfg.media_dir).strip() not in {"", "."}, "MEDIA_DIR cannot be empty")
    db_candidate = Path(cfg.db_path)
    require(not db_candidate.exists() or not db_candidate.is_dir(),
            "DB_PATH must refer to a database file, not a directory")
    require(not cfg.media_dir.exists() or cfg.media_dir.is_dir(),
            "MEDIA_DIR must refer to a directory")

    require(0 <= cfg.backfill <= 1_000_000, "BACKFILL must be between 0 and 1000000")
    require(1 <= cfg.heartbeat_secs <= 86_400,
            "HEARTBEAT_SECS must be between 1 and 86400")
    require(1 <= cfg.max_backoff_secs <= 86_400,
            "MAX_BACKOFF_SECS must be between 1 and 86400")
    require(0 <= cfg.sql_busy_retries <= 1_000,
            "SQL_BUSY_RETRIES must be between 0 and 1000")
    require(math.isfinite(cfg.sql_busy_sleep) and 0 <= cfg.sql_busy_sleep <= 60,
            "SQL_BUSY_SLEEP must be between 0 and 60")

    require(bool(cfg.openai_model.strip()), "OPENAI_MODEL cannot be empty")
    require(1 <= cfg.openai_timeout_secs <= 3_600,
            "OPENAI_TIMEOUT_SECS must be between 1 and 3600")
    if cfg.openai_base_url:
        parsed = urlparse(cfg.openai_base_url)
        require(parsed.scheme in {"http", "https"} and bool(parsed.netloc),
                "OPENAI_BASE_URL must be an absolute http(s) URL")

    credentials = (cfg.lbank_api_key, cfg.lbank_secret)
    require(not any(credentials) or all(credentials),
            "LBANK_API_KEY and LBANK_SECRET must be provided together")
    require(cfg.exchange in {"xt", "bitunix"},
            "EXCHANGE must be 'xt' or 'bitunix'")
    require(bool(re.fullmatch(r"[A-Z0-9]{2,10}", cfg.order_quote)),
            "ORDER_QUOTE must contain 2-10 uppercase letters or digits")
    require(math.isfinite(cfg.order_notional) and 0 < cfg.order_notional <= 1_000_000_000,
            "ORDER_NOTIONAL must be between 0 (exclusive) and 1000000000")
    require(math.isfinite(cfg.max_price_deviation_pct)
            and 0 <= cfg.max_price_deviation_pct <= 1,
            "MAX_PRICE_DEVIATION_PCT must be between 0 and 1")

    for name, value in (
        ("MEDIA_MAX_BYTES", cfg.media_max_bytes),
        ("MEDIA_MAX_TOTAL_BYTES", cfg.media_max_total_bytes),
        ("MEDIA_MAX_PIXELS", cfg.media_max_pixels),
        ("MEDIA_MAX_IMAGES", cfg.media_max_images),
        ("MEDIA_MAX_DISK_BYTES", cfg.media_max_disk_bytes),
    ):
        require(value > 0, f"{name} must be positive")
    require(cfg.media_max_total_bytes >= cfg.media_max_bytes,
            "MEDIA_MAX_TOTAL_BYTES must be at least MEDIA_MAX_BYTES")
    require(cfg.media_max_disk_bytes >= cfg.media_max_bytes,
            "MEDIA_MAX_DISK_BYTES must be at least MEDIA_MAX_BYTES")
    require(cfg.media_retention_days >= 0, "MEDIA_RETENTION_DAYS cannot be negative")

    require(cfg.trading_mode in {"sandbox", "live"},
            "TRADING_MODE must be 'sandbox' or 'live'")
    require(cfg.execution_market_type in {"spot", "swap"},
            "EXECUTION_MARKET_TYPE must be 'spot' or 'swap'")
    require(cfg.margin_mode in {"isolated", "cross"},
            "MARGIN_MODE must be 'isolated' or 'cross'")
    require(1 <= cfg.ticker_max_age_secs <= 3_600,
            "TICKER_MAX_AGE_SECS must be between 1 and 3600")
    require(math.isfinite(cfg.balance_buffer_pct) and 0 <= cfg.balance_buffer_pct < 1,
            "BALANCE_BUFFER_PCT must be between 0 (inclusive) and 1 (exclusive)")
    require(math.isfinite(cfg.max_leverage) and 1 <= cfg.max_leverage <= 1_000,
            "MAX_LEVERAGE must be between 1 and 1000")

    require(cfg.signal_approval_mode in {"manual", "automatic"},
            "SIGNAL_APPROVAL_MODE must be 'manual' or 'automatic'")
    require(cfg.historical_signal_policy in {"store_only", "extract_no_execute"},
            "HISTORICAL_SIGNAL_POLICY must be 'store_only' or 'extract_no_execute'")
    require(1 <= cfg.signal_max_age_secs <= 2_592_000,
            "SIGNAL_MAX_AGE_SECS must be between 1 and 2592000")
    require(math.isfinite(cfg.signal_min_confidence) and 0 <= cfg.signal_min_confidence <= 1,
            "SIGNAL_MIN_CONFIDENCE must be between 0 and 1")
    for token in cfg.signal_token_allowlist:
        require(bool(re.fullmatch(r"[A-Z][A-Z0-9]{1,14}", token)),
                f"SIGNAL_TOKEN_ALLOWLIST contains invalid token: {token!r}")
    require(1 <= cfg.signal_max_open_positions <= 100_000,
            "SIGNAL_MAX_OPEN_POSITIONS must be between 1 and 100000")
    require(math.isfinite(cfg.signal_max_total_notional)
            and cfg.signal_max_total_notional > 0,
            "SIGNAL_MAX_TOTAL_NOTIONAL must be finite and positive")
    require(1 <= cfg.extraction_max_attempts <= 100,
            "EXTRACTION_MAX_ATTEMPTS must be between 1 and 100")
    require(0 <= cfg.extraction_retry_base_secs <= 86_400,
            "EXTRACTION_RETRY_BASE_SECS must be between 0 and 86400")
    require(1 <= cfg.extraction_worker_interval_secs <= 3_600,
            "EXTRACTION_WORKER_INTERVAL_SECS must be between 1 and 3600")

    require(1 <= cfg.blocking_workers <= 64, "BLOCKING_WORKERS must be between 1 and 64")
    require(0 <= cfg.blocking_queue_limit <= 10_000,
            "BLOCKING_QUEUE_LIMIT must be between 0 and 10000")
    require(math.isfinite(cfg.blocking_submit_timeout_secs)
            and 0 < cfg.blocking_submit_timeout_secs <= 3_600,
            "BLOCKING_SUBMIT_TIMEOUT_SECS must be between 0 (exclusive) and 3600")
    require(math.isfinite(cfg.blocking_operation_timeout_secs)
            and 0 < cfg.blocking_operation_timeout_secs <= 3_600,
            "BLOCKING_OPERATION_TIMEOUT_SECS must be between 0 (exclusive) and 3600")
    require(1 <= cfg.exchange_timeout_secs <= 3_600,
            "EXCHANGE_TIMEOUT_SECS must be between 1 and 3600")
    require(1 <= cfg.backfill_max_attempts <= 100,
            "BACKFILL_MAX_ATTEMPTS must be between 1 and 100")
    require(1 <= cfg.backfill_page_size <= 1_000,
            "BACKFILL_PAGE_SIZE must be between 1 and 1000")
    require(math.isfinite(cfg.backfill_retry_base_secs)
            and 0 <= cfg.backfill_retry_base_secs <= 86_400,
            "BACKFILL_RETRY_BASE_SECS must be between 0 and 86400")
    require(cfg.backfill_failure_policy in {"continue_live", "stop"},
            "BACKFILL_FAILURE_POLICY must be 'continue_live' or 'stop'")
    require(1 <= cfg.heartbeat_failure_threshold <= 100,
            "HEARTBEAT_FAILURE_THRESHOLD must be between 1 and 100")
    require(1 <= cfg.auth_retry_max_attempts <= 100,
            "AUTH_RETRY_MAX_ATTEMPTS must be between 1 and 100")
    if cfg.enable_auto_execution:
        require(execution_market_type_explicit,
                "Automatic execution requires explicit EXECUTION_MARKET_TYPE=spot or swap")

    if errors:
        raise ConfigValidationError(errors)


def _channel_from_mapping(value: object, source: str) -> ChannelPolicyConfig:
    if not isinstance(value, dict):
        raise ConfigValidationError([f"{source} entries must be JSON objects"])
    try:
        return ChannelPolicyConfig(
            channel_id=str(value.get("channel_id") or "").strip(),
            channel_title=str(value.get("channel_title") or "").strip(),
            policy=str(value.get("policy") or "single_message").strip().lower(),
            window_size=int(value.get("window_size", 5)),
            enabled=bool(value.get("enabled", True)),
            prompt=(str(value.get("prompt") or value.get("channel_prompt") or "").strip() or None),
        )
    except (TypeError, ValueError) as exc:
        raise ConfigValidationError(
            [f"{source} contains an invalid channel configuration"]
        ) from exc


def _parse_channels_config() -> tuple[ChannelPolicyConfig, ...]:
    channels: list[ChannelPolicyConfig] = []
    legacy_channel_id = (os.getenv("CHANNEL_ID") or "").strip()
    legacy_channel_title = (os.getenv("CHANNEL_TITLE") or "").strip()
    if legacy_channel_id or legacy_channel_title:
        channels.append(
            ChannelPolicyConfig(
                channel_id=legacy_channel_id,
                channel_title=legacy_channel_title,
                prompt=(os.getenv("CHANNEL_PROMPT") or "").strip() or None,
            )
        )

    channels_json = (os.getenv("CHANNELS_CONFIG") or "").strip()
    if channels_json:
        try:
            values = json.loads(channels_json)
        except json.JSONDecodeError as exc:
            raise ConfigValidationError(["CHANNELS_CONFIG must be valid JSON"]) from exc
        if not isinstance(values, list):
            raise ConfigValidationError(["CHANNELS_CONFIG must be a JSON array"])
        channels.extend(
            _channel_from_mapping(value, "CHANNELS_CONFIG") for value in values
        )

    channels_file = (os.getenv("CHANNELS_FILE") or "").strip()
    if channels_file:
        path = Path(channels_file)
        try:
            values = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigValidationError(
                [f"CHANNELS_FILE could not be read as JSON: {path}"]
            ) from exc
        if not isinstance(values, list):
            raise ConfigValidationError(["CHANNELS_FILE must contain a JSON array"])
        channels.extend(
            _channel_from_mapping(value, "CHANNELS_FILE") for value in values
        )

    unique: dict[tuple[str, str], ChannelPolicyConfig] = {}
    for channel in channels:
        unique.setdefault((channel.channel_id, channel.channel_title), channel)
    return tuple(unique.values())


def load_config() -> Config:
    load_dotenv()

    api_id = _env_int("API_ID", required=True)
    api_hash = (os.getenv("API_HASH") or "").strip()
    session_name = (os.getenv("SESSION_NAME", "tg_session") or "").strip()
    exchange = (os.getenv("EXCHANGE") or "xt").strip().lower()

    proxy_type = (os.getenv("PROXY_TYPE") or "").upper().strip()
    proxy_host = (os.getenv("PROXY_HOST") or "").strip() or None
    proxy_port_str = os.getenv("PROXY_PORT") or None
    proxy_port = _env_int("PROXY_PORT") if proxy_port_str else None
    proxy_username = (os.getenv("PROXY_USERNAME") or "").strip() or None
    proxy_password = (os.getenv("PROXY_PASSWORD") or "").strip() or None

    channel_title = (os.getenv("CHANNEL_TITLE") or "").strip()
    channel_id = (os.getenv("CHANNEL_ID") or "").strip()
    channels = _parse_channels_config()
    if channels:
        channel_id = channel_id or channels[0].channel_id
        channel_title = channel_title or channels[0].channel_title

    backfill = _env_int("BACKFILL", 3)
    db_path = (os.getenv("DB_PATH", "./tg_channel.db") or "").strip()

    media_dir = Path((os.getenv("MEDIA_DIR", "./output/media") or "").strip())

    heartbeat_secs = _env_int("HEARTBEAT_SECS", 180)
    max_backoff_secs = _env_int("MAX_BACKOFF_SECS", 300)
    sql_busy_retries = _env_int("SQL_BUSY_RETRIES", 10)
    sql_busy_sleep = _env_float("SQL_BUSY_SLEEP", 0.2)
    log_level = (os.getenv("LOG_LEVEL") or "INFO").strip().upper()
    log_file = (os.getenv("LOG_FILE") or "./output/logs/bot.log").strip()
    log_backup_count = _env_int("LOG_BACKUP_COUNT", 14)

    openai_api_key = (os.getenv("OPENAI_API_KEY") or "").strip() or None
    openai_model = (os.getenv("OPENAI_MODEL", "gpt-4o-mini") or "").strip()
    openai_timeout_secs = _env_int("OPENAI_TIMEOUT_SECS", 30)
    openai_base_url = (os.getenv("OPENAI_BASE_URL") or "").strip() or None
    upload_base = (os.getenv("UPLOAD_BASE") or "http://localhost:8080").rstrip("/")

    lbank_api_key = (os.getenv("LBANK_API_KEY") or "").strip() or None
    lbank_secret = (os.getenv("LBANK_SECRET") or "").strip() or None
    lbank_password = (os.getenv("LBANK_PASSWORD") or "").strip() or None
    xt_api_key = (os.getenv("XT_API_KEY") or "").strip() or None
    xt_secret = (os.getenv("XT_SECRET") or "").strip() or None
    xt_password = (os.getenv("XT_PASSWORD") or "").strip() or None
    xt_margin_mode = (os.getenv("XT_MARGIN_MODE") or "cross").strip().lower()
    bitunix_api_key = (os.getenv("BITUNIX_API_KEY") or "").strip() or None
    bitunix_secret = (os.getenv("BITUNIX_SECRET") or "").strip() or None
    bitunix_base_url = (
        os.getenv("BITUNIX_BASE_URL") or "https://fapi.bitunix.com"
    ).rstrip("/")
    bitunix_language = (os.getenv("BITUNIX_LANGUAGE") or "en-US").strip()

    order_quote = (os.getenv("ORDER_QUOTE") or "USDT").strip().upper()
    order_notional = _env_float("ORDER_NOTIONAL", 10)
    max_price_deviation_pct = _env_float("MAX_PRICE_DEVIATION_PCT", 0.02)
    enable_auto_execution = _parse_bool("ENABLE_AUTO_EXECUTION", False)
    trading_mode = (os.getenv("TRADING_MODE") or "sandbox").strip().lower()
    execution_market_type_raw = os.getenv("EXECUTION_MARKET_TYPE")
    execution_market_type = (execution_market_type_raw or "spot").strip().lower()
    margin_mode = (os.getenv("MARGIN_MODE") or "isolated").strip().lower()
    require_protective_orders = _parse_bool("REQUIRE_PROTECTIVE_ORDERS", True)
    ticker_max_age_secs = _env_int("TICKER_MAX_AGE_SECS", 15)
    balance_buffer_pct = _env_float("BALANCE_BUFFER_PCT", 0.01)
    max_leverage = _env_float("MAX_LEVERAGE", 5)
    live_trading_confirmation = os.getenv("LIVE_TRADING_CONFIRMATION") or None
    signal_extraction_enabled = _parse_bool("SIGNAL_EXTRACTION_ENABLED", False)
    signal_approval_mode = (os.getenv("SIGNAL_APPROVAL_MODE") or "manual").strip().lower()
    signal_token_allowlist = tuple(
        dict.fromkeys(
            token.strip().upper()
            for token in (os.getenv("SIGNAL_TOKEN_ALLOWLIST") or "").split(",")
            if token.strip()
        )
    )
    signal_max_age_secs = _env_int("SIGNAL_MAX_AGE_SECS", 300)
    signal_min_confidence = _env_float("SIGNAL_MIN_CONFIDENCE", 0.75)
    signal_max_open_positions = _env_int("SIGNAL_MAX_OPEN_POSITIONS", 3)
    signal_max_total_notional = _env_float("SIGNAL_MAX_TOTAL_NOTIONAL", 100)
    extraction_max_attempts = _env_int("EXTRACTION_MAX_ATTEMPTS", 3)
    extraction_retry_base_secs = _env_int("EXTRACTION_RETRY_BASE_SECS", 15)
    extraction_worker_interval_secs = _env_int("EXTRACTION_WORKER_INTERVAL_SECS", 5)
    historical_signal_policy = (os.getenv("HISTORICAL_SIGNAL_POLICY") or "store_only").strip().lower()
    blocking_workers = _env_int("BLOCKING_WORKERS", 4)
    blocking_queue_limit = _env_int("BLOCKING_QUEUE_LIMIT", 16)
    blocking_submit_timeout_secs = _env_float("BLOCKING_SUBMIT_TIMEOUT_SECS", 5)
    blocking_operation_timeout_secs = _env_float("BLOCKING_OPERATION_TIMEOUT_SECS", 60)
    exchange_timeout_secs = _env_int("EXCHANGE_TIMEOUT_SECS", 30)
    backfill_max_attempts = _env_int("BACKFILL_MAX_ATTEMPTS", 3)
    backfill_page_size = _env_int("BACKFILL_PAGE_SIZE", 100)
    backfill_retry_base_secs = _env_float("BACKFILL_RETRY_BASE_SECS", 1)
    backfill_failure_policy = (
        os.getenv("BACKFILL_FAILURE_POLICY") or "continue_live"
    ).strip().lower()
    heartbeat_failure_threshold = _env_int("HEARTBEAT_FAILURE_THRESHOLD", 3)
    auth_retry_max_attempts = _env_int("AUTH_RETRY_MAX_ATTEMPTS", 3)
    media_max_bytes = _env_int("MEDIA_MAX_BYTES", 10 * 1024 * 1024)
    media_max_total_bytes = _env_int("MEDIA_MAX_TOTAL_BYTES", 20 * 1024 * 1024)
    media_max_pixels = _env_int("MEDIA_MAX_PIXELS", 25_000_000)
    media_max_images = _env_int("MEDIA_MAX_IMAGES", 4)
    media_max_disk_bytes = _env_int("MEDIA_MAX_DISK_BYTES", 1024 * 1024 * 1024)
    media_retention_days = _env_int("MEDIA_RETENTION_DAYS", 0)

    cfg = Config(
        api_id=api_id,
        api_hash=api_hash,
        session_name=session_name,
        exchange=exchange,
        proxy_type=proxy_type,
        proxy_host=proxy_host,
        proxy_port=proxy_port,
        proxy_username=proxy_username,
        proxy_password=proxy_password,
        channel_title=channel_title,
        channel_id=channel_id,
        channels=channels,
        backfill=backfill,
        db_path=db_path,
        media_dir=media_dir,
        heartbeat_secs=heartbeat_secs,
        max_backoff_secs=max_backoff_secs,
        sql_busy_retries=sql_busy_retries,
        sql_busy_sleep=sql_busy_sleep,
        log_level=log_level,
        log_file=log_file,
        log_backup_count=log_backup_count,
        openai_api_key=openai_api_key,
        openai_model=openai_model,
        openai_timeout_secs=openai_timeout_secs,
        openai_base_url=openai_base_url,
        upload_base=upload_base,
        lbank_api_key=lbank_api_key,
        lbank_secret=lbank_secret,
        lbank_password=lbank_password,
        xt_api_key=xt_api_key,
        xt_secret=xt_secret,
        xt_password=xt_password,
        xt_margin_mode=xt_margin_mode,
        bitunix_api_key=bitunix_api_key,
        bitunix_secret=bitunix_secret,
        bitunix_base_url=bitunix_base_url,
        bitunix_language=bitunix_language,
        order_quote=order_quote,
        order_notional=order_notional,
        max_price_deviation_pct=max_price_deviation_pct,
        enable_auto_execution=enable_auto_execution,
        media_max_bytes=media_max_bytes,
        media_max_total_bytes=media_max_total_bytes,
        media_max_pixels=media_max_pixels,
        media_max_images=media_max_images,
        media_max_disk_bytes=media_max_disk_bytes,
        media_retention_days=media_retention_days,
        trading_mode=trading_mode,
        execution_market_type=execution_market_type,
        margin_mode=margin_mode,
        require_protective_orders=require_protective_orders,
        ticker_max_age_secs=ticker_max_age_secs,
        balance_buffer_pct=balance_buffer_pct,
        max_leverage=max_leverage,
        live_trading_confirmation=live_trading_confirmation,
        signal_extraction_enabled=signal_extraction_enabled,
        signal_approval_mode=signal_approval_mode,
        signal_token_allowlist=signal_token_allowlist,
        signal_max_age_secs=signal_max_age_secs,
        signal_min_confidence=signal_min_confidence,
        signal_max_open_positions=signal_max_open_positions,
        signal_max_total_notional=signal_max_total_notional,
        extraction_max_attempts=extraction_max_attempts,
        extraction_retry_base_secs=extraction_retry_base_secs,
        extraction_worker_interval_secs=extraction_worker_interval_secs,
        historical_signal_policy=historical_signal_policy,
        blocking_workers=blocking_workers,
        blocking_queue_limit=blocking_queue_limit,
        blocking_submit_timeout_secs=blocking_submit_timeout_secs,
        blocking_operation_timeout_secs=blocking_operation_timeout_secs,
        exchange_timeout_secs=exchange_timeout_secs,
        backfill_max_attempts=backfill_max_attempts,
        backfill_page_size=backfill_page_size,
        backfill_retry_base_secs=backfill_retry_base_secs,
        backfill_failure_policy=backfill_failure_policy,
        heartbeat_failure_threshold=heartbeat_failure_threshold,
        auth_retry_max_attempts=auth_retry_max_attempts,
    )
    validate_config(
        cfg,
        execution_market_type_explicit=bool(execution_market_type_raw),
    )
    validate_execution_authorization(cfg)
    return cfg
