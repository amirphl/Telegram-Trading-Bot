from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from internal.db.sqlite import sql_execute_with_retry


@dataclass
class SubmittedPosition:
    chat_id: int
    message_id: int
    symbol: str
    side: str
    quantity: float
    price: Optional[float]
    leverage: Optional[float]
    order_id: Optional[str]
    status: str
    error: Optional[str] = None


def upsert_submitted_position(conn, sp: SubmittedPosition, busy_retries: int, busy_sleep_secs: float) -> None:
    now = datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()
    sql = """
    INSERT INTO positions_submitted (
      chat_id, message_id, symbol, side, quantity, price, leverage, order_id, status, error, created_at_utc, updated_at_utc
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(chat_id, message_id) DO UPDATE SET
      symbol=excluded.symbol,
      side=excluded.side,
      quantity=excluded.quantity,
      price=excluded.price,
      leverage=excluded.leverage,
      order_id=excluded.order_id,
      status=excluded.status,
      error=excluded.error,
      updated_at_utc=excluded.updated_at_utc
    ;
    """
    sql_execute_with_retry(
        conn,
        sql,
        (
            sp.chat_id,
            sp.message_id,
            sp.symbol,
            sp.side,
            sp.quantity,
            sp.price,
            sp.leverage,
            sp.order_id,
            sp.status,
            sp.error,
            now,
            now,
        ),
        busy_retries=busy_retries,
        busy_sleep_secs=busy_sleep_secs,
    )


def update_position_status(conn, chat_id: int, message_id: int, status: str, error: Optional[str], busy_retries: int, busy_sleep_secs: float) -> None:
    now = datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()
    sql = """
    UPDATE positions_submitted
    SET status = ?, error = ?, updated_at_utc = ?
    WHERE chat_id = ? AND message_id = ?
    ;
    """
    sql_execute_with_retry(
        conn,
        sql,
        (
            status,
            error,
            now,
            chat_id,
            message_id,
        ),
        busy_retries=busy_retries,
        busy_sleep_secs=busy_sleep_secs,
    ) 