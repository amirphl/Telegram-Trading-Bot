import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from telethon.tl.custom.message import Message

from internal.db.sqlite import sql_execute_with_retry
from internal.services.blocking import run_db
from pkg.media import ALLOWED_IMAGE_MIME_TYPES, inspect_image_file, is_path_within
from pkg.serialization import dumps_json


@dataclass(frozen=True)
class MediaPolicy:
    max_file_bytes: int
    max_total_bytes: int
    max_pixels: int
    max_images: int
    max_disk_bytes: int
    retention_days: int

    @classmethod
    def from_config(cls, cfg) -> "MediaPolicy":
        return cls(
            max_file_bytes=cfg.media_max_bytes,
            max_total_bytes=cfg.media_max_total_bytes,
            max_pixels=cfg.media_max_pixels,
            max_images=cfg.media_max_images,
            max_disk_bytes=cfg.media_max_disk_bytes,
            retention_days=cfg.media_retention_days,
        )


@dataclass(frozen=True)
class TelegramMediaInfo:
    supported: bool
    mime_type: str | None
    file_size: int | None
    width: int | None
    height: int | None
    reason: str | None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_processing_status(
    conn,
    chat_id: int,
    message_id: int,
    status: str,
    reason: str | None,
    busy_retries: int,
    busy_sleep_secs: float,
) -> None:
    sql_execute_with_retry(
        conn,
        """
        INSERT INTO message_processing (chat_id, message_id, status, reason, updated_at_utc)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(chat_id, message_id) DO UPDATE SET
          status=excluded.status,
          reason=excluded.reason,
          updated_at_utc=excluded.updated_at_utc;
        """,
        (chat_id, message_id, status, reason, _utc_now()),
        busy_retries=busy_retries,
        busy_sleep_secs=busy_sleep_secs,
    )
    sql_execute_with_retry(
        conn,
        """
        INSERT INTO message_processing_events
          (chat_id, message_id, status, reason, created_at_utc)
        VALUES (?, ?, ?, ?, ?)
        """,
        (chat_id, message_id, status, reason, _utc_now()),
        busy_retries=busy_retries,
        busy_sleep_secs=busy_sleep_secs,
    )


