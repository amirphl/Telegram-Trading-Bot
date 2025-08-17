from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    api_id: int
    api_hash: str
    session_name: str

    proxy_type: str
    proxy_host: Optional[str]
    proxy_port: Optional[int]
    proxy_username: Optional[str]
    proxy_password: Optional[str]

    channel_title: str
    channel_id: str

    backfill: int
    db_path: str
    media_dir: Path

    heartbeat_secs: int
    max_backoff_secs: int

    sql_busy_retries: int
    sql_busy_sleep: float

    # OpenAI settings
    openai_api_key: Optional[str]
    openai_model: str
    openai_timeout_secs: int
    openai_base_url: Optional[str]

    # Exchange/LBank settings
    lbank_api_key: Optional[str]
    lbank_secret: Optional[str]
    lbank_password: Optional[str]

    order_quote: str
    order_notional: float
    max_price_deviation_pct: float
    enable_auto_execution: bool


def load_config() -> Config:
    load_dotenv()

    api_id = int(os.getenv("API_ID"))
    api_hash = os.getenv("API_HASH") or ""
    session_name = os.getenv("SESSION_NAME", "tg_session")

    proxy_type = (os.getenv("PROXY_TYPE") or "").upper().strip()
    proxy_host = os.getenv("PROXY_HOST") or None
    proxy_port_str = os.getenv("PROXY_PORT") or None
    proxy_port = int(proxy_port_str) if proxy_port_str else None
    proxy_username = os.getenv("PROXY_USERNAME") or None
    proxy_password = os.getenv("PROXY_PASSWORD") or None

    channel_title = os.getenv("CHANNEL_TITLE") or ""
    channel_id = os.getenv("CHANNEL_ID") or ""

    backfill = int(os.getenv("BACKFILL", "3"))
    db_path = os.getenv("DB_PATH", "./tg_channel.db")

    media_dir = Path(os.getenv("MEDIA_DIR", "./output/media"))
    media_dir.mkdir(parents=True, exist_ok=True)

    heartbeat_secs = int(os.getenv("HEARTBEAT_SECS", "180"))
    max_backoff_secs = int(os.getenv("MAX_BACKOFF_SECS", "300"))
    sql_busy_retries = int(os.getenv("SQL_BUSY_RETRIES", "10"))
    sql_busy_sleep = float(os.getenv("SQL_BUSY_SLEEP", "0.2"))

    openai_api_key = os.getenv("OPENAI_API_KEY") or None
    openai_model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    openai_timeout_secs = int(os.getenv("OPENAI_TIMEOUT_SECS", "30"))
    openai_base_url = os.getenv("OPENAI_BASE_URL") or None

    lbank_api_key = os.getenv("LBANK_API_KEY") or None
    lbank_secret = os.getenv("LBANK_SECRET") or None
    lbank_password = os.getenv("LBANK_PASSWORD") or None

    order_quote = (os.getenv("ORDER_QUOTE") or "USDT").upper()
    order_notional = float(os.getenv("ORDER_NOTIONAL", "10"))
    max_price_deviation_pct = float(os.getenv("MAX_PRICE_DEVIATION_PCT", "0.02"))
    enable_auto_execution = (os.getenv("ENABLE_AUTO_EXECUTION", "1") not in ("0", "false", "False"))

    return Config(
        api_id=api_id,
        api_hash=api_hash,
        session_name=session_name,
        proxy_type=proxy_type,
        proxy_host=proxy_host,
        proxy_port=proxy_port,
        proxy_username=proxy_username,
        proxy_password=proxy_password,
        channel_title=channel_title,
        channel_id=channel_id,
        backfill=backfill,
        db_path=db_path,
        media_dir=media_dir,
        heartbeat_secs=heartbeat_secs,
        max_backoff_secs=max_backoff_secs,
        sql_busy_retries=sql_busy_retries,
        sql_busy_sleep=sql_busy_sleep,
        openai_api_key=openai_api_key,
        openai_model=openai_model,
        openai_timeout_secs=openai_timeout_secs,
        openai_base_url=openai_base_url,
        lbank_api_key=lbank_api_key,
        lbank_secret=lbank_secret,
        lbank_password=lbank_password,
        order_quote=order_quote,
        order_notional=order_notional,
        max_price_deviation_pct=max_price_deviation_pct,
        enable_auto_execution=enable_auto_execution,
    ) 