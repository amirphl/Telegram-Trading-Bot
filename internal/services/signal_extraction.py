from pathlib import Path
from typing import List, Optional

from telethon.tl.custom.message import Message

from internal.repositories.messages import download_media_if_any
from internal.repositories.signals import TradeSignal, insert_trade_signal
from internal.services.openai_client import OpenAIExtractor
from internal.services.executor import submit_position_if_enabled
from internal.types.context import BotContext


def _is_image_only(msg: Message) -> bool:
    if not msg.media:
        return False
    text = (msg.message or '').strip()
    return len(text) == 0


async def process_signal_if_image_only(ctx: BotContext, msg: Message, image_paths: Optional[List[Path]] = None) -> None:
    if not _is_image_only(msg):
        return

    paths: List[Path] = list(image_paths or [])
    if not paths:
        for p in await download_media_if_any(msg, ctx.channel_title_for_path, ctx.cfg.media_dir):
            paths.append(p)

    extractor = OpenAIExtractor(ctx.cfg)
    result = extractor.extract_signal(msg.message or None, paths)
    if not result:
        return

    try:
        stop_losses = result.get('stop_losses') or []
        take_profits = result.get('take_profits') or []
        sig = TradeSignal(
            chat_id=msg.chat_id,
            message_id=msg.id,
            token=result.get('token'),
            position_type=result.get('position_type'),
            entry_price=(float(result['entry_price']) if result.get('entry_price') is not None else None),
            leverage=(float(result['leverage']) if result.get('leverage') is not None else None),
            stop_losses=[float(x) for x in stop_losses if x is not None],
            take_profits=[float(x) for x in take_profits if x is not None],
            model_name=ctx.cfg.openai_model,
        )
        insert_trade_signal(
            ctx.db_conn,
            sig,
            busy_retries=ctx.cfg.sql_busy_retries,
            busy_sleep_secs=ctx.cfg.sql_busy_sleep,
        )
        print(f"[+] Saved trade signal for message {msg.id}")

        submit_position_if_enabled(ctx.cfg, ctx.db_conn, sig)
    except Exception as e:
        print(f"[!] Failed to save trade signal for message {msg.id}: {e}")