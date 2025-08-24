import asyncio
import sqlite3
import time


DDL_MESSAGES = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=10000;

CREATE TABLE IF NOT EXISTS messages (
  chat_id            INTEGER NOT NULL,
  message_id         INTEGER NOT NULL,
  date_utc           TEXT NOT NULL,
  edit_date_utc      TEXT,
  text               TEXT,
  views              INTEGER,
  forwards           INTEGER,
  replies_count      INTEGER,
  post_author        TEXT,
  grouped_id         INTEGER,
  reply_to_msg_id    INTEGER,
  fwd_from_raw       TEXT,
  via_bot_id         INTEGER,
  entities_raw       TEXT,
  raw_json           TEXT NOT NULL,
  PRIMARY KEY (chat_id, message_id)
);

CREATE TABLE IF NOT EXISTS media_files (
  chat_id        INTEGER NOT NULL,
  message_id     INTEGER NOT NULL,
  file_name      TEXT NOT NULL,
  mime_type      TEXT,
  file_size      INTEGER,
  local_path     TEXT NOT NULL,
  PRIMARY KEY (chat_id, message_id, file_name),
  FOREIGN KEY (chat_id, message_id)
    REFERENCES messages(chat_id, message_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS trade_signals (
  chat_id            INTEGER NOT NULL,
  message_id         INTEGER NOT NULL,
  token              TEXT,
  position_type      TEXT,
  entry_price        REAL,
  leverage           REAL,
  stop_losses_json   TEXT,
  take_profits_json  TEXT,
  model_name         TEXT,
  created_at_utc     TEXT NOT NULL,
  PRIMARY KEY (chat_id, message_id),
  FOREIGN KEY (chat_id, message_id)
    REFERENCES messages(chat_id, message_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS positions_submitted (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id            INTEGER NOT NULL,
  message_id         INTEGER NOT NULL,
  symbol             TEXT NOT NULL,
  side               TEXT NOT NULL,
  quantity           REAL NOT NULL,
  price              REAL,
  leverage           REAL,
  order_id           TEXT,
  status             TEXT NOT NULL,
  error              TEXT,
  created_at_utc     TEXT NOT NULL,
  updated_at_utc     TEXT NOT NULL,
  UNIQUE(chat_id, message_id)
);

CREATE INDEX IF NOT EXISTS idx_messages_chat_date ON messages(chat_id, date_utc);
"""


def connect_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, isolation_level=None, timeout=10.0)
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=10000;")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    for stmt in filter(None, (s.strip() for s in DDL_MESSAGES.split(";"))):
        conn.execute(stmt)


def sql_execute_with_retry(
    conn: sqlite3.Connection,
    sql: str,
    params: tuple = (),
    busy_retries: int = 10,
    busy_sleep_secs: float = 0.2,
) -> None:
    attempts = 0
    while True:
        try:
            conn.execute(sql, params)
            return
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if (
                "database is locked" in msg or "database table is locked" in msg
            ) and attempts < busy_retries:
                attempts += 1
                try:
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        time.sleep(busy_sleep_secs)
                    else:
                        loop.run_until_complete(asyncio.sleep(busy_sleep_secs))
                except Exception:
                    time.sleep(busy_sleep_secs)
                continue
            raise

