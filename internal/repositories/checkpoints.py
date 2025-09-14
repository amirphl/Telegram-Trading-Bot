from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_channel_checkpoint(conn, chat_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT chat_id, last_message_id, status, last_error, updated_at_utc
        FROM channel_checkpoints WHERE chat_id = ?
        """,
        (chat_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "chat_id": int(row[0]),
        "last_message_id": int(row[1]),
        "status": str(row[2]),
        "last_error": row[3],
        "updated_at_utc": str(row[4]),
    }


def latest_stored_message_id(conn, chat_id: int) -> int | None:
    row = conn.execute(
        "SELECT MAX(message_id) FROM messages WHERE chat_id = ?",
        (chat_id,),
    ).fetchone()
    return int(row[0]) if row and row[0] is not None else None


def save_channel_checkpoint(
    conn,
    chat_id: int,
    last_message_id: int,
    *,
    status: str = "ready",
    error: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO channel_checkpoints
          (chat_id, last_message_id, status, last_error, updated_at_utc)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET
          last_message_id=MAX(channel_checkpoints.last_message_id, excluded.last_message_id),
          status=excluded.status,
          last_error=excluded.last_error,
          updated_at_utc=excluded.updated_at_utc
        """,
        (chat_id, last_message_id, status, error, _utc_now()),
    )


def advance_live_checkpoint(conn, chat_id: int, message_id: int) -> bool:
    """Advance only when no unresolved recovery gap precedes the live event."""
    checkpoint = get_channel_checkpoint(conn, chat_id)
    if checkpoint is not None and checkpoint["status"] == "retry_exhausted":
        return False
    save_channel_checkpoint(
        conn,
        chat_id,
        message_id,
        status="ready",
    )
    return True


def mark_channel_checkpoint_failed(conn, chat_id: int, error: str) -> None:
    checkpoint = get_channel_checkpoint(conn, chat_id)
    last_message_id = checkpoint["last_message_id"] if checkpoint else 0
    save_channel_checkpoint(
        conn,
        chat_id,
        last_message_id,
        status="retry_exhausted",
        error=error,
    )
