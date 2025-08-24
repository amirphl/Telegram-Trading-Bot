from pathlib import Path
from typing import Dict, Any, List, Optional

from telethon.tl.custom.message import Message

from pkg.serialization import dumps_json
from internal.db.sqlite import sql_execute_with_retry


def message_to_record(msg: Message) -> Dict[str, Any]:
    entities_raw = msg.entities if getattr(msg, "entities", None) else None
    fwd_raw = msg.fwd_from if getattr(msg, "fwd_from", None) else None
    return {
        "chat_id": msg.chat_id,
        "message_id": msg.id,
        "date_utc": msg.date.replace(tzinfo=None).isoformat() if msg.date else None,
        "edit_date_utc": (
            msg.edit_date.replace(tzinfo=None).isoformat() if msg.edit_date else None
        ),
        "text": msg.message or None,
        "views": getattr(msg, "views", None),
        "forwards": getattr(msg, "forwards", None),
        "replies_count": getattr(getattr(msg, "replies", None), "replies", None),
        "post_author": getattr(msg, "post_author", None),
        "grouped_id": getattr(msg, "grouped_id", None),
        "reply_to_msg_id": getattr(
            getattr(msg, "reply_to", None), "reply_to_msg_id", None
        ),
        "fwd_from_raw": dumps_json(fwd_raw) if fwd_raw else None,
        "via_bot_id": getattr(msg, "via_bot_id", None),
        "entities_raw": dumps_json(entities_raw) if entities_raw else None,
        "raw_json": dumps_json(msg),
    }


def upsert_message(
    conn,
    rec: Dict[str, Any],
    busy_retries: int,
    busy_sleep_secs: float,
) -> None:
    sql = """
    INSERT INTO messages (
      chat_id, message_id, date_utc, edit_date_utc, text,
      views, forwards, replies_count, post_author, grouped_id,
      reply_to_msg_id, fwd_from_raw, via_bot_id, entities_raw, raw_json
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(chat_id, message_id) DO UPDATE SET
      date_utc=excluded.date_utc,
      edit_date_utc=excluded.edit_date_utc,
      text=excluded.text,
      views=excluded.views,
      forwards=excluded.forwards,
      replies_count=excluded.replies_count,
      post_author=excluded.post_author,
      grouped_id=excluded.grouped_id,
      reply_to_msg_id=excluded.reply_to_msg_id,
      fwd_from_raw=excluded.fwd_from_raw,
      via_bot_id=excluded.via_bot_id,
      entities_raw=excluded.entities_raw,
      raw_json=excluded.raw_json;
    """
    vals = (
        rec["chat_id"],
        rec["message_id"],
        rec["date_utc"],
        rec["edit_date_utc"],
        rec["text"],
        rec["views"],
        rec["forwards"],
        rec["replies_count"],
        rec["post_author"],
        rec["grouped_id"],
        rec["reply_to_msg_id"],
        rec["fwd_from_raw"],
        rec["via_bot_id"],
        rec["entities_raw"],
        rec["raw_json"],
    )
    sql_execute_with_retry(
        conn, sql, vals, busy_retries=busy_retries, busy_sleep_secs=busy_sleep_secs
    )


async def download_media_if_any(
    msg: Message,
    channel_title_for_path: str,
    media_dir: Path,
) -> List[Path]:
    saved: List[Path] = []
    if msg.media:
        from datetime import datetime

        stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        base = f"{channel_title_for_path}_{msg.id}_{stamp}"
        path = await msg.download_media(file=media_dir / base)
        if path:
            saved.append(Path(path))
    return saved


def insert_media(
    conn,
    chat_id: int,
    message_id: int,
    local_path: Path,
    mime_type: Optional[str],
    file_size: Optional[int],
    busy_retries: int,
    busy_sleep_secs: float,
) -> None:
    sql = """
    INSERT INTO media_files (chat_id, message_id, file_name, mime_type, file_size, local_path)
    VALUES (?, ?, ?, ?, ?, ?)
    ON CONFLICT(chat_id, message_id, file_name) DO NOTHING;
    """
    sql_execute_with_retry(
        conn,
        sql,
        (chat_id, message_id, local_path.name, mime_type, file_size, str(local_path)),
        busy_retries=busy_retries,
        busy_sleep_secs=busy_sleep_secs,
    )


async def persist_message(
    conn,
    msg: Message,
    channel_title_for_path: str,
    media_dir: Path,
    busy_retries: int,
    busy_sleep_secs: float,
) -> List[Path]:
    rec = message_to_record(msg)
    upsert_message(
        conn, rec, busy_retries=busy_retries, busy_sleep_secs=busy_sleep_secs
    )
    saved_paths: List[Path] = []
    for p in await download_media_if_any(msg, channel_title_for_path, media_dir):
        insert_media(
            conn,
            rec["chat_id"],
            rec["message_id"],
            p,
            None,
            None,
            busy_retries=busy_retries,
            busy_sleep_secs=busy_sleep_secs,
        )
        saved_paths.append(p)
    return saved_paths

