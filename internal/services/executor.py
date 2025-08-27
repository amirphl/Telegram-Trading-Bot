from typing import Optional
import logging

from internal.repositories.signals import TradeSignal
from internal.repositories.positions import (
    SubmittedPosition,
    upsert_submitted_position,
    update_position_status,
)
from internal.services.exchange import ExecutionResult, execute_signal
from internal.services.exchange_xt import XTClient
from internal.services.exchange_bitunix import BitunixClient
from internal.services.order_sizing import determine_order_quantity
from configs.config import Config


logger = logging.getLogger(__name__)


def _dry_run_symbol(cfg: Config, token: Optional[str]) -> str:
    t = (token or "").upper()
    if cfg.exchange == "bitunix":
        return BitunixClient(cfg).swap_symbol(t, cfg.order_quote)
    return XTClient(cfg).swap_symbol(t, cfg.order_quote)


def submit_position_if_enabled(
    cfg: Config, conn, sig: TradeSignal
) -> Optional[ExecutionResult]:
    if not cfg.enable_auto_execution:
        # Dry-run: log the order we would submit
        side = "buy" if (sig.position_type or "").lower() == "long" else "sell"
        symbol = _dry_run_symbol(cfg, sig.token)
        quantity = 0.0
        try:
            quantity = determine_order_quantity(cfg, sig.token, sig.entry_price) or 0.0
        except Exception:
            quantity = 0.0
        logger.info(
            "[DRY-RUN] Auto-exec disabled; would submit order: exchange=%s symbol=%s side=%s leverage=%s entry_price=%s quantity=%.8f stop_losses=%s take_profits=%s order_type=%s",
            cfg.exchange,
            symbol,
            side,
            str(sig.leverage),
            str(sig.entry_price),
            quantity,
            sig.stop_losses,
            sig.take_profits,
            "market",
        )
        return None

    # Validate credentials for chosen exchange
    if cfg.exchange == "xt":
        if not (cfg.xt_api_key and cfg.xt_secret):
            update_position_status(
                conn,
                chat_id=sig.chat_id,
                message_id=sig.message_id,
                status="rejected_config",
                error="XT credentials not configured",
                busy_retries=10,
                busy_sleep_secs=0.2,
            )
            return None
    elif cfg.exchange == "bitunix":
        if not (cfg.bitunix_api_key and cfg.bitunix_secret):
            update_position_status(
                conn,
                chat_id=sig.chat_id,
                message_id=sig.message_id,
                status="rejected_config",
                error="Bitunix credentials not configured",
                busy_retries=10,
                busy_sleep_secs=0.2,
            )
            return None
    else:
        update_position_status(
            conn,
            chat_id=sig.chat_id,
            message_id=sig.message_id,
            status="rejected_config",
            error=f"Unknown exchange: {cfg.exchange}",
            busy_retries=10,
            busy_sleep_secs=0.2,
        )
        return None

    # Pre-create record as pending
    symbol_for_record = (
        f"{(sig.token or '').upper()}/{cfg.order_quote}"
        if cfg.exchange == "xt"
        else f"{(sig.token or '').upper()}{cfg.order_quote}"
    )
    upsert_submitted_position(
        conn,
        SubmittedPosition(
            chat_id=sig.chat_id,
            message_id=sig.message_id,
            symbol=symbol_for_record,
            side=("buy" if (sig.position_type or "").lower() == "long" else "sell"),
            quantity=0.0,
            price=sig.entry_price,
            leverage=sig.leverage,
            order_id=None,
            status="pending",
            error=None,
        ),
        busy_retries=10,
        busy_sleep_secs=0.2,
    )

    quantity = determine_order_quantity(cfg, sig.token, sig.entry_price)
    if quantity <= 0:
        update_position_status(
            conn,
            chat_id=sig.chat_id,
            message_id=sig.message_id,
            status="rejected_no_quantity",
            error="Unable to compute order quantity",
            busy_retries=10,
            busy_sleep_secs=0.2,
        )
        return None

    order_type = "market"

    if cfg.exchange == "xt":
        client = XTClient(cfg)
        result = execute_signal(
            cfg,
            client,
            sig.token,
            sig.position_type,
            sig.entry_price,
            sig.leverage,
            precomputed_quantity=quantity,
            stop_losses=sig.stop_losses,
            take_profits=sig.take_profits,
            order_type=order_type,
        )
    else:
        # Bitunix path
        try:
            client = BitunixClient(cfg)
            side = "BUY" if (sig.position_type or "").lower() == "long" else "SELL"
            symbol_pair = client.swap_symbol(sig.token or "", cfg.order_quote)

            tp_price = None
            sl_price = None
            if sig.take_profits:
                try:
                    if (sig.position_type or "").lower() == "long":
                        tp_price = float(min(sig.take_profits))
                    else:
                        tp_price = float(max(sig.take_profits))
                except Exception:
                    tp_price = None
            if sig.stop_losses:
                try:
                    if (sig.position_type or "").lower() == "long":
                        sl_price = float(max(sig.stop_losses))
                    else:
                        sl_price = float(min(sig.stop_losses))
                except Exception:
                    sl_price = None

            params = {
                "reduceOnly": False,
                "tpPrice": tp_price,
                "tpStopType": "LAST_PRICE" if tp_price is not None else None,
                "tpOrderType": "LIMIT" if tp_price is not None else None,
                "tpOrderPrice": tp_price if tp_price is not None else None,
                "slPrice": sl_price,
                "slStopType": "LAST_PRICE" if sl_price is not None else None,
                "slOrderType": "MARKET" if sl_price is not None else None,
                "slOrderPrice": None,
            }
            if sig.leverage is not None:
                params["leverage"] = int(sig.leverage)

            if order_type == "market":
                r = client.market_order(
                    symbol_pair, side=side, amount=quantity, params=params
                )
            else:
                r = client.limit_order(
                    symbol_pair,
                    side=side,
                    amount=quantity,
                    price=float(sig.entry_price) if sig.entry_price else None,
                    params=params,
                )
            result = r
        except Exception as e:
            logger.error("Bitunix order error: %s", e)
            r = ExecutionResult(False, None, "error", str(e))
            result = r

    if result and getattr(result, "success", False):
        update_position_status(
            conn,
            chat_id=sig.chat_id,
            message_id=sig.message_id,
            status="submitted",
            error=None,
            busy_retries=10,
            busy_sleep_secs=0.2,
        )
    elif result:
        update_position_status(
            conn,
            chat_id=sig.chat_id,
            message_id=sig.message_id,
            status=getattr(result, "status", "error"),
            error=getattr(result, "error", "unknown"),
            busy_retries=10,
            busy_sleep_secs=0.2,
        )

    return result
