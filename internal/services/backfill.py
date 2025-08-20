import asyncio
import random

from telethon.errors import FloodWaitError

from internal.repositories.messages import persist_message
from internal.types.context import BotContext


async def backfill_recent(client, entity, ctx: BotContext, limit: int):
    if limit <= 0:
        return
    print(f"[*] Backfilling last {limit} messages…")
    attempts = 0
    while True:
        try:
            async for msg in client.iter_messages(entity, limit=limit):
                await persist_message(
                    ctx.db_conn,
                    msg,
                    ctx.channel_title_for_path,
                    ctx.cfg.media_dir,
                    busy_retries=ctx.cfg.sql_busy_retries,
                    busy_sleep_secs=ctx.cfg.sql_busy_sleep,
                )
            print("[*] Backfill complete.")
            return
        except FloodWaitError as e:
            wait = int(getattr(e, "seconds", 30)) + 1
            print(f"[!] FloodWait: sleeping {wait}s…")
            await asyncio.sleep(wait)
        except Exception as e:
            attempts += 1
            backoff = min(
                ctx.cfg.max_backoff_secs, (2 ** min(attempts, 6)) + random.uniform(0, 1)
            )
            print(f"[!] Backfill error: {e}. Retrying in {int(backoff)}s…")
            await asyncio.sleep(backoff) 