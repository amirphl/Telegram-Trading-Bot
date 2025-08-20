import asyncio
import random

from telethon.errors import (
    AuthKeyUnregisteredError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    rpcerrorlist,
)

from configs.config import Config
from internal.db.sqlite import connect_db, init_db
from api.telegram.client import build_client
from api.telegram.utils import resolve_channel
from internal.services.heartbeat import heartbeat_task
from internal.services.backfill import backfill_recent
from internal.types.context import BotContext


async def run_forever(cfg: Config):
    """
    Keep the client alive forever. Reconnect on any error with exponential backoff.
    """
    db_conn = connect_db(cfg.db_path)
    init_db(db_conn)

    client = build_client(cfg)

    ctx = BotContext(
        db_conn=db_conn,
        target_id=None,
        channel_title_for_path="channel",
        cfg=cfg,
    )

    # Register handlers once; they read runtime state from ctx
    from api.telegram.handlers import register_handlers

    register_handlers(client, ctx)

    attempts = 0
    while True:
        try:
            print("[*] Connecting (or re-connecting)…")
            await client.connect()

            if not await client.is_user_authorized():
                print("[*] Authorizing… (enter your phone/code/2FA)")
                await client.start()

            entity = await resolve_channel(client, cfg)
            ctx.target_id = entity.id
            ctx.channel_title_for_path = (
                getattr(entity, "title", cfg.channel_title) or "channel"
            ).replace(" ", "_")

            print(
                f"[*] Watching channel: {getattr(entity, 'title', 'Unknown')} (id={entity.id})"
            )
            print(f"[*] DB: {cfg.db_path} | Media: {cfg.media_dir}")

            await backfill_recent(client, entity, ctx, cfg.backfill)

            hb = asyncio.create_task(heartbeat_task(client, cfg.heartbeat_secs))

            await client.run_until_disconnected()

            hb.cancel()
            try:
                await hb
            except asyncio.CancelledError:
                pass
            raise ConnectionError("Disconnected")

        except (AuthKeyUnregisteredError, PhoneCodeExpiredError, PhoneCodeInvalidError) as e:
            print(
                f"[!] Auth error: {e}. You likely need to re-run interactively to re-login."
            )
            attempts += 1
        except rpcerrorlist.FloodWaitError as e:
            wait = int(getattr(e, "seconds", 60)) + 1
            print(f"[!] FloodWait: sleeping {wait}s before reconnect…")
            await asyncio.sleep(wait)
            attempts = 0
            continue
        except Exception as e:
            attempts += 1
            backoff = min(
                cfg.max_backoff_secs, (2 ** min(attempts, 6)) + random.uniform(0, 1)
            )
            print(f"[!] Connection error: {e}. Reconnecting in {int(backoff)}s…")
            await asyncio.sleep(backoff)
            continue
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass 