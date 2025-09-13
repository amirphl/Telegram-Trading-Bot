import asyncio
from dataclasses import dataclass, field
from enum import Enum
from sqlite3 import Connection

from configs.config import Config


class SignalDiscoveryPolicy(Enum):
    """How messages from a configured channel are analyzed."""

    SINGLE_MESSAGE = "single_message"
    WINDOWED_MESSAGES = "windowed_messages"


@dataclass
class ChannelConfig:
    channel_id: str
    channel_title: str
    policy: SignalDiscoveryPolicy
    window_size: int = 5
    enabled: bool = True
    prompt: str | None = None


@dataclass
class BotContext:
    db_conn: Connection
    cfg: Config
    channels: dict[int, ChannelConfig] = field(default_factory=dict)
    target_id: int | None = None
    channel_title_for_path: str = "channel"
    ingestion_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def get_channel_config(self, chat_id: int) -> ChannelConfig | None:
        return self.channels.get(chat_id)

    def is_channel_monitored(self, chat_id: int) -> bool:
        channel_config = self.get_channel_config(chat_id)
        return channel_config is not None and channel_config.enabled