def archive_message_revision(
    conn,
    chat_id: int,
    message_id: int,
    busy_retries: int,
    busy_sleep_secs: float,
) -> bool:
    row = conn.execute(
        """
        SELECT date_utc, edit_date_utc, text, raw_json
        FROM messages WHERE chat_id = ? AND message_id = ?
        """,
        (chat_id, message_id),
    ).fetchone()
    if not row:
        return False
    sql_execute_with_retry(
        conn,
        """
        INSERT INTO message_revisions
          (chat_id, message_id, date_utc, edit_date_utc, text, raw_json, archived_at_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (chat_id, message_id, row[0], row[1], row[2], row[3], _utc_now()),
        busy_retries=busy_retries,
        busy_sleep_secs=busy_sleep_secs,
    )
    return True


def _media_dimensions(msg: Message) -> tuple[int | None, int | None]:
    candidates = []
    photo = getattr(msg, "photo", None)
    if photo:
        candidates.extend(getattr(photo, "sizes", None) or [])
    document = getattr(msg, "document", None)
    if document:
        candidates.extend(getattr(document, "attributes", None) or [])
    file_obj = getattr(msg, "file", None)
    if file_obj:
        candidates.append(file_obj)

    dimensions = []
    for candidate in candidates:
        width = getattr(candidate, "w", None) or getattr(candidate, "width", None)
        height = getattr(candidate, "h", None) or getattr(candidate, "height", None)
        if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
            dimensions.append((width, height))
    if not dimensions:
        return None, None
    return max(dimensions, key=lambda item: item[0] * item[1])


def inspect_telegram_image(msg: Message, policy: MediaPolicy) -> TelegramMediaInfo:
    if not getattr(msg, "media", None):
        return TelegramMediaInfo(False, None, None, None, None, "message_has_no_media")

    for attr in ("sticker", "video", "video_note", "voice", "audio", "gif"):
        if getattr(msg, attr, None):
            return TelegramMediaInfo(False, None, None, None, None, f"unsupported_media_type:{attr}")

    file_obj = getattr(msg, "file", None)
    mime_type = "image/jpeg" if getattr(msg, "photo", None) else getattr(file_obj, "mime_type", None)
    if mime_type not in ALLOWED_IMAGE_MIME_TYPES:
        return TelegramMediaInfo(
            False,
            mime_type,
            getattr(file_obj, "size", None),
            None,
            None,
            f"unsupported_media_mime:{mime_type or 'unknown'}",
        )

    file_size = getattr(file_obj, "size", None)
    if isinstance(file_size, int) and file_size > policy.max_file_bytes:
        return TelegramMediaInfo(
            False,
            mime_type,
            file_size,
            None,
            None,
            f"media_too_large:{file_size}>{policy.max_file_bytes}",
        )

    width, height = _media_dimensions(msg)
    if width and height and width * height > policy.max_pixels:
        return TelegramMediaInfo(
            False,
            mime_type,
            file_size,
            width,
            height,
            f"media_too_many_pixels:{width * height}>{policy.max_pixels}",
        )
    return TelegramMediaInfo(True, mime_type, file_size, width, height, None)


def _managed_message_base(media_dir: Path, chat_id: int, message_id: int) -> Path:
    root = media_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    chat_key = f"n{abs(chat_id)}" if chat_id < 0 else str(chat_id)
    channel_dir = root / f"channel_{chat_key}"
    channel_dir.mkdir(parents=True, exist_ok=True)
    base = channel_dir / f"message_{message_id}"
    if not is_path_within(base, root):
        raise ValueError("refusing media path outside MEDIA_DIR")
    return base


def _directory_size(root: Path) -> int:
    if not root.exists():
        return 0
    total = 0
    for path in root.rglob("*"):
        try:
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def _stored_media_paths(conn, chat_id: int, message_id: int) -> list[Path]:
    rows = conn.execute(
        "SELECT local_path FROM media_files WHERE chat_id = ? AND message_id = ?",
        (chat_id, message_id),
    ).fetchall()
    return [Path(row[0]) for row in rows]


def _remove_managed_file(path: Path, media_dir: Path) -> None:
    if path.is_symlink() and is_path_within(path.parent, media_dir):
        path.unlink(missing_ok=True)
        return
    if not is_path_within(path, media_dir):
        return
    try:
        if path.is_file() or path.is_symlink():
            path.unlink()
    except FileNotFoundError:
        pass


def remove_message_media(
    conn,
    chat_id: int,
    message_id: int,
    media_dir: Path,
    busy_retries: int,
    busy_sleep_secs: float,
) -> None:
    paths = _stored_media_paths(conn, chat_id, message_id)
    for path in paths:
        _remove_managed_file(path, media_dir)
    sql_execute_with_retry(
        conn,
        "DELETE FROM media_files WHERE chat_id = ? AND message_id = ?",
        (chat_id, message_id),
        busy_retries=busy_retries,
        busy_sleep_secs=busy_sleep_secs,
    )


def message_to_record(msg: Message) -> dict[str, Any]:
    entities_raw = msg.entities if getattr(msg, "entities", None) else None
    fwd_raw = msg.fwd_from if getattr(msg, "fwd_from", None) else None
    return {
        "chat_id": msg.chat_id,
        "message_id": msg.id,
        "date_utc": msg.date.replace(tzinfo=None).isoformat() if msg.date else None,
        "edit_date_utc": (msg.edit_date.replace(tzinfo=None).isoformat() if msg.edit_date else None),
        "text": msg.message or None,
        "views": getattr(msg, "views", None),
        "forwards": getattr(msg, "forwards", None),
        "replies_count": getattr(getattr(msg, "replies", None), "replies", None),
        "post_author": getattr(msg, "post_author", None),
        "grouped_id": getattr(msg, "grouped_id", None),
        "reply_to_msg_id": getattr(getattr(msg, "reply_to", None), "reply_to_msg_id", None),
        "fwd_from_raw": dumps_json(fwd_raw) if fwd_raw else None,
        "via_bot_id": getattr(msg, "via_bot_id", None),
        "entities_raw": dumps_json(entities_raw) if entities_raw else None,
        "raw_json": dumps_json(msg),
    }


def upsert_message(
    conn,
    rec: dict[str, Any],
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
    sql_execute_with_retry(conn, sql, vals, busy_retries=busy_retries, busy_sleep_secs=busy_sleep_secs)


def _transaction(conn, operation, busy_retries: int, busy_sleep_secs: float):
    sql_execute_with_retry(
        conn,
        "BEGIN IMMEDIATE",
        busy_retries=busy_retries,
        busy_sleep_secs=busy_sleep_secs,
    )
    try:
        result = operation()
        conn.execute("COMMIT")
        return result
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def begin_message_persistence(
    conn,
    rec: dict[str, Any],
    busy_retries: int,
    busy_sleep_secs: float,
) -> None:
    now = _utc_now()

    def operation() -> None:
        upsert_message(conn, rec, busy_retries, busy_sleep_secs)
        conn.execute(
            """
            UPDATE messages SET persistence_status='pending_media',
              persistence_error=NULL, persistence_updated_at_utc=?
            WHERE chat_id=? AND message_id=?
            """,
            (now, int(rec["chat_id"]), int(rec["message_id"])),
        )

    _transaction(conn, operation, busy_retries, busy_sleep_secs)


def finalize_message_persistence(
    conn,
    rec: dict[str, Any],
    media_records: list[tuple[Path, str, int]],
    *,
    replace_media: bool,
    status: str,
    error: str | None,
    processing_status: str | None,
    processing_reason: str | None,
    busy_retries: int,
    busy_sleep_secs: float,
) -> list[Path]:
    chat_id = int(rec["chat_id"])
    message_id = int(rec["message_id"])

    def operation() -> list[Path]:
        previous = _stored_media_paths(conn, chat_id, message_id) if replace_media else []
        if replace_media:
            conn.execute(
                "DELETE FROM media_files WHERE chat_id=? AND message_id=?",
                (chat_id, message_id),
            )
        for path, mime_type, file_size in media_records:
            insert_media(
                conn,
                chat_id,
                message_id,
                path,
                mime_type,
                file_size,
                busy_retries,
                busy_sleep_secs,
            )
        conn.execute(
            """
            UPDATE messages SET persistence_status=?, persistence_error=?,
              persistence_updated_at_utc=?
            WHERE chat_id=? AND message_id=?
            """,
            (status, error, _utc_now(), chat_id, message_id),
        )
        if processing_status:
            record_processing_status(
                conn,
                chat_id,
                message_id,
                processing_status,
                processing_reason,
                busy_retries,
                busy_sleep_secs,
            )
        return previous

    return _transaction(conn, operation, busy_retries, busy_sleep_secs)


async def download_media_if_any(
    msg: Message,
    channel_title_for_path: str,
    media_dir: Path,
    policy: MediaPolicy | None = None,
    existing_paths: list[Path] | None = None,
) -> list[Path]:
    del channel_title_for_path  # Titles are intentionally excluded from filesystem paths.
    policy = policy or MediaPolicy(
        max_file_bytes=10 * 1024 * 1024,
        max_total_bytes=20 * 1024 * 1024,
        max_pixels=25_000_000,
        max_images=4,
        max_disk_bytes=1024 * 1024 * 1024,
        retention_days=0,
    )
    media_info = inspect_telegram_image(msg, policy)
    if not media_info.supported:
        return []

    reusable: list[Path] = []
    for existing in existing_paths or []:
        try:
            inspect_image_file(
                existing,
                max_bytes=policy.max_file_bytes,
                max_pixels=policy.max_pixels,
            )
            if is_path_within(existing, media_dir):
                reusable.append(existing)
        except (OSError, ValueError):
            continue
    if reusable:
        return reusable

    expected_size = media_info.file_size or 0
    if _directory_size(media_dir) + expected_size > policy.max_disk_bytes:
        return []

    base = _managed_message_base(media_dir, int(msg.chat_id), int(msg.id))
    staging_base = base.parent / f".{base.name}.{uuid.uuid4().hex}.pending"
    downloaded = await msg.download_media(file=staging_base)
    if not downloaded:
        return []
    path = Path(downloaded)
    if not is_path_within(path, media_dir):
        raise ValueError("Telegram returned a media path outside MEDIA_DIR")
    try:
        inspect_image_file(
            path,
            max_bytes=policy.max_file_bytes,
            max_pixels=policy.max_pixels,
        )
    except (OSError, ValueError):
        _remove_managed_file(path, media_dir)
        return []
    if _directory_size(media_dir) > policy.max_disk_bytes:
        _remove_managed_file(path, media_dir)
        return []
    suffix = path.suffix.lower()
    if not suffix or suffix == ".pending":
        suffix = ".jpg" if media_info.mime_type == "image/jpeg" else ".png"
    final_path = base.with_suffix(suffix)
    if not is_path_within(final_path, media_dir):
        _remove_managed_file(path, media_dir)
        raise ValueError("refusing final media path outside MEDIA_DIR")
    try:
        os.replace(path, final_path)
    except OSError:
        _remove_managed_file(path, media_dir)
        raise
    return [final_path]


def insert_media(
    conn,
    chat_id: int,
    message_id: int,
    local_path: Path,
    mime_type: str | None,
    file_size: int | None,
    busy_retries: int,
    busy_sleep_secs: float,
) -> None:
    sql = """
    INSERT INTO media_files (chat_id, message_id, file_name, mime_type, file_size, local_path)
    VALUES (?, ?, ?, ?, ?, ?)
    ON CONFLICT(chat_id, message_id, file_name) DO UPDATE SET
      mime_type=excluded.mime_type,
      file_size=excluded.file_size,
      local_path=excluded.local_path;
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
    media_policy: MediaPolicy | None = None,
    replace_media: bool = False,
    skip_media_reason: str | None = None,
) -> list[Path]:
    rec = await run_db(conn, message_to_record, msg)
    await run_db(
        conn,
        begin_message_persistence,
        conn,
        rec,
        busy_retries,
        busy_sleep_secs,
    )
    policy = media_policy or MediaPolicy(
        max_file_bytes=10 * 1024 * 1024,
        max_total_bytes=20 * 1024 * 1024,
        max_pixels=25_000_000,
        max_images=4,
        max_disk_bytes=1024 * 1024 * 1024,
        retention_days=0,
    )
    if skip_media_reason and getattr(msg, "media", None):
        previous = await run_db(
            conn,
            finalize_message_persistence,
            conn,
            rec,
            [],
            replace_media=replace_media,
            status="rejected",
            error=skip_media_reason,
            processing_status="rejected_media",
            processing_reason=skip_media_reason,
            busy_retries=busy_retries,
            busy_sleep_secs=busy_sleep_secs,
        )
        for path in previous:
            _remove_managed_file(path, media_dir)
        return []

    media_info = inspect_telegram_image(msg, policy)
    if getattr(msg, "media", None) and not media_info.supported:
        previous = await run_db(
            conn,
            finalize_message_persistence,
            conn,
            rec,
            [],
            replace_media=replace_media,
            status="rejected",
            error=media_info.reason,
            processing_status="rejected_media",
            processing_reason=media_info.reason,
            busy_retries=busy_retries,
            busy_sleep_secs=busy_sleep_secs,
        )
        for path in previous:
            _remove_managed_file(path, media_dir)
        return []

    existing_paths = await run_db(
        conn, _stored_media_paths, conn, int(rec["chat_id"]), int(rec["message_id"])
    )
    saved_paths: list[Path] = []
    try:
        downloaded_paths = await download_media_if_any(
            msg,
            channel_title_for_path,
            media_dir,
            policy=policy,
            existing_paths=[] if replace_media else existing_paths,
        )
        media_records = []
        for path in downloaded_paths:
            file_info = inspect_image_file(
                path,
                max_bytes=policy.max_file_bytes,
                max_pixels=policy.max_pixels,
            )
            media_records.append((path, file_info.mime_type, file_info.file_size))
            saved_paths.append(path)

        failure_reason = None
        persistence_status = "complete"
        processing_status = None
        if getattr(msg, "media", None) and not saved_paths:
            failure_reason = "media_download_or_validation_failed"
            persistence_status = "repair_required"
            processing_status = "media_repair_required"
            if _directory_size(media_dir) + (media_info.file_size or 0) > policy.max_disk_bytes:
                failure_reason = "media_disk_limit_exceeded"
                persistence_status = "rejected"
                processing_status = "rejected_media"

        previous = await run_db(
            conn,
            finalize_message_persistence,
            conn,
            rec,
            media_records,
            replace_media=replace_media,
            status=persistence_status,
            error=failure_reason,
            processing_status=processing_status,
            processing_reason=failure_reason,
            busy_retries=busy_retries,
            busy_sleep_secs=busy_sleep_secs,
        )
        current = {path.resolve() for path in saved_paths if path.exists()}
        for path in previous:
            try:
                if path.resolve() not in current:
                    _remove_managed_file(path, media_dir)
            except OSError:
                _remove_managed_file(path, media_dir)
        return saved_paths
    except BaseException as exc:
        reason = f"{exc.__class__.__name__}: media persistence interrupted"
        await run_db(
            conn,
            finalize_message_persistence,
            conn,
            rec,
            [],
            replace_media=False,
            status="repair_required",
            error=reason,
            processing_status="media_repair_required",
            processing_reason=reason,
            busy_retries=busy_retries,
            busy_sleep_secs=busy_sleep_secs,
        )
        raise


