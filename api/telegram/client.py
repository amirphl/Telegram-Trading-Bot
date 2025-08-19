from typing import Optional, Tuple

from telethon import TelegramClient
from telethon.network.connection import ConnectionTcpAbridged

from configs.config import Config


def _build_proxy_tuple(cfg: Config) -> Optional[Tuple]:
    if cfg.proxy_type in ("SOCKS5", "HTTP") and cfg.proxy_host and cfg.proxy_port:
        return (
            cfg.proxy_type.lower(),
            cfg.proxy_host,
            int(cfg.proxy_port),
            True,
            cfg.proxy_username,
            cfg.proxy_password,
        )
    return None


def build_client(cfg: Config) -> TelegramClient:
    return TelegramClient(
        cfg.session_name,
        cfg.api_id,
        cfg.api_hash,
        proxy=_build_proxy_tuple(cfg),
        connection=ConnectionTcpAbridged,
        connection_retries=None,
        request_retries=5,
        retry_delay=1,
        timeout=10,
        flood_sleep_threshold=60,
    ) 