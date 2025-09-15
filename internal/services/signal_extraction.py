from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from pathlib import Path
from typing import Any

from telethon.tl.custom.message import Message

from internal.repositories.extraction_jobs import (
    claim_extraction_job,
    complete_extraction_job,
    due_extraction_jobs,
    enqueue_extraction_job,
    fail_extraction_job,
    mark_job,
)
from internal.repositories.messages import (
    MediaPolicy,
    download_media_if_any,
    record_processing_status,
)
from internal.repositories.signals import TradeSignal, insert_trade_signal
from internal.services.blocking import run_blocking, run_db
from internal.services.executor import submit_position_if_enabled
from internal.services.openai_client import OpenAIExtractionError, OpenAIExtractor
from internal.services.signal_validation import validate_signal_output
from internal.types.context import BotContext, ChannelConfig


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SignalInput:
    supported: bool
    kind: str
    text: str
    reason: str | None = None


def classify_signal_input(
    msg: Message,
    image_paths: list[Path] | None = None,
    text_override: str | None = None,
) -> SignalInput:
    text = (text_override if text_override is not None else (msg.message or "")).strip()
    has_images = bool(image_paths)
    if text and has_images:
        return SignalInput(True, "captioned_image", text)
    if has_images:
        return SignalInput(True, "image_only", text)
    if text:
        return SignalInput(True, "text_only", text)
    if getattr(msg, "media", None):
        return SignalInput(False, "unsupported_media", text, "no_valid_supported_image")
    return SignalInput(False, "empty", text, "message_has_no_signal_content")


def _get_recent_messages(
    conn,
    chat_id: int,
    window_size: int,
) -> list[dict[str, Any]]:
    """Return the most recent channel messages in chronological order."""
    rows = conn.execute(
        """
        SELECT chat_id, message_id, date_utc, text, raw_json
        FROM messages
        WHERE chat_id = ?
        ORDER BY date_utc DESC, message_id DESC
        LIMIT ?
        """,
        (chat_id, window_size),
    ).fetchall()
    return [
        {
            "chat_id": row[0],
            "message_id": row[1],
            "date_utc": row[2],
            "text": row[3] or "",
            "raw_json": row[4],
        }
        for row in reversed(rows)
    ]