def delete_message_and_media(
    conn,
    chat_id: int,
    message_id: int,
    media_dir: Path,
    busy_retries: int,
    busy_sleep_secs: float,
) -> str:
    position = conn.execute(
        "SELECT status FROM positions_submitted WHERE chat_id = ? AND message_id = ?",
        (chat_id, message_id),
    ).fetchone()
    if position:
        remove_message_media(
            conn,
            chat_id,
            message_id,
            media_dir,
            busy_retries,
            busy_sleep_secs,
        )
        record_processing_status(
            conn,
            chat_id,
            message_id,
            "source_deleted_order_retained",
            f"Telegram source was deleted; local media removed and order audit retained with status={position[0]}",
            busy_retries,
            busy_sleep_secs,
        )
        return "retained_order_audit"

    remove_message_media(
        conn,
        chat_id,
        message_id,
        media_dir,
        busy_retries,
        busy_sleep_secs,
    )
    sql_execute_with_retry(
        conn,
        "DELETE FROM messages WHERE chat_id = ? AND message_id = ?",
        (chat_id, message_id),
        busy_retries=busy_retries,
        busy_sleep_secs=busy_sleep_secs,
    )
    return "deleted"


def delete_unsubmitted_signal(
    conn,
    chat_id: int,
    message_id: int,
    busy_retries: int,
    busy_sleep_secs: float,
) -> bool:
    position = conn.execute(
        "SELECT 1 FROM positions_submitted WHERE chat_id = ? AND message_id = ?",
        (chat_id, message_id),
    ).fetchone()
    if position:
        return False
    sql_execute_with_retry(
        conn,
        "DELETE FROM trade_signals WHERE chat_id = ? AND message_id = ?",
        (chat_id, message_id),
        busy_retries=busy_retries,
        busy_sleep_secs=busy_sleep_secs,
    )
    return True


