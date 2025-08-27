from pathlib import Path
from typing import List, Optional, Dict, Any
import logging

from telethon.tl.custom.message import Message

from internal.repositories.messages import download_media_if_any
from internal.repositories.signals import TradeSignal, insert_trade_signal
from internal.services.openai_client import OpenAIExtractor
from internal.services.executor import submit_position_if_enabled
from internal.types.context import BotContext, ChannelConfig


logger = logging.getLogger(__name__)


def _get_recent_messages(
    conn,
    chat_id: int,
    window_size: int,
) -> List[Dict[str, Any]]:
    """Get the most recent N messages from a channel"""
    sql = """
    SELECT chat_id, message_id, date_utc, text, raw_json
    FROM messages 
    WHERE chat_id = ?
    ORDER BY date_utc DESC, message_id DESC
    LIMIT ?
    """

    cursor = conn.cursor()
    try:
        cursor.execute(sql, (chat_id, window_size))
        rows = cursor.fetchall()

        # Convert to list of dicts for easier handling
        messages = []
        for row in rows:
            messages.append(
                {
                    "chat_id": row[0],
                    "message_id": row[1],
                    "date_utc": row[2],
                    "text": row[3] or "",
                    "raw_json": row[4],
                }
            )

        # Return in chronological order (oldest first)
        return list(reversed(messages))
    finally:
        cursor.close()


def _combine_messages_for_analysis(messages: List[Dict[str, Any]]) -> str:
    """Combine multiple messages into a single text for analysis"""
    if not messages:
        return ""

    combined_parts = []
    for msg in messages:
        text = msg.get("text", "").strip()
        if text:
            # Add message metadata for context
            msg_id = msg.get("message_id", "unknown")
            date_str = msg.get("date_utc", "unknown")
            combined_parts.append(f"[Message {msg_id} at {date_str}]: {text}")

    return "\n\n".join(combined_parts)


async def process_windowed_signal_extraction(
    ctx: BotContext,
    channel_config: ChannelConfig,
    current_msg: Message,
    image_paths: Optional[List[Path]] = None,
) -> None:
    """
    Process signal extraction using a window of recent messages.
    This combines the current message with recent messages from the same channel.
    """
    chat_id = current_msg.chat_id

    # Get recent messages including the current one
    recent_messages = _get_recent_messages(
        ctx.db_conn,
        chat_id,
        channel_config.window_size,
    )

    if not recent_messages:
        logger.debug(
            "No recent messages found for windowed analysis in chat %s", chat_id
        )
        return

    # Combine messages for analysis
    combined_text = _combine_messages_for_analysis(recent_messages)

    # Download media from current message if any
    paths: List[Path] = list(image_paths or [])
    if not paths and current_msg.media:
        for p in await download_media_if_any(
            current_msg,
            channel_config.channel_title.replace(" ", "_"),
            ctx.cfg.media_dir,
        ):
            paths.append(p)

    # Extract signal using combined text and any images
    extractor = OpenAIExtractor(ctx.cfg)
    result = extractor.extract_signal(
        combined_text, paths, channel_prompt=channel_config.prompt
    )

    if not result:
        logger.info("No signal detected in windowed analysis for chat %s", chat_id)
        return

    try:
        stop_losses = result.get("stop_losses") or []
        take_profits = result.get("take_profits") or []

        sig = TradeSignal(
            chat_id=current_msg.chat_id,
            message_id=current_msg.id,  # Associate with the triggering message
            token=result.get("token"),
            position_type=result.get("position_type"),
            entry_price=(
                float(result["entry_price"])
                if result.get("entry_price") is not None
                else None
            ),
            leverage=(
                float(result["leverage"])
                if result.get("leverage") is not None
                else None
            ),
            stop_losses=[float(x) for x in stop_losses if x is not None],
            take_profits=[float(x) for x in take_profits if x is not None],
            model_name=f"{ctx.cfg.openai_model}_windowed_{channel_config.window_size}",
        )

        insert_trade_signal(
            ctx.db_conn,
            sig,
            busy_retries=ctx.cfg.sql_busy_retries,
            busy_sleep_secs=ctx.cfg.sql_busy_sleep,
        )

        logger.info(
            "Saved windowed trade signal for message %s (window size: %d)",
            current_msg.id,
            channel_config.window_size,
        )

        # Submit position if auto-execution is enabled
        res = submit_position_if_enabled(ctx.cfg, ctx.db_conn, sig)
        if res and not res.success:
            logger.error(
                "Auto-execution failed for msg %s: status=%s error=%s",
                current_msg.id,
                res.status,
                res.error,
            )

    except Exception as e:
        logger.exception(
            "Failed to save windowed trade signal for message %s: %s", current_msg.id, e
        )


async def process_single_message_signal_extraction(
    ctx: BotContext,
    channel_config: ChannelConfig,
    current_msg: Message,
    image_paths: Optional[List[Path]] = None,
) -> None:
    """
    Process signal extraction for a single message.
    This is the original behavior for single message analysis.
    """
    # Only process if message has media (images) and no text, or has text
    text = (current_msg.message or "").strip()
    has_media = current_msg.media is not None

    # Skip empty messages without media
    if not text and not has_media:
        return

    # Download media if any
    paths: List[Path] = list(image_paths or [])
    if not paths and has_media:
        for p in await download_media_if_any(
            current_msg,
            channel_config.channel_title.replace(" ", "_"),
            ctx.cfg.media_dir,
        ):
            paths.append(p)

    # Extract signal
    extractor = OpenAIExtractor(ctx.cfg)
    result = extractor.extract_signal(
        text or None, paths, channel_prompt=channel_config.prompt
    )

    if not result:
        logger.info(
            "No signal detected in single message analysis for message %s",
            current_msg.id,
        )
        return

    try:
        stop_losses = result.get("stop_losses") or []
        take_profits = result.get("take_profits") or []

        sig = TradeSignal(
            chat_id=current_msg.chat_id,
            message_id=current_msg.id,
            token=result.get("token"),
            position_type=result.get("position_type"),
            entry_price=(
                float(result["entry_price"])
                if result.get("entry_price") is not None
                else None
            ),
            leverage=(
                float(result["leverage"])
                if result.get("leverage") is not None
                else None
            ),
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

        logger.info("Saved single message trade signal for message %s", current_msg.id)

        # Submit position if auto-execution is enabled
        res = submit_position_if_enabled(ctx.cfg, ctx.db_conn, sig)
        if res and not res.success:
            logger.error(
                "Auto-execution failed for msg %s: status=%s error=%s",
                current_msg.id,
                res.status,
                res.error,
            )

    except Exception as e:
        logger.exception(
            "Failed to save single message trade signal for message %s: %s",
            current_msg.id,
            e,
        )
