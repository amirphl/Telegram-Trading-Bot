import asyncio
import logging

from configs.config import load_config
from api.telegram.client import build_client


logger = logging.getLogger(__name__)


async def list_channels() -> None:
    cfg = load_config()
    client = build_client(cfg)

    await client.connect()
    if not await client.is_user_authorized():
        logger.info("Authorizing… (enter your phone/code/2FA)")
        await client.start()

    async for dialog in client.iter_dialogs():
        if dialog.is_channel:
            title = (dialog.name or "").strip()
            cid = getattr(getattr(dialog, "entity", None), "id", None)
            if title and cid is not None:
                print(f"{title}\t{cid}")

    await client.disconnect()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(list_channels())