def _combine_messages_for_analysis(messages: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for message in messages:
        text = message.get("text", "").strip()
        if text:
            parts.append(
                f"[Message {message.get('message_id', 'unknown')} at "
                f"{message.get('date_utc', 'unknown')}]: {text}"
            )
    return "\n\n".join(parts)


async def _record(
    ctx: BotContext,
    chat_id: int,
    message_id: int,
    status: str,
    reason: str | None,
) -> None:
    await run_db(
        ctx.db_conn,
        record_processing_status,
        ctx.db_conn,
        chat_id,
        message_id,
        status,
        reason,
        ctx.cfg.sql_busy_retries,
        ctx.cfg.sql_busy_sleep,
    )


def _channel_prompt(ctx: BotContext, chat_id: int) -> str | None:
    get_channel_config = getattr(ctx, "get_channel_config", None)
    if callable(get_channel_config):
        channel_config = get_channel_config(chat_id)
    else:
        channel_config = getattr(ctx, "channels", {}).get(chat_id)
    return getattr(channel_config, "prompt", None) if channel_config else None


async def process_extraction_job(
    ctx: BotContext,
    chat_id: int,
    message_id: int,
    *,
    channel_prompt: str | None = None,
) -> bool:
    job = await run_db(
        ctx.db_conn, claim_extraction_job, ctx.db_conn, chat_id, message_id
    )
    if job is None:
        return False

    extractor = OpenAIExtractor(ctx.cfg)
    try:
        result = await run_blocking(
            getattr(ctx.db_conn, "worker_pool", None),
            extractor.extract_signal,
            job["text"] or None,
            [Path(value) for value in job["image_paths"]],
            channel_prompt=channel_prompt or _channel_prompt(ctx, chat_id),
            timeout_secs=float(
                getattr(ctx.cfg, "blocking_operation_timeout_secs", 60)
            ),
        )
    except OpenAIExtractionError as exc:
        status = await run_db(
            ctx.db_conn,
            fail_extraction_job,
            ctx.db_conn,
            chat_id,
            message_id,
            exc.code,
            exc.safe_message,
            retryable=exc.retryable,
            retry_base_secs=int(getattr(ctx.cfg, "extraction_retry_base_secs", 15)),
        )
        await _record(
            ctx,
            chat_id,
            message_id,
            f"extraction_{status}",
            f"{exc.code}: {exc.safe_message}",
        )
        logger.warning(
            "OpenAI extraction %s for message %s: %s",
            status,
            message_id,
            exc.code,
        )
        return True
    except Exception:
        status = await run_db(
            ctx.db_conn,
            fail_extraction_job,
            ctx.db_conn,
            chat_id,
            message_id,
            "internal_extraction_error",
            "Unexpected extraction failure",
            retryable=True,
            retry_base_secs=int(getattr(ctx.cfg, "extraction_retry_base_secs", 15)),
        )
        await _record(
            ctx,
            chat_id,
            message_id,
            f"extraction_{status}",
            "internal_extraction_error",
        )
        logger.exception("OpenAI extraction failed for message %s", message_id)
        return True

    validation = await run_db(
        ctx.db_conn,
        validate_signal_output,
        result,
        ctx.cfg,
        message_date=job["date_utc"],
        conn=ctx.db_conn,
        historical=bool(job["historical"]),
        allow_execution=bool(job["allow_execution"]),
    )
    if not validation.is_signal and validation.valid:
        detail = "model classified input as non-signal"
        await run_db(
            ctx.db_conn,
            complete_extraction_job,
            ctx.db_conn,
            chat_id,
            message_id,
            "no_signal",
            result=result,
            validation=validation.as_dict(),
            detail=detail,
        )
        await _record(ctx, chat_id, message_id, "extraction_no_signal", detail)
        return True
    if not validation.valid or validation.normalized is None:
        detail = ",".join(validation.errors)
        await run_db(
            ctx.db_conn,
            complete_extraction_job,
            ctx.db_conn,
            chat_id,
            message_id,
            "rejected",
            result=result,
            validation=validation.as_dict(),
            detail=detail,
        )
        await _record(ctx, chat_id, message_id, "rejected_signal", detail)
        return True

    normalized = validation.normalized
    signal = TradeSignal(
        chat_id=chat_id,
        message_id=message_id,
        token=normalized["token"],
        position_type=normalized["position_type"],
        entry_price=normalized["entry_price"],
        leverage=normalized["leverage"],
        stop_losses=normalized["stop_losses"],
        take_profits=normalized["take_profits"],
        model_name=ctx.cfg.openai_model,
    )
    await run_db(
        ctx.db_conn,
        insert_trade_signal,
        ctx.db_conn,
        signal,
        busy_retries=ctx.cfg.sql_busy_retries,
        busy_sleep_secs=ctx.cfg.sql_busy_sleep,
    )

    processing_status = "signal_saved_review_required"
    reason = ",".join(validation.execution_blockers) or "execution requires review"
    if validation.executable:
        execution = await run_db(
            ctx.db_conn,
            submit_position_if_enabled,
            ctx.cfg,
            ctx.db_conn,
            signal,
        )
        if execution is not None:
            processing_status = f"signal_saved_execution_{execution.status}"
            reason = execution.error or "execution submitted"
        else:
            processing_status = "signal_saved_execution_disabled"
            reason = "execution adapter declined submission"
    await run_db(
        ctx.db_conn,
        complete_extraction_job,
        ctx.db_conn,
        chat_id,
        message_id,
        "completed",
        result=result,
        validation=validation.as_dict(),
        detail=reason,
    )
    await _record(ctx, chat_id, message_id, processing_status, reason)
    logger.info("Saved validated trade signal for message %s", message_id)
    return True


async def process_due_extraction_jobs(ctx: BotContext, limit: int = 20) -> int:
    processed = 0
    due_jobs = await run_db(
        ctx.db_conn, due_extraction_jobs, ctx.db_conn, limit
    )
    for chat_id, message_id in due_jobs:
        if await process_extraction_job(ctx, chat_id, message_id):
            processed += 1
    return processed


async def extraction_worker_task(ctx: BotContext) -> None:
    interval = max(1, int(getattr(ctx.cfg, "extraction_worker_interval_secs", 5)))
    while True:
        try:
            await process_due_extraction_jobs(ctx)
        except Exception:
            logger.exception("Extraction worker cycle failed safely")
        await asyncio.sleep(interval)


async def process_signal_message(
    ctx: BotContext,
    msg: Message,
    image_paths: list[Path] | None = None,
    *,
    text_override: str | None = None,
    allow_execution: bool = True,
    historical: bool = False,
    replace_existing: bool = False,
    channel_prompt: str | None = None,
) -> None:
    policy = MediaPolicy.from_config(ctx.cfg)
    paths: list[Path] = list(image_paths or [])
    if getattr(msg, "media", None) and not paths:
        paths.extend(
            await download_media_if_any(
                msg,
                ctx.channel_title_for_path,
                ctx.cfg.media_dir,
                policy=policy,
            )
        )
    signal_input = classify_signal_input(msg, paths, text_override)
    chat_id, message_id = int(msg.chat_id), int(msg.id)
    if not signal_input.supported:
        await _record(ctx, chat_id, message_id, "unsupported_signal", signal_input.reason)
        return
    if len(paths) > policy.max_images:
        await _record(ctx, chat_id, message_id, "rejected_signal", "image_count_exceeded")
        return
    try:
        total_bytes = sum(path.stat().st_size for path in paths)
    except OSError:
        await _record(ctx, chat_id, message_id, "rejected_signal", "media_unavailable")
        return
    if total_bytes > policy.max_total_bytes:
        await _record(
            ctx, chat_id, message_id, "rejected_signal", "total_image_bytes_exceeded"
        )
        return

    created = await run_db(
        ctx.db_conn,
        enqueue_extraction_job,
        ctx.db_conn,
        chat_id,
        message_id,
        int(getattr(ctx.cfg, "extraction_max_attempts", 3)),
        historical=historical,
        allow_execution=allow_execution and not historical,
        input_text=signal_input.text,
        image_paths=[str(path) for path in paths],
        replace=replace_existing,
    )
    if not created:
        return
    if not getattr(ctx.cfg, "signal_extraction_enabled", False):
        await run_db(
            ctx.db_conn,
            mark_job,
            ctx.db_conn,
            chat_id,
            message_id,
            "disabled",
            "signal extraction disabled by configuration",
        )
        await _record(
            ctx,
            chat_id,
            message_id,
            "extraction_disabled",
            "SIGNAL_EXTRACTION_ENABLED=false",
        )
        return
    await process_extraction_job(
        ctx, chat_id, message_id, channel_prompt=channel_prompt
    )


async def process_windowed_signal_extraction(
    ctx: BotContext,
    channel_config: ChannelConfig,
    current_msg: Message,
    image_paths: list[Path] | None = None,
) -> None:
    """Queue a validated extraction using the channel's rolling text window."""
    recent_messages = await run_db(
        ctx.db_conn,
        _get_recent_messages,
        ctx.db_conn,
        int(current_msg.chat_id),
        channel_config.window_size,
    )
    if not recent_messages:
        logger.debug(
            "No recent messages found for windowed analysis in chat %s",
            current_msg.chat_id,
        )
        return
    await process_signal_message(
        ctx,
        current_msg,
        image_paths=image_paths,
        text_override=_combine_messages_for_analysis(recent_messages),
        channel_prompt=channel_config.prompt,
    )


async def process_single_message_signal_extraction(
    ctx: BotContext,
    channel_config: ChannelConfig,
    current_msg: Message,
    image_paths: list[Path] | None = None,
) -> None:
    """Queue a validated extraction for one message using its channel prompt."""
    await process_signal_message(
        ctx,
        current_msg,
        image_paths=image_paths,
        channel_prompt=channel_config.prompt,
    )


async def process_signal_if_image_only(
    ctx: BotContext,
    msg: Message,
    image_paths: list[Path] | None = None,
) -> None:
    """Compatibility entry point; the classifier now supports text and images."""
    await process_signal_message(ctx, msg, image_paths=image_paths)
