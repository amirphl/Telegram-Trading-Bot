from __future__ import annotations

import os
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, List, Dict

from dotenv import load_dotenv


@dataclass
class ChannelPolicyConfig:
    """Configuration for a channel's signal discovery policy"""

    channel_id: str
    channel_title: str
    policy: str  # "single_message" or "windowed_messages"
    window_size: int = 5  # Only used for windowed_messages policy
    enabled: bool = True
    prompt: Optional[str] = None


@dataclass(frozen=True)
class Config:
    api_id: int
    api_hash: str
    session_name: str

    # Target exchange: 'xt' or 'bitunix'
    exchange: str

    proxy_type: str
    proxy_host: Optional[str]
    proxy_port: Optional[int]
    proxy_username: Optional[str]
    proxy_password: Optional[str]

    # Multiple channels configuration
    channels: List[ChannelPolicyConfig]

    backfill: int
    db_path: str
    media_dir: Path

    heartbeat_secs: int
    max_backoff_secs: int

    sql_busy_retries: int
    sql_busy_sleep: float

    # Logging
    log_level: str
    log_file: str
    log_backup_count: int

    # OpenAI settings
    openai_api_key: Optional[str]
    openai_model: str
    openai_timeout_secs: int
    openai_base_url: Optional[str]

    # Image upload service base URL
    upload_base: str

    # Exchange/LBank settings
    lbank_api_key: Optional[str]
    lbank_secret: Optional[str]
    lbank_password: Optional[str]

    # Exchange/XT settings
    xt_api_key: Optional[str]
    xt_secret: Optional[str]
    xt_password: Optional[str]
    xt_margin_mode: str  # 'cross' or 'isolated'

    # Exchange/Bitunix settings
    bitunix_api_key: Optional[str]
    bitunix_secret: Optional[str]
    bitunix_base_url: str
    bitunix_language: str

    order_quote: str
    order_notional: float
    max_price_deviation_pct: float
    enable_auto_execution: bool


def _parse_channels_config() -> List[ChannelPolicyConfig]:
    """Parse channels configuration from environment variables and file"""
    channels: List[ChannelPolicyConfig] = []

    # Legacy single-channel
    legacy_channel_id = os.getenv("CHANNEL_ID")
    legacy_channel_title = os.getenv("CHANNEL_TITLE")
    legacy_prompt = os.getenv("CHANNEL_PROMPT")
    if legacy_channel_id and legacy_channel_title:
        channels.append(
            ChannelPolicyConfig(
                channel_id=legacy_channel_id,
                channel_title=legacy_channel_title,
                policy="single_message",
                window_size=5,
                enabled=True,
                prompt=legacy_prompt or None,
            )
        )

    # CHANNELS_CONFIG (JSON string)
    channels_json = os.getenv("CHANNELS_CONFIG")
    if channels_json:
        try:
            channels_data = json.loads(channels_json)
            for c in channels_data:
                channels.append(
                    ChannelPolicyConfig(
                        channel_id=c.get("channel_id", ""),
                        channel_title=c.get("channel_title", ""),
                        policy=c.get("policy", "single_message"),
                        window_size=int(c.get("window_size", 5)),
                        enabled=bool(c.get("enabled", True)),
                        prompt=(c.get("prompt") or c.get("channel_prompt") or None),
                    )
                )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            pass

    # CHANNELS_FILE (JSON array persisted on disk)
    channels_file = os.getenv("CHANNELS_FILE", "./configs/channels.json")
    try:
        p = Path(channels_file)
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, list):
                for c in data:
                    channels.append(
                        ChannelPolicyConfig(
                            channel_id=str(c.get("channel_id", "")),
                            channel_title=str(c.get("channel_title", "")),
                            policy=str(c.get("policy", "single_message")),
                            window_size=int(c.get("window_size", 5)),
                            enabled=bool(c.get("enabled", True)),
                            prompt=(c.get("prompt") or c.get("channel_prompt") or None),
                        )
                    )
    except Exception:
        # Ignore file errors; continue with env-based config
        pass

    if not channels:
        raise ValueError(
            "No channels configured. Set CHANNELS_CONFIG or create configs/channels.json"
        )

    # Deduplicate by (channel_id, channel_title)
    unique: Dict[str, ChannelPolicyConfig] = {}
    for ch in channels:
        key = f"{ch.channel_id}|{ch.channel_title}"
        if key not in unique:
            unique[key] = ch
    return list(unique.values())


