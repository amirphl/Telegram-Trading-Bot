from telethon import events
import logging

from internal.repositories.messages import persist_message
from internal.types.context import BotContext, SignalDiscoveryPolicy
from internal.services.signal_extraction import (
    process_windowed_signal_extraction,
    process_single_message_signal_extraction,
)


logger = logging.getLogger(__name__)


def register_handlers(client, ctx: BotContext) -> None:
    @client.on(events.NewMessage())
    async def on_new_message(event):
        try:
            chat_id = event.chat_id

            # Check if this channel is being monitored
            if not ctx.is_channel_monitored(chat_id):
                logger.warning("Channel %s is not being monitored", chat_id)
                return

            channel_config = ctx.get_channel_config(chat_id)
            if not channel_config:
                logger.warning(
                    "No channel configuration found for chat_id: %s", chat_id
                )
                return

            # Persist the message to database
            saved_paths = await persist_message(
                ctx.db_conn,
                event.message,
                channel_config.channel_title.replace(" ", "_"),
                ctx.cfg.media_dir,
                busy_retries=ctx.cfg.sql_busy_retries,
                busy_sleep_secs=ctx.cfg.sql_busy_sleep,
            )
            logger.info(
                "Upserted message %s from channel '%s'",
                event.message.id,
                channel_config.channel_title,
            )

            # Process signal extraction based on channel policy
            if channel_config.policy == SignalDiscoveryPolicy.SINGLE_MESSAGE:
                await process_single_message_signal_extraction(
                    ctx, channel_config, event.message, image_paths=saved_paths
                )
            elif channel_config.policy == SignalDiscoveryPolicy.WINDOWED_MESSAGES:
                await process_windowed_signal_extraction(
                    ctx, channel_config, event.message, image_paths=saved_paths
                )
            else:
                logger.warning(
                    "Unknown signal discovery policy: %s", channel_config.policy
                )

        except Exception as e:
            logger.exception("Handler error: %s", e)
