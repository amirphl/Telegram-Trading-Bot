import asyncio
import logging

from telethon import TelegramClient


logger = logging.getLogger(__name__)


async def heartbeat_task(client: TelegramClient, interval_secs: int):
    while True:
        try:
            await client.get_me()
        except Exception as e:
            logger.exception("Heartbeat error (handled by run loop): %s", e)
        await asyncio.sleep(interval_secs) 