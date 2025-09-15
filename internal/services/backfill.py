import asyncio
import logging
import random
from dataclasses import dataclass
from typing import Any

from telethon import utils as telethon_utils
from telethon.errors import FloodWaitError

from internal.repositories.checkpoints import (
    get_channel_checkpoint,
    latest_stored_message_id,
    mark_channel_checkpoint_failed,
    save_channel_checkpoint,
)
from internal.repositories.extraction_jobs import enqueue_extraction_job, mark_job
from internal.repositories.messages import persist_message
from internal.services.blocking import run_db
from internal.services.signal_extraction import (
    classify_signal_input,
    process_signal_message,
)
from internal.types.context import BotContext


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BackfillResult:
    completed: bool
    processed: int
    last_message_id: int
    attempts: int
    continued_live: bool = False
    error: str | None = None


class BackfillStopped(RuntimeError):
    pass


def _channel_details(entity: Any, ctx: BotContext) -> tuple[int, str, str]:
    """Resolve checkpoint identity and display/path names for either context model."""
    try:
        entity_id = int(telethon_utils.get_peer_id(entity))
    except (AttributeError, TypeError, ValueError):
        entity_id = int(ctx.target_id) if ctx.target_id is not None else 0

    channel_config = None
    get_channel_config = getattr(ctx, "get_channel_config", None)
    if callable(get_channel_config):
        channel_config = get_channel_config(entity_id)

    target_id = entity_id or (
        int(ctx.target_id) if ctx.target_id is not None else 0
    )
    if channel_config is not None:
        title = channel_config.channel_title
        return target_id, title.replace(" ", "_"), title

    path_title = getattr(ctx, "channel_title_for_path", "channel") or "channel"
    return target_id, path_title, path_title.replace("_", " ")


async def _collect_messages(iterator) -> list[Any]:
    return [message async for message in iterator]


async def _process_message(
    ctx: BotContext, msg: Any, channel_title_for_path: str
) -> None:
    paths = await persist_message(
        ctx.db_conn,
        msg,
        channel_title_for_path,
        ctx.cfg.media_dir,
        busy_retries=ctx.cfg.sql_busy_retries,
        busy_sleep_secs=ctx.cfg.sql_busy_sleep,
    )
    policy = getattr(ctx.cfg, "historical_signal_policy", "store_only")
    if policy == "extract_no_execute":
        await process_signal_message(
            ctx, msg, image_paths=paths, allow_execution=False, historical=True
        )
        return

    signal_input = classify_signal_input(msg, paths)
    if not signal_input.supported:
        return
    created = await run_db(
        ctx.db_conn,
        enqueue_extraction_job,
        ctx.db_conn,
        int(msg.chat_id),
        int(msg.id),
        int(getattr(ctx.cfg, "extraction_max_attempts", 3)),
        historical=True,
        allow_execution=False,
        input_text=signal_input.text,
        image_paths=[str(path) for path in paths],
    )
    if created:
        await run_db(
            ctx.db_conn,
            mark_job,
            ctx.db_conn,
            int(msg.chat_id),
            int(msg.id),
            "historical_skipped",
            "HISTORICAL_SIGNAL_POLICY=store_only",
        )


async def _backfill_once(
    client,
    entity,
    ctx: BotContext,
    initial_limit: int,
    chat_id: int,
    channel_title_for_path: str,
) -> tuple[int, int]:
    checkpoint = await run_db(
        ctx.db_conn, get_channel_checkpoint, ctx.db_conn, chat_id
    )
    if checkpoint is None:
        migrated = await run_db(
            ctx.db_conn, latest_stored_message_id, ctx.db_conn, chat_id
        )
        if migrated is not None:
            await run_db(
                ctx.db_conn,
                save_channel_checkpoint,
                ctx.db_conn,
                chat_id,
                migrated,
                status="migrated",
            )
            checkpoint = {"last_message_id": migrated}

    processed = 0
    last_message_id = int(checkpoint["last_message_id"]) if checkpoint else 0
    if checkpoint is None:
        if initial_limit <= 0:
            return processed, last_message_id
        # The configured lookback is only the first-run seed. Once seeded, every
        # restart uses the durable high-water mark and has no outage-size limit.
        messages = await _collect_messages(
            client.iter_messages(entity, limit=initial_limit)
        )
        messages.sort(key=lambda message: int(message.id))
        pages = [messages]
    else:
        page_size = max(
            1,
            int(getattr(ctx.cfg, "backfill_page_size", initial_limit or 100)),
        )
        pages = []
        while True:
            page = await _collect_messages(
                client.iter_messages(
                    entity,
                    min_id=last_message_id,
                    limit=page_size,
                    reverse=True,
                )
            )
            page.sort(key=lambda message: int(message.id))
            if not page:
                break
            # Process before requesting the next page so recovery memory stays
            # bounded even after a long outage.
            for msg in page:
                if int(msg.id) <= last_message_id:
                    continue
                await _process_message(ctx, msg, channel_title_for_path)
                last_message_id = int(msg.id)
                processed += 1
                await run_db(
                    ctx.db_conn,
                    save_channel_checkpoint,
                    ctx.db_conn,
                    chat_id,
                    last_message_id,
                    status="catching_up",
                )
            if len(page) < page_size:
                break

    # First-run seed is processed oldest-first so a crash resumes from a valid
    # contiguous high-water mark within the selected initial window.
    for page in pages:
        for msg in page:
            if int(msg.id) <= last_message_id:
                continue
            await _process_message(ctx, msg, channel_title_for_path)
            last_message_id = int(msg.id)
            processed += 1
            await run_db(
                ctx.db_conn,
                save_channel_checkpoint,
                ctx.db_conn,
                chat_id,
                last_message_id,
                status="catching_up",
            )

    if last_message_id:
        await run_db(
            ctx.db_conn,
            save_channel_checkpoint,
            ctx.db_conn,
            chat_id,
            last_message_id,
            status="ready",
        )
    return processed, last_message_id