def cleanup_media_storage(conn, media_dir: Path, retention_days: int = 0) -> dict[str, int]:
    root = media_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    removed_files = 0
    removed_rows = 0
    repaired_messages = set()
    cutoff = None
    if retention_days > 0:
        cutoff = datetime.now(timezone.utc).timestamp() - retention_days * 86400

    rows = conn.execute("SELECT chat_id, message_id, file_name, local_path FROM media_files").fetchall()
    known_paths = set()
    for chat_id, message_id, file_name, local_path in rows:
        path = Path(local_path)
        managed = is_path_within(path, root)
        if managed:
            known_paths.add(path.resolve())
        missing = not path.exists() or not managed
        expired = False
        if cutoff is not None and not missing:
            try:
                expired = path.stat().st_mtime < cutoff
            except OSError:
                missing = True
        if missing or expired:
            if expired:
                _remove_managed_file(path, root)
                removed_files += 1
            conn.execute(
                "DELETE FROM media_files WHERE chat_id = ? AND message_id = ? AND file_name = ?",
                (chat_id, message_id, file_name),
            )
            conn.execute(
                """
                UPDATE messages SET persistence_status='repair_required',
                  persistence_error='media row referenced a missing, expired, or unmanaged file',
                  persistence_updated_at_utc=?
                WHERE chat_id=? AND message_id=?
                """,
                (_utc_now(), chat_id, message_id),
            )
            repaired_messages.add((chat_id, message_id))
            removed_rows += 1

    for path in root.rglob("*"):
        try:
            resolved = path.resolve()
            if resolved not in known_paths and path.is_file() and not path.is_symlink() or path.is_symlink() and is_path_within(path.parent, root):
                path.unlink()
                removed_files += 1
        except OSError:
            continue
    directories = sorted(
        (path for path in root.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for directory in directories:
        try:
            directory.rmdir()
        except OSError:
            continue
    pending = conn.execute(
        "SELECT chat_id,message_id FROM messages WHERE persistence_status='pending_media'"
    ).fetchall()
    if pending:
        conn.execute(
            """
            UPDATE messages SET persistence_status='repair_required',
              persistence_error='startup recovered interrupted media persistence',
              persistence_updated_at_utc=?
            WHERE persistence_status='pending_media'
            """,
            (_utc_now(),),
        )
        repaired_messages.update((int(row[0]), int(row[1])) for row in pending)
    return {
        "removed_files": removed_files,
        "removed_rows": removed_rows,
        "repaired_messages": len(repaired_messages),
    }
