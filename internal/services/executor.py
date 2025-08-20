from typing import Optional

from internal.repositories.signals import TradeSignal
from internal.repositories.positions import SubmittedPosition, upsert_submitted_position, update_position_status
from internal.services.exchange_lbank import execute_signal, ExecutionResult
from configs.config import Config


def submit_position_if_enabled(cfg: Config, conn, sig: TradeSignal) -> Optional[ExecutionResult]:
    if not cfg.enable_auto_execution:
        return None

    # Pre-create record as pending
    upsert_submitted_position(
        conn,
        SubmittedPosition(
            chat_id=sig.chat_id,
            message_id=sig.message_id,
            symbol=f"{(sig.token or '').upper()}/{cfg.order_quote}",
            side=('buy' if (sig.position_type or '').lower() == 'long' else 'sell'),
            quantity=0.0,
            price=sig.entry_price,
            leverage=sig.leverage,
            order_id=None,
            status='pending',
            error=None,
        ),
        busy_retries=10,
        busy_sleep_secs=0.2,
    )

    result = execute_signal(cfg, sig.token, sig.position_type, sig.entry_price, sig.leverage)

    if result.success:
        update_position_status(
            conn,
            chat_id=sig.chat_id,
            message_id=sig.message_id,
            status='submitted',
            error=None,
            busy_retries=10,
            busy_sleep_secs=0.2,
        )
    else:
        update_position_status(
            conn,
            chat_id=sig.chat_id,
            message_id=sig.message_id,
            status=result.status,
            error=result.error,
            busy_retries=10,
            busy_sleep_secs=0.2,
        )

    return result 