from __future__ import annotations

from collections import defaultdict
import logging
from typing import Optional

from configs.config import Config, validate_execution_authorization
from internal.repositories.positions import (
    SubmittedPosition,
    claim_position_submission,
    get_submitted_position,
    list_all_active_protective_orders,
    list_reconcilable_positions,
    save_execution_result,
    upsert_submitted_position,
    update_position_plan,
    update_position_status,
    update_protective_order_state,
)
from internal.repositories.signals import TradeSignal
from internal.services.exchange import (
    ExecutionResult as LegacyExecutionResult,
    execute_signal,
)
from internal.services.exchange_bitunix import BitunixClient
from internal.services.exchange_lbank import (
    ExecutionPlan,
    ExecutionRejected,
    ExecutionResult,
    LBankClient,
    execute_plan,
    get_lbank_client,
    normalize_token,
    plan_signal,
    rejected_result,
    stable_client_order_id,
)
from internal.services.exchange_xt import XTClient
from internal.services.order_sizing import determine_order_quantity


logger = logging.getLogger(__name__)


def _dry_run_symbol(cfg: Config, token: Optional[str]) -> str:
    t = (token or "").upper()
    if cfg.exchange == "bitunix":
        return BitunixClient(cfg).swap_symbol(t, cfg.order_quote)
    return XTClient(cfg).swap_symbol(t, cfg.order_quote)


def _submit_legacy(
    cfg: Config, conn, sig: TradeSignal
) -> Optional[LegacyExecutionResult]:
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
            r = LegacyExecutionResult(False, None, "error", str(e))
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


def _existing_result(row: dict) -> ExecutionResult:
    return ExecutionResult(
        success=False,
        order_id=row.get("order_id"),
        client_order_id=row.get("client_order_id"),
        status="duplicate_ignored",
        error=f"submission already exists with status={row.get('status')}",
        symbol=row.get("symbol"),
        side=row.get("side"),
        market_type=row.get("market_type"),
        requested_quantity=row.get("requested_quantity"),
        filled_quantity=row.get("filled_quantity"),
        current_price=row.get("price"),
        average_price=row.get("average_price"),
        cost=row.get("cost"),
        exchange_status=row.get("exchange_status"),
        leverage=row.get("leverage"),
        price_source=row.get("price_source"),
        price_timestamp_utc=row.get("price_timestamp_utc"),
        price_deviation_pct=row.get("price_deviation_pct"),
    )


def _claim_candidate(cfg: Config, sig: TradeSignal, client_order_id: str) -> SubmittedPosition:
    try:
        base = normalize_token(sig.token, cfg.order_quote)
        symbol = f"{base}/{cfg.order_quote}"
        if cfg.execution_market_type == "swap":
            symbol = f"{symbol}:{cfg.order_quote}"
    except ExecutionRejected:
        symbol = f"INVALID/{cfg.order_quote}"
    direction = (sig.position_type or "").lower()
    side = "buy" if direction == "long" else "sell" if direction == "short" else "invalid"
    return SubmittedPosition(
        chat_id=sig.chat_id,
        message_id=sig.message_id,
        symbol=symbol,
        side=side,
        quantity=0.0,
        price=sig.entry_price,
        leverage=sig.leverage,
        order_id=None,
        client_order_id=client_order_id,
        market_type=cfg.execution_market_type,
        status="claimed",
    )


def _submit_lbank(
    cfg: Config,
    conn,
    sig: TradeSignal,
    *,
    client: LBankClient | None = None,
) -> ExecutionResult | None:
    if not cfg.enable_auto_execution:
        return None
    try:
        validate_execution_authorization(cfg)
    except ValueError as exc:
        return rejected_result("execution_not_authorized", str(exc))

    retries = cfg.sql_busy_retries
    sleep = cfg.sql_busy_sleep
    client_order_id = stable_client_order_id(sig.chat_id, sig.message_id)
    claimed = claim_position_submission(
        conn,
        _claim_candidate(cfg, sig, client_order_id),
        retries,
        sleep,
    )
    if not claimed:
        existing = get_submitted_position(conn, sig.chat_id, sig.message_id)
        return _existing_result(existing or {})

    try:
        client, plan = plan_signal(
            cfg,
            sig.token,
            sig.position_type,
            sig.entry_price,
            sig.leverage,
            sig.stop_losses,
            sig.take_profits,
            client_order_id,
            client=client,
        )
    except Exception as exc:
        result = rejected_result("validation_rejected", str(exc), client_order_id)
        save_execution_result(conn, sig.chat_id, sig.message_id, result, retries, sleep)
        return result

    transitioned = update_position_plan(
        conn, sig.chat_id, sig.message_id, plan, retries, sleep
    )
    if not transitioned:
        result = rejected_result(
            "submission_state_conflict",
            "claim no longer permits order submission",
            client_order_id,
            plan,
        )
        return result
    result = execute_plan(client, plan)
    save_execution_result(conn, sig.chat_id, sig.message_id, result, retries, sleep)
    return result


def submit_position_if_enabled(
    cfg: Config,
    conn,
    sig: TradeSignal,
    *,
    client: LBankClient | None = None,
) -> ExecutionResult | LegacyExecutionResult | None:
    """Submit through the configured exchange while retaining each backend's flow."""
    exchange = getattr(cfg, "exchange", "lbank")
    if exchange in {"xt", "bitunix"}:
        return _submit_legacy(cfg, conn, sig)
    if exchange == "lbank":
        return _submit_lbank(cfg, conn, sig, client=client)
    if not cfg.enable_auto_execution:
        return None
    return rejected_result("execution_not_authorized", f"Unknown exchange: {exchange}")