async def backfill_recent(
    client, entity, ctx: BotContext, limit: int
) -> BackfillResult:
    chat_id, channel_title_for_path, channel_title = _channel_details(entity, ctx)
    if not chat_id:
        logger.warning("No channel configuration found for entity %s", entity)
        return BackfillResult(True, 0, 0, 0)

    max_attempts = max(1, int(getattr(ctx.cfg, "backfill_max_attempts", 3)))
    retry_base = max(
        0.0, float(getattr(ctx.cfg, "backfill_retry_base_secs", 1.0))
    )
    max_backoff = max(0.0, float(getattr(ctx.cfg, "max_backoff_secs", 300)))
    policy = getattr(ctx.cfg, "backfill_failure_policy", "continue_live")
    total_processed = 0
    last_message_id = 0
    error = "backfill failed"
    logger.info(
        "Resuming channel backfill for '%s' from its durable checkpoint…",
        channel_title,
    )

    for attempt in range(1, max_attempts + 1):
        try:
            ingestion_lock = getattr(ctx, "ingestion_lock", None)
            if ingestion_lock is None:
                ingestion_lock = asyncio.Lock()
                ctx.ingestion_lock = ingestion_lock
            async with ingestion_lock:
                processed, last_message_id = await _backfill_once(
                    client,
                    entity,
                    ctx,
                    limit,
                    chat_id,
                    channel_title_for_path,
                )
            total_processed += processed
            logger.info(
                "Backfill complete for '%s' at message %d (%d processed).",
                channel_title,
                last_message_id,
                total_processed,
            )
            return BackfillResult(True, total_processed, last_message_id, attempt)
        except asyncio.CancelledError:
            raise
        except FloodWaitError as exc:
            seconds = int(getattr(exc, "seconds", 30))
            error = f"FloodWaitError: retry requested after {seconds}s"
            delay = min(max_backoff, float(seconds + 1))
        except Exception as exc:
            error = f"{exc.__class__.__name__}: {exc}"
            delay = min(
                max_backoff,
                retry_base * (2 ** min(attempt - 1, 6)) + random.uniform(0, 1),
            )

        if attempt < max_attempts:
            logger.warning(
                "Backfill attempt %d/%d for '%s' failed: %s. Retrying in %.1fs…",
                attempt,
                max_attempts,
                channel_title,
                error,
                delay,
            )
            await asyncio.sleep(delay)

    checkpoint = await run_db(
        ctx.db_conn, get_channel_checkpoint, ctx.db_conn, chat_id
    )
    if checkpoint is not None:
        last_message_id = int(checkpoint["last_message_id"])
    await run_db(
        ctx.db_conn,
        mark_channel_checkpoint_failed,
        ctx.db_conn,
        chat_id,
        error,
    )
    continued = policy == "continue_live"
    logger.error(
        "Backfill for '%s' exhausted %d attempts at message %d: %s. policy=%s",
        channel_title,
        max_attempts,
        last_message_id,
        error,
        policy,
    )
    result = BackfillResult(
        False,
        total_processed,
        last_message_id,
        max_attempts,
        continued_live=continued,
        error=error,
    )
    if not continued:
        raise BackfillStopped(f"backfill failed and policy=stop: {error}")
    return result
