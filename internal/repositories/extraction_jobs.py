from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from pkg.serialization import dumps_json


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _event(conn, chat_id: int, message_id: int, status: str, code: str | None, detail: str | None) -> None:
    conn.execute(
        """
        INSERT INTO signal_extraction_events
          (chat_id, message_id, status, error_code, detail, created_at_utc)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (chat_id, message_id, status, code, detail, _now().isoformat()),
    )


def enqueue_extraction_job(
    conn,
    chat_id: int,
    message_id: int,
    max_attempts: int,
    *,
    historical: bool,
    allow_execution: bool,
    input_text: str | None = None,
    image_paths: list[str] | None = None,
    replace: bool = False,
) -> bool:
    existing = conn.execute(
        "SELECT status FROM signal_extraction_jobs WHERE chat_id=? AND message_id=?",
        (chat_id, message_id),
    ).fetchone()
    if existing and not replace:
        return False
    now = _now().isoformat()
    conn.execute(
        """
        INSERT INTO signal_extraction_jobs
          (chat_id, message_id, status, attempts, max_attempts, next_attempt_at_utc,
           input_text, image_paths_json,
           historical, allow_execution, created_at_utc, updated_at_utc)
        VALUES (?, ?, 'pending', 0, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(chat_id, message_id) DO UPDATE SET
          status='pending', attempts=0, max_attempts=excluded.max_attempts,
          next_attempt_at_utc=excluded.next_attempt_at_utc,
          last_error_code=NULL, last_error=NULL, model_result_json=NULL,
          validation_json=NULL, historical=excluded.historical,
          input_text=excluded.input_text, image_paths_json=excluded.image_paths_json,
          allow_execution=excluded.allow_execution, updated_at_utc=excluded.updated_at_utc
        """,
        (chat_id, message_id, max_attempts, now, input_text,
         dumps_json(image_paths or []), int(historical), int(allow_execution), now, now),
    )
    _event(conn, chat_id, message_id, "pending", None, None)
    return True


def mark_job(conn, chat_id: int, message_id: int, status: str, detail: str | None = None) -> None:
    now = _now().isoformat()
    conn.execute(
        """
        UPDATE signal_extraction_jobs
        SET status=?, next_attempt_at_utc=NULL, last_error=?, updated_at_utc=?
        WHERE chat_id=? AND message_id=?
        """,
        (status, detail, now, chat_id, message_id),
    )
    _event(conn, chat_id, message_id, status, None, detail)


def claim_extraction_job(conn, chat_id: int, message_id: int) -> dict[str, Any] | None:
    now = _now().isoformat()
    cursor = conn.execute(
        """
        UPDATE signal_extraction_jobs
        SET status='processing', attempts=attempts+1, updated_at_utc=?
        WHERE chat_id=? AND message_id=?
          AND status IN ('pending', 'retrying')
          AND attempts < max_attempts
          AND (next_attempt_at_utc IS NULL OR next_attempt_at_utc <= ?)
        """,
        (now, chat_id, message_id, now),
    )
    if cursor.rowcount != 1:
        return None
    _event(conn, chat_id, message_id, "processing", None, None)
    row = conn.execute(
        """
        SELECT j.chat_id, j.message_id, j.attempts, j.max_attempts, j.historical,
               j.allow_execution, m.date_utc, COALESCE(j.input_text, m.text),
               j.image_paths_json
        FROM signal_extraction_jobs j
        JOIN messages m USING (chat_id, message_id)
        WHERE j.chat_id=? AND j.message_id=?
        """,
        (chat_id, message_id),
    ).fetchone()
    if not row:
        return None
    keys = ("chat_id", "message_id", "attempts", "max_attempts", "historical", "allow_execution", "date_utc", "text", "image_paths_json")
    result = dict(zip(keys, row))
    try:
        result["image_paths"] = json.loads(result.pop("image_paths_json") or "[]")
    except (TypeError, json.JSONDecodeError):
        result["image_paths"] = []
    return result


def due_extraction_jobs(conn, limit: int = 20) -> list[tuple[int, int]]:
    rows = conn.execute(
        """
        SELECT chat_id, message_id FROM signal_extraction_jobs
        WHERE status IN ('pending', 'retrying') AND attempts < max_attempts
          AND (next_attempt_at_utc IS NULL OR next_attempt_at_utc <= ?)
        ORDER BY created_at_utc LIMIT ?
        """,
        (_now().isoformat(), limit),
    ).fetchall()
    return [(int(row[0]), int(row[1])) for row in rows]


def recover_interrupted_jobs(conn) -> dict[str, int]:
    """Recover jobs left in processing by a crash; exhausted jobs fail closed."""
    rows = conn.execute(
        "SELECT chat_id,message_id,attempts,max_attempts FROM signal_extraction_jobs WHERE status='processing'"
    ).fetchall()
    summary = {"requeued": 0, "failed": 0}
    now = _now().isoformat()
    for chat_id, message_id, attempts, max_attempts in rows:
        if attempts < max_attempts:
            status, code, detail = "retrying", "interrupted", "recovered interrupted extraction"
            summary["requeued"] += 1
        else:
            status, code, detail = "failed", "attempts_exhausted", "interrupted extraction exhausted retries"
            summary["failed"] += 1
        conn.execute(
            """
            UPDATE signal_extraction_jobs SET status=?, next_attempt_at_utc=?,
              last_error_code=?, last_error=?, updated_at_utc=?
            WHERE chat_id=? AND message_id=?
            """,
            (status, now if status == "retrying" else None, code, detail, now, chat_id, message_id),
        )
        _event(conn, int(chat_id), int(message_id), status, code, detail)
    return summary


def complete_extraction_job(
    conn,
    chat_id: int,
    message_id: int,
    status: str,
    *,
    result: dict[str, Any] | None = None,
    validation: dict[str, Any] | None = None,
    detail: str | None = None,
) -> None:
    now = _now().isoformat()
    conn.execute(
        """
        UPDATE signal_extraction_jobs SET status=?, next_attempt_at_utc=NULL,
          last_error_code=NULL, last_error=?, model_result_json=?, validation_json=?,
          updated_at_utc=? WHERE chat_id=? AND message_id=?
        """,
        (status, detail, dumps_json(result) if result is not None else None,
         dumps_json(validation) if validation is not None else None, now, chat_id, message_id),
    )
    _event(conn, chat_id, message_id, status, None, detail)


def fail_extraction_job(
    conn,
    chat_id: int,
    message_id: int,
    code: str,
    safe_message: str,
    *,
    retryable: bool,
    retry_base_secs: int,
) -> str:
    row = conn.execute(
        "SELECT attempts, max_attempts FROM signal_extraction_jobs WHERE chat_id=? AND message_id=?",
        (chat_id, message_id),
    ).fetchone()
    attempts, max_attempts = row if row else (1, 1)
    can_retry = retryable and attempts < max_attempts
    status = "retrying" if can_retry else "failed"
    next_at = (
        _now() + timedelta(seconds=retry_base_secs * (2 ** max(0, attempts - 1)))
    ).isoformat() if can_retry else None
    conn.execute(
        """
        UPDATE signal_extraction_jobs SET status=?, next_attempt_at_utc=?,
          last_error_code=?, last_error=?, updated_at_utc=?
        WHERE chat_id=? AND message_id=?
        """,
        (status, next_at, code, safe_message, _now().isoformat(), chat_id, message_id),
    )
    _event(conn, chat_id, message_id, status, code, safe_message)
    return status


def replay_failed_job(conn, chat_id: int, message_id: int) -> bool:
    """Requeue a failed job in review-only mode so replay cannot create a trade."""
    now = _now().isoformat()
    cursor = conn.execute(
        """
        UPDATE signal_extraction_jobs SET status='pending', attempts=0,
          next_attempt_at_utc=?, last_error_code=NULL, last_error=NULL,
          allow_execution=0, updated_at_utc=?
        WHERE chat_id=? AND message_id=? AND status='failed'
        """,
        (now, now, chat_id, message_id),
    )
    if cursor.rowcount == 1:
        _event(conn, chat_id, message_id, "pending", None, "operator replay; execution disabled")
        return True
    return False
