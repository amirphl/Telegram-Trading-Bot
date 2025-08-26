from dataclasses import dataclass
from sqlite3 import Connection
from typing import Optional, Dict
from enum import Enum

from configs.config import Config


class SignalDiscoveryPolicy(Enum):
    """Signal discovery policies for channels"""

    SINGLE_MESSAGE = "single_message"  # Process each message individually
    WINDOWED_MESSAGES = "windowed_messages"  # Process last N messages together


@dataclass
class ChannelConfig:
    """Configuration for a single channel"""

    channel_id: str
    channel_title: str
    policy: SignalDiscoveryPolicy
    window_size: int = 5  # Only used for WINDOWED_MESSAGES policy
    enabled: bool = True
    prompt: Optional[str] = None


@dataclass
class BotContext:
    db_conn: Connection
    channels: Dict[int, ChannelConfig]  # chat_id -> ChannelConfig
    cfg: Config

    def get_channel_config(self, chat_id: int) -> Optional[ChannelConfig]:
        """Get channel configuration by chat_id"""
        return self.channels.get(chat_id)

    def is_channel_monitored(self, chat_id: int) -> bool:
        """Check if a channel is being monitored"""
        channel_config = self.channels.get(chat_id)
        return channel_config is not None and channel_config.enabled

