import asyncio

from telethon import TelegramClient


async def heartbeat_task(client: TelegramClient, interval_secs: int):
    while True:
        try:
            await client.get_me()
        except Exception as e:
            print(f"[!] Heartbeat error (will be handled by run loop): {e}")
        await asyncio.sleep(interval_secs) 