from dataclasses import dataclass
from sqlite3 import Connection
from typing import Optional

from configs.config import Config


@dataclass
class BotContext:
    db_conn: Connection
    target_id: Optional[int]
    channel_title_for_path: str
    cfg: Config 