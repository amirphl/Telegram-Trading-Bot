import asyncio
import random
import logging

from telethon.errors import FloodWaitError
from telethon.utils import get_peer_id

from internal.repositories.messages import persist_message
from internal.types.context import BotContext


logger = logging.getLogger(__name__)


async def backfill_recent(client, entity, ctx: BotContext, limit: int):
    if limit <= 0:
        return

    # Get channel configuration for this entity
    peer_id = get_peer_id(entity)
    channel_config = ctx.get_channel_config(peer_id)
    if not channel_config:
        logger.warning("No channel configuration found for entity %s", peer_id)
        return

    channel_title_for_path = channel_config.channel_title.replace(" ", "_")

    logger.info(
        "Backfilling last %d messages for channel '%s'…",
        limit,
        channel_config.channel_title,
    )
    attempts = 0
    while True:
        try:
            async for msg in client.iter_messages(entity, limit=limit):
                await persist_message(
                    ctx.db_conn,
                    msg,
                    channel_title_for_path,
                    ctx.cfg.media_dir,
                    busy_retries=ctx.cfg.sql_busy_retries,
                    busy_sleep_secs=ctx.cfg.sql_busy_sleep,
                )
            logger.info(
                "Backfill complete for channel '%s'.", channel_config.channel_title
            )
            return
        except FloodWaitError as e:
            wait = int(getattr(e, "seconds", 30)) + 1
            logger.warning("FloodWait during backfill: sleeping %ds…", wait)
            await asyncio.sleep(wait)
        except Exception as e:
            attempts += 1
            backoff = min(
                ctx.cfg.max_backoff_secs, (2 ** min(attempts, 6)) + random.uniform(0, 1)
            )
            logger.error(
                "Backfill error for channel '%s': %s. Retrying in %ds…",
                channel_config.channel_title,
                e,
                int(backoff),
            )
            await asyncio.sleep(backoff)
