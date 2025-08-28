import asyncio
import random
import logging

from telethon.errors import (
    AuthKeyUnregisteredError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    rpcerrorlist,
)
from telethon.utils import get_peer_id

from configs.config import Config
from internal.db.sqlite import connect_db, init_db
from api.telegram.client import build_client
from api.telegram.utils import resolve_channel
from internal.services.heartbeat import heartbeat_task
from internal.services.backfill import backfill_recent
from internal.types.context import BotContext, ChannelConfig, SignalDiscoveryPolicy


logger = logging.getLogger(__name__)


async def _resolve_and_setup_channels(client, cfg: Config) -> dict:
    """Resolve all configured channels and return a mapping of chat_id -> ChannelConfig"""
    channels = {}

    for channel_policy_config in cfg.channels:
        try:
            # Create a temporary config object for resolve_channel function
            temp_cfg = type(
                "TempConfig",
                (),
                {
                    "channel_id": channel_policy_config.channel_id,
                    "channel_title": channel_policy_config.channel_title,
                },
            )()

            entity = await resolve_channel(client, temp_cfg)

            # Convert policy string to enum
            policy = SignalDiscoveryPolicy.SINGLE_MESSAGE
            if channel_policy_config.policy == "windowed_messages":
                policy = SignalDiscoveryPolicy.WINDOWED_MESSAGES

            channel_config = ChannelConfig(
                channel_id=channel_policy_config.channel_id,
                channel_title=getattr(
                    entity, "title", channel_policy_config.channel_title
                )
                or channel_policy_config.channel_title,
                policy=policy,
                window_size=channel_policy_config.window_size,
                enabled=channel_policy_config.enabled,
                prompt=getattr(channel_policy_config, "prompt", None),
            )

            peer_id = get_peer_id(entity)
            channels[peer_id] = channel_config

            logger.info(
                "Configured channel '%s' (peer_id=%s, policy=%s, window_size=%d)",
                channel_config.channel_title,
                peer_id,
                policy.value,
                channel_config.window_size,
            )

        except Exception as e:
            logger.error(
                "Failed to resolve channel '%s' (%s): %s",
                channel_policy_config.channel_title,
                channel_policy_config.channel_id,
                e,
            )
            continue

    if not channels:
        raise ValueError("No channels could be resolved successfully")

    return channels


async def _backfill_all_channels(
    client, channels: dict, ctx: BotContext, backfill_count: int
):
    """Backfill recent messages for all configured channels"""
    for chat_id, channel_config in channels.items():
        if not channel_config.enabled:
            continue

        try:
            # Resolve a real entity/peer for Telethon
            entity = await client.get_entity(chat_id)
            await backfill_recent(client, entity, ctx, backfill_count)
            logger.info(
                "Backfilled last %d messages for channel '%s'",
                backfill_count,
                channel_config.channel_title,
            )
        except Exception as e:
            logger.error(
                "Failed to backfill channel '%s': %s", channel_config.channel_title, e
            )


async def run_forever(cfg: Config):
    """
    Keep the client alive forever. Reconnect on any error with exponential backoff.
    """
    db_conn = connect_db(cfg.db_path)
    init_db(db_conn)

    client = build_client(cfg)

    # Initialize context with empty channels - will be populated after connection
    ctx = BotContext(
        db_conn=db_conn,
        channels={},
        cfg=cfg,
    )

    # Register handlers once; they read runtime state from ctx
    from api.telegram.handlers import register_handlers

    register_handlers(client, ctx)

    attempts = 0
    while True:
        try:
            logger.info("Connecting (or re-connecting)…")
            await client.connect()

            if not await client.is_user_authorized():
                logger.info("Authorizing… (enter your phone/code/2FA)")
                await client.start()

            # Resolve and setup all configured channels
            channels = await _resolve_and_setup_channels(client, cfg)
            ctx.channels = channels

            logger.info("Successfully configured %d channels", len(channels))
            logger.info("DB: %s | Media: %s", cfg.db_path, cfg.media_dir)

            # Backfill recent messages for all channels
            await _backfill_all_channels(client, channels, ctx, cfg.backfill)

            hb = asyncio.create_task(heartbeat_task(client, cfg.heartbeat_secs))

            await client.run_until_disconnected()

            hb.cancel()
            try:
                await hb
            except asyncio.CancelledError:
                pass
            logger.critical("Telegram client disconnected")
            raise ConnectionError("Disconnected")

        except (
            AuthKeyUnregisteredError,
            PhoneCodeExpiredError,
            PhoneCodeInvalidError,
        ) as e:
            logger.error(
                "Auth error: %s. You likely need to re-run interactively to re-login.",
                e,
            )
            attempts += 1
        except rpcerrorlist.FloodWaitError as e:
            wait = int(getattr(e, "seconds", 60)) + 1
            logger.warning("FloodWait: sleeping %ds before reconnect…", wait)
            await asyncio.sleep(wait)
            attempts = 0
            continue
        except Exception as e:
            attempts += 1
            backoff = min(
                cfg.max_backoff_secs, (2 ** min(attempts, 6)) + random.uniform(0, 1)
            )
            logger.error("Connection error: %s. Reconnecting in %ds…", e, int(backoff))
            await asyncio.sleep(backoff)
            continue
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass
