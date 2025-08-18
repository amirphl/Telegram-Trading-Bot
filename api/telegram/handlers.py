from telethon import events

from internal.repositories.messages import persist_message
from internal.types.context import BotContext
from internal.services.signal_extraction import process_signal_if_image_only


def register_handlers(client, ctx: BotContext) -> None:
    @client.on(events.NewMessage())
    async def on_new_message(event):
        try:
            if ctx.target_id is None:
                return
            if event.chat_id != ctx.target_id:
                return
            saved_paths = await persist_message(
                ctx.db_conn,
                event.message,
                ctx.channel_title_for_path,
                ctx.cfg.media_dir,
                busy_retries=ctx.cfg.sql_busy_retries,
                busy_sleep_secs=ctx.cfg.sql_busy_sleep,
            )
            print(f"[+] Upserted message {event.message.id}")

            await process_signal_if_image_only(ctx, event.message, image_paths=saved_paths)
        except Exception as e:
            print(f"[!] Handler error: {e}") 