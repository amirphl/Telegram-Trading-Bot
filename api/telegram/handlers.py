import asyncio
import logging

from telethon import events

from internal.repositories.checkpoints import advance_live_checkpoint
from internal.repositories.messages import (
    MediaPolicy,
    archive_message_revision,
    delete_message_and_media,
    delete_unsubmitted_signal,
    inspect_telegram_image,
    persist_message,
    record_processing_status,
)
from internal.services.blocking import run_db
from internal.services.signal_extraction import (
    process_signal_message,
    process_single_message_signal_extraction,
    process_windowed_signal_extraction,
)
from internal.types.context import BotContext, SignalDiscoveryPolicy


logger = logging.getLogger(__name__)
_unmonitored_logged: set[int] = set()


def register_handlers(client, ctx: BotContext) -> None:
    ingestion_lock = getattr(ctx, "ingestion_lock", None) or asyncio.Lock()
    ctx.ingestion_lock = ingestion_lock

    def channel_config_for(event):
        chat_id = getattr(event, "chat_id", None)
        get_channel_config = getattr(ctx, "get_channel_config", None)
        if callable(get_channel_config):
            channel_config = get_channel_config(chat_id)
            if channel_config is not None and channel_config.enabled:
                return channel_config
            return None
        if getattr(ctx, "target_id", None) == chat_id:
            return False  # A valid legacy target without a ChannelConfig.
        return None

    def is_target(event) -> bool:
        channel_config = channel_config_for(event)
        if channel_config is None:
            chat_id = getattr(event, "chat_id", None)
            if chat_id not in _unmonitored_logged:
                logger.info("Ignoring unmonitored channel %s", chat_id)
                _unmonitored_logged.add(chat_id)
            return False
        return True

    def channel_path(event) -> str:
        channel_config = channel_config_for(event)
        if channel_config:
            return channel_config.channel_title.replace(" ", "_")
        return getattr(ctx, "channel_title_for_path", "channel")

    async def persist(msg, *, replace_media=False, skip_media_reason=None):
        return await persist_message(
            ctx.db_conn,
            msg,
            channel_path(msg),
            ctx.cfg.media_dir,
            busy_retries=ctx.cfg.sql_busy_retries,
            busy_sleep_secs=ctx.cfg.sql_busy_sleep,
            media_policy=MediaPolicy.from_config(ctx.cfg),
            replace_media=replace_media,
            skip_media_reason=skip_media_reason,
        )

    async def checkpoint(messages) -> None:
        messages = list(messages)
        ids = [int(message.id) for message in messages]
        if not ids or not hasattr(ctx.db_conn, "execute"):
            return
        chat_id = getattr(messages[0], "chat_id", None)
        if chat_id is None:
            return
        await run_db(
            ctx.db_conn,
            advance_live_checkpoint,
            ctx.db_conn,
            int(chat_id),
            max(ids),
        )

    async def process_new_message(message, saved_paths, channel_config) -> None:
        if not channel_config:
            await process_signal_message(ctx, message, image_paths=saved_paths)
        elif channel_config.policy == SignalDiscoveryPolicy.SINGLE_MESSAGE:
            await process_single_message_signal_extraction(
                ctx, channel_config, message, image_paths=saved_paths
            )
        elif channel_config.policy == SignalDiscoveryPolicy.WINDOWED_MESSAGES:
            await process_windowed_signal_extraction(
                ctx, channel_config, message, image_paths=saved_paths
            )
        else:
            logger.warning(
                "Unknown signal discovery policy for channel %s: %s",
                message.chat_id,
                channel_config.policy,
            )

    @client.on(events.NewMessage())
    async def on_new_message(event):
        try:
            if not is_target(event):
                return
            if getattr(event.message, "grouped_id", None):
                # Album events are processed once by on_album below.
                return
            async with ingestion_lock:
                channel_config = channel_config_for(event)
                saved_paths = await persist(event.message)
                logger.info("Upserted message %s", event.message.id)
                await process_new_message(event.message, saved_paths, channel_config)
                await checkpoint((event.message,))
        except Exception:
            logger.exception("New-message handler failed")

    @client.on(events.Album())
    async def on_album(event):
        try:
            if not is_target(event):
                return
            async with ingestion_lock:
                messages = list(event.messages or [])
                if not messages:
                    return
                policy = MediaPolicy.from_config(ctx.cfg)
                media_info = [inspect_telegram_image(msg, policy) for msg in messages]
                supported = [info for info in media_info if info.supported]
                declared_bytes = sum(info.file_size or 0 for info in supported)
                rejection = None
                unsupported = [info for info in media_info if not info.supported]
                if unsupported:
                    reasons = ",".join(
                        sorted({info.reason or "unknown" for info in unsupported})
                    )
                    rejection = f"album_contains_unsupported_media:{reasons}"
                elif len(supported) > policy.max_images:
                    rejection = (
                        f"album_image_count_exceeded:{len(supported)}>{policy.max_images}"
                    )
                elif declared_bytes > policy.max_total_bytes:
                    rejection = (
                        f"album_total_bytes_exceeded:{declared_bytes}>"
                        f"{policy.max_total_bytes}"
                    )

                image_paths = []
                for msg in messages:
                    image_paths.extend(await persist(msg, skip_media_reason=rejection))
                if rejection:
                    await checkpoint(messages)
                    return
                if len(image_paths) != len(supported):
                    rejection = (
                        f"album_incomplete_download:{len(image_paths)}/{len(supported)}"
                    )
                else:
                    actual_bytes = sum(path.stat().st_size for path in image_paths)
                    if actual_bytes > policy.max_total_bytes:
                        rejection = (
                            f"album_total_bytes_exceeded:{actual_bytes}>"
                            f"{policy.max_total_bytes}"
                        )
                if rejection:
                    for msg in messages:
                        await run_db(
                            ctx.db_conn,
                            record_processing_status,
                            ctx.db_conn,
                            int(msg.chat_id),
                            int(msg.id),
                            "rejected_signal",
                            rejection,
                            ctx.cfg.sql_busy_retries,
                            ctx.cfg.sql_busy_sleep,
                        )
                    await checkpoint(messages)
                    return

                captions = [
                    str(msg.message).strip()
                    for msg in messages
                    if getattr(msg, "message", None)
                ]
                combined_text = "\n\n".join(dict.fromkeys(captions))
                anchor = next(
                    (msg for msg in messages if getattr(msg, "message", None)),
                    messages[0],
                )
                await process_signal_message(
                    ctx,
                    anchor,
                    image_paths=image_paths,
                    text_override=combined_text,
                )
                for msg in messages:
                    if msg.id == anchor.id:
                        continue
                    await run_db(
                        ctx.db_conn,
                        record_processing_status,
                        ctx.db_conn,
                        int(msg.chat_id),
                        int(msg.id),
                        "album_member",
                        f"processed_with_anchor_message:{anchor.id}",
                        ctx.cfg.sql_busy_retries,
                        ctx.cfg.sql_busy_sleep,
                    )
                await checkpoint(messages)
        except Exception:
            logger.exception("Album handler failed")

    @client.on(events.MessageEdited())
    async def on_message_edited(event):
        try:
            if not is_target(event):
                return
            await run_db(
                ctx.db_conn,
                archive_message_revision,
                ctx.db_conn,
                int(event.message.chat_id),
                int(event.message.id),
                ctx.cfg.sql_busy_retries,
                ctx.cfg.sql_busy_sleep,
            )
            saved_paths = await persist(event.message, replace_media=True)
            can_replace_signal = await run_db(
                ctx.db_conn,
                delete_unsubmitted_signal,
                ctx.db_conn,
                int(event.message.chat_id),
                int(event.message.id),
                ctx.cfg.sql_busy_retries,
                ctx.cfg.sql_busy_sleep,
            )
            if not can_replace_signal:
                await run_db(
                    ctx.db_conn,
                    record_processing_status,
                    ctx.db_conn,
                    int(event.message.chat_id),
                    int(event.message.id),
                    "edited_after_order_review_required",
                    "source changed after order creation; original signal and order retained",
                    ctx.cfg.sql_busy_retries,
                    ctx.cfg.sql_busy_sleep,
                )
                return
            if getattr(event.message, "grouped_id", None):
                await run_db(
                    ctx.db_conn,
                    record_processing_status,
                    ctx.db_conn,
                    int(event.message.chat_id),
                    int(event.message.id),
                    "edited_album_review_required",
                    "album member changed; automatic execution is disabled for edits",
                    ctx.cfg.sql_busy_retries,
                    ctx.cfg.sql_busy_sleep,
                )
                return
            await process_signal_message(
                ctx,
                event.message,
                image_paths=saved_paths,
                allow_execution=False,
                replace_existing=True,
            )
        except Exception:
            logger.exception("Edit handler failed")

    @client.on(events.MessageDeleted())
    async def on_message_deleted(event):
        try:
            if not is_target(event):
                return
            for message_id in event.deleted_ids:
                result = await run_db(
                    ctx.db_conn,
                    delete_message_and_media,
                    ctx.db_conn,
                    int(event.chat_id),
                    int(message_id),
                    ctx.cfg.media_dir,
                    ctx.cfg.sql_busy_retries,
                    ctx.cfg.sql_busy_sleep,
                )
                logger.info("Deleted message %s: %s", message_id, result)
        except Exception:
            logger.exception("Delete handler failed")