def _reconstructed_plan(row: dict, client: LBankClient) -> ExecutionPlan:
    markets = client.load_markets()
    market = markets.get(row["symbol"]) or {"symbol": row["symbol"]}
    return ExecutionPlan(
        symbol=row["symbol"],
        market_type=row.get("market_type") or client.cfg.execution_market_type,
        side=row["side"],
        quantity=float(row.get("requested_quantity") or row.get("quantity") or 0.0),
        current_price=float(row.get("price") or 0.0),
        leverage=float(row.get("leverage") or 1.0),
        client_order_id=row["client_order_id"],
        price_source=row.get("price_source") or "unknown",
        price_timestamp_utc=row.get("price_timestamp_utc") or "",
        price_deviation_pct=row.get("price_deviation_pct"),
        stop_losses=[],
        take_profits=[],
        market=market,
    )


def _reconciled_result(order: dict, plan: ExecutionPlan, protected: bool) -> ExecutionResult:
    status = "reconciled_entry_found"
    success = True
    error = None
    if not protected:
        status = "reconciled_entry_found_protection_review"
        success = False
        error = "entry exists but protective-order state cannot be proven safely"
    return ExecutionResult(
        success=success,
        order_id=str(order.get("id")) if order.get("id") is not None else None,
        client_order_id=str(order.get("clientOrderId") or plan.client_order_id),
        status=status,
        error=error,
        symbol=str(order.get("symbol") or plan.symbol),
        side=str(order.get("side") or plan.side),
        market_type=plan.market_type,
        requested_quantity=plan.quantity,
        filled_quantity=float(order["filled"]) if order.get("filled") is not None else None,
        current_price=plan.current_price,
        average_price=float(order["average"]) if order.get("average") is not None else None,
        cost=float(order["cost"]) if order.get("cost") is not None else None,
        fee=order.get("fees") or order.get("fee"),
        exchange_status=str(order.get("status") or "unknown"),
        leverage=plan.leverage,
        price_source=plan.price_source,
        price_timestamp_utc=plan.price_timestamp_utc,
        price_deviation_pct=plan.price_deviation_pct,
        raw_order=order,
        reconciled=True,
    )


def reconcile_pending_positions(
    cfg: Config,
    conn,
    *,
    client: LBankClient | None = None,
) -> dict[str, int]:
    rows = list_reconcilable_positions(conn)
    summary = {"checked": 0, "found": 0, "manual_review": 0, "abandoned": 0}
    if not rows:
        return summary
    client = client or get_lbank_client(cfg)
    retries = cfg.sql_busy_retries
    sleep = cfg.sql_busy_sleep
    for row in rows:
        summary["checked"] += 1
        if row["status"] == "claimed":
            update_position_status(
                conn,
                row["chat_id"],
                row["message_id"],
                "abandoned_before_submission",
                "startup recovery found a claim that never reached submission",
                retries,
                sleep,
            )
            summary["abandoned"] += 1
            continue
        order = client.find_order(
            row["symbol"], row["client_order_id"], row.get("order_id")
        )
        if order is None:
            update_position_status(
                conn,
                row["chat_id"],
                row["message_id"],
                "reconciliation_not_found_manual_review",
                "order was not found; automatic resubmission is forbidden",
                retries,
                sleep,
            )
            summary["manual_review"] += 1
            continue
        plan = _reconstructed_plan(row, client)
        protective_count = conn.execute(
            "SELECT COUNT(*) FROM protective_orders WHERE chat_id=? AND message_id=?",
            (row["chat_id"], row["message_id"]),
        ).fetchone()[0]
        protected = protective_count > 0 or not cfg.require_protective_orders
        result = _reconciled_result(order, plan, protected)
        save_execution_result(
            conn, row["chat_id"], row["message_id"], result, retries, sleep
        )
        summary["found"] += 1
        if not protected:
            summary["manual_review"] += 1
    return summary


def reconcile_protective_orders(
    cfg: Config,
    conn,
    *,
    client: LBankClient | None = None,
) -> dict[str, int]:
    rows = list_all_active_protective_orders(conn)
    summary = {"checked": 0, "updated": 0, "siblings_canceled": 0}
    if not rows:
        return summary
    client = client or get_lbank_client(cfg)
    retries = cfg.sql_busy_retries
    sleep = cfg.sql_busy_sleep
    grouped: dict[tuple[int, int], list[dict]] = defaultdict(list)
    for row in rows:
        grouped[(row["chat_id"], row["message_id"])].append(row)

    for group in grouped.values():
        triggered = None
        for row in group:
            summary["checked"] += 1
            order = client.find_order(
                row["symbol"], row["client_order_id"], row.get("order_id")
            )
            if order is None:
                continue
            exchange_status = str(order.get("status") or "unknown").lower()
            status = "closed" if exchange_status == "closed" else exchange_status
            update_protective_order_state(
                conn,
                row["id"],
                status,
                exchange_status,
                None,
                order,
                retries,
                sleep,
            )
            summary["updated"] += 1
            if status == "closed":
                triggered = row
        if triggered and (getattr(client.exchange, "has", {}) or {}).get("cancelOrder"):
            for sibling in group:
                if sibling["id"] == triggered["id"] or not sibling.get("order_id"):
                    continue
                try:
                    client.exchange.cancel_order(sibling["order_id"], sibling["symbol"])
                    update_protective_order_state(
                        conn,
                        sibling["id"],
                        "canceled",
                        "canceled",
                        None,
                        None,
                        retries,
                        sleep,
                    )
                    summary["siblings_canceled"] += 1
                except Exception as exc:
                    update_protective_order_state(
                        conn,
                        sibling["id"],
                        "cancel_failed_manual_review",
                        sibling.get("exchange_status"),
                        str(exc),
                        None,
                        retries,
                        sleep,
                    )
    return summary
