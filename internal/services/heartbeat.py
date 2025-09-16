import asyncio
import logging

from telethon import TelegramClient


logger = logging.getLogger(__name__)


class HeartbeatFailure(ConnectionError):
    pass


async def heartbeat_task(
    client: TelegramClient,
    interval_secs: int,
    failure_threshold: int = 3,
) -> None:
    """Raise after consecutive health failures so the runner can reconnect."""
    if failure_threshold <= 0:
        raise ValueError("heartbeat failure threshold must be positive")

    consecutive_failures = 0
    while True:
        try:
            await client.get_me()
            consecutive_failures = 0
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            consecutive_failures += 1
            logger.exception(
                "Heartbeat failure %s/%s (handled by run loop): %s",
                consecutive_failures,
                failure_threshold,
                exc,
            )
            if consecutive_failures >= failure_threshold:
                raise HeartbeatFailure(
                    f"heartbeat failed {consecutive_failures} consecutive times"
                ) from exc
        await asyncio.sleep(max(1, interval_secs))