def load_config() -> Config:
    load_dotenv()

    api_id = int(os.getenv("API_ID") or 0)
    api_hash = os.getenv("API_HASH") or ""
    session_name = os.getenv("SESSION_NAME", "tg_session")

    exchange = (os.getenv("EXCHANGE") or "xt").strip().lower()
    if exchange not in ("xt", "bitunix"):
        exchange = "xt"

    proxy_type = (os.getenv("PROXY_TYPE") or "").upper().strip()
    proxy_host = os.getenv("PROXY_HOST") or None
    proxy_port_str = os.getenv("PROXY_PORT") or None
    proxy_port = int(proxy_port_str) if proxy_port_str else None
    proxy_username = os.getenv("PROXY_USERNAME") or None
    proxy_password = os.getenv("PROXY_PASSWORD") or None

    channels = _parse_channels_config()

    backfill = int(os.getenv("BACKFILL", "3"))
    db_path = os.getenv("DB_PATH", "./tg_channel.db")

    media_dir = Path(os.getenv("MEDIA_DIR", "./output/media"))
    media_dir.mkdir(parents=True, exist_ok=True)

    heartbeat_secs = int(os.getenv("HEARTBEAT_SECS", "180"))
    max_backoff_secs = int(os.getenv("MAX_BACKOFF_SECS", "300"))
    sql_busy_retries = int(os.getenv("SQL_BUSY_RETRIES", "10"))
    sql_busy_sleep = float(os.getenv("SQL_BUSY_SLEEP", "0.2"))

    # Logging config
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    log_file = os.getenv("LOG_FILE", "./output/logs/bot.log")
    log_backup_count = int(os.getenv("LOG_BACKUP_COUNT", "14"))

    openai_api_key = os.getenv("OPENAI_API_KEY") or None
    openai_model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
    openai_timeout_secs = int(os.getenv("OPENAI_TIMEOUT_SECS", "299"))
    openai_base_url = os.getenv("OPENAI_BASE_URL") or None

    # Image upload service
    upload_base = (os.getenv("UPLOAD_BASE") or "http://localhost:8080").rstrip("/")

    lbank_api_key = os.getenv("LBANK_API_KEY") or None
    lbank_secret = os.getenv("LBANK_SECRET") or None
    lbank_password = os.getenv("LBANK_PASSWORD") or None

    xt_api_key = os.getenv("XT_API_KEY") or None
    xt_secret = os.getenv("XT_SECRET") or None
    xt_password = os.getenv("XT_PASSWORD") or None
    xt_margin_mode = (os.getenv("XT_MARGIN_MODE") or "cross").strip().lower()
    if xt_margin_mode not in ("cross", "isolated"):
        xt_margin_mode = "cross"

    bitunix_api_key = os.getenv("BITUNIX_API_KEY") or None
    bitunix_secret = os.getenv("BITUNIX_SECRET") or None
    bitunix_base_url = os.getenv("BITUNIX_BASE_URL", "https://fapi.bitunix.com").rstrip(
        "/"
    )
    bitunix_language = os.getenv("BITUNIX_LANGUAGE", "en-US")

    order_quote = (os.getenv("ORDER_QUOTE") or "USDT").upper()
    order_notional = float(os.getenv("ORDER_NOTIONAL", "10"))
    max_price_deviation_pct = float(os.getenv("MAX_PRICE_DEVIATION_PCT", "0.02"))
    enable_auto_execution = os.getenv("ENABLE_AUTO_EXECUTION", "1") not in (
        "0",
        "false",
        "False",
    )

    return Config(
        api_id=api_id,
        api_hash=api_hash,
        session_name=session_name,
        proxy_type=proxy_type,
        proxy_host=proxy_host,
        proxy_port=proxy_port,
        proxy_username=proxy_username,
        proxy_password=proxy_password,
        exchange=exchange,
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
    )
