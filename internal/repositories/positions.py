from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from internal.db.sqlite import sql_execute_with_retry
from pkg.serialization import dumps_json


@dataclass
class SubmittedPosition:
    chat_id: int
    message_id: int
    symbol: str
    side: str
    quantity: float
    price: float | None
    leverage: float | None
    order_id: str | None
    status: str
    error: str | None = None
    client_order_id: str | None = None
    market_type: str | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_position_event(
    conn,
    chat_id: int,
    message_id: int,
    status: str,
    detail: dict[str, Any] | None,
    busy_retries: int,
    busy_sleep_secs: float,
) -> None:
    sql_execute_with_retry(
        conn,
        """
        INSERT INTO position_events
          (chat_id, message_id, status, detail_json, created_at_utc)
        VALUES (?, ?, ?, ?, ?)
        """,
        (chat_id, message_id, status, dumps_json(detail) if detail is not None else None, _now()),
        busy_retries=busy_retries,
        busy_sleep_secs=busy_sleep_secs,
    )


def claim_position_submission(
    conn,
    position: SubmittedPosition,
    busy_retries: int,
    busy_sleep_secs: float,
) -> bool:
    now = _now()
    cursor = conn.execute(
        """
        INSERT INTO positions_submitted (
          chat_id, message_id, symbol, side, quantity, price, leverage,
          order_id, client_order_id, market_type, requested_quantity,
          status, error, created_at_utc, updated_at_utc
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(chat_id, message_id) DO NOTHING
        """,
        (
            position.chat_id,
            position.message_id,
            position.symbol,
            position.side,
            position.quantity,
            position.price,
            position.leverage,
            position.order_id,
            position.client_order_id,
            position.market_type,
            position.quantity,
            "claimed",
            None,
            now,
            now,
        ),
    )
    claimed = cursor.rowcount == 1
    if claimed:
        record_position_event(
            conn,
            position.chat_id,
            position.message_id,
            "claimed",
            {"client_order_id": position.client_order_id},
            busy_retries,
            busy_sleep_secs,
        )
    return claimed


def upsert_submitted_position(
    conn,
    sp: SubmittedPosition,
    busy_retries: int,
    busy_sleep_secs: float,
) -> None:
    """Compatibility writer. New submissions should use claim_position_submission."""
    now = _now()
    sql_execute_with_retry(
        conn,
        """
        INSERT INTO positions_submitted (
          chat_id, message_id, symbol, side, quantity, price, leverage, order_id,
          client_order_id, market_type, requested_quantity, status, error,
          created_at_utc, updated_at_utc
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(chat_id, message_id) DO UPDATE SET
          symbol=excluded.symbol,
          side=excluded.side,
          quantity=excluded.quantity,
          price=excluded.price,
          leverage=excluded.leverage,
          order_id=excluded.order_id,
          client_order_id=COALESCE(positions_submitted.client_order_id, excluded.client_order_id),
          market_type=excluded.market_type,
          requested_quantity=excluded.requested_quantity,
          status=excluded.status,
          error=excluded.error,
          updated_at_utc=excluded.updated_at_utc
        """,
        (
            sp.chat_id,
            sp.message_id,
            sp.symbol,
            sp.side,
            sp.quantity,
            sp.price,
            sp.leverage,
            sp.order_id,
            sp.client_order_id,
            sp.market_type,
            sp.quantity,
            sp.status,
            sp.error,
            now,
            now,
        ),
        busy_retries=busy_retries,
        busy_sleep_secs=busy_sleep_secs,
    )


def update_position_plan(
    conn,
    chat_id: int,
    message_id: int,
    plan,
    busy_retries: int,
    busy_sleep_secs: float,
) -> bool:
    now = _now()
    cursor = conn.execute(
        """
        UPDATE positions_submitted SET
          symbol=?, side=?, quantity=?, requested_quantity=?, price=?, leverage=?,
          market_type=?, price_source=?, price_timestamp_utc=?, price_deviation_pct=?,
          status='submitting', error=NULL, updated_at_utc=?
        WHERE chat_id=? AND message_id=? AND status='claimed'
        """,
        (
            plan.symbol,
            plan.side,
            plan.quantity,
            plan.quantity,
            plan.current_price,
            plan.leverage,
            plan.market_type,
            plan.price_source,
            plan.price_timestamp_utc,
            plan.price_deviation_pct,
            now,
            chat_id,
            message_id,
        ),
    )
    transitioned = cursor.rowcount == 1
    if not transitioned:
        return False
    record_position_event(
        conn,
        chat_id,
        message_id,
        "submitting",
        {
            "symbol": plan.symbol,
            "side": plan.side,
            "quantity": plan.quantity,
            "price": plan.current_price,
            "price_source": plan.price_source,
        },
        busy_retries,
        busy_sleep_secs,
    )
    return True


def save_execution_result(
    conn,
    chat_id: int,
    message_id: int,
    result,
    busy_retries: int,
    busy_sleep_secs: float,
) -> None:
    now = _now()
    sql_execute_with_retry(
        conn,
        """
        UPDATE positions_submitted SET
          symbol=COALESCE(?, symbol), side=COALESCE(?, side),
          quantity=COALESCE(?, quantity), requested_quantity=COALESCE(?, requested_quantity),
          filled_quantity=?, price=COALESCE(?, price), average_price=?, cost=?, fee_json=?,
          leverage=COALESCE(?, leverage), order_id=COALESCE(?, order_id),
          client_order_id=COALESCE(?, client_order_id), market_type=COALESCE(?, market_type),
          exchange_status=?, price_source=COALESCE(?, price_source),
          price_timestamp_utc=COALESCE(?, price_timestamp_utc),
          price_deviation_pct=COALESCE(?, price_deviation_pct), entry_order_raw_json=?,
          protective_orders_json=?, status=?, error=?,
          submitted_at_utc=CASE WHEN ? THEN COALESCE(submitted_at_utc, ?) ELSE submitted_at_utc END,
          reconciled_at_utc=CASE WHEN ? THEN ? ELSE reconciled_at_utc END,
          updated_at_utc=?
        WHERE chat_id=? AND message_id=?
        """,
        (
            result.symbol,
            result.side,
            result.requested_quantity,
            result.requested_quantity,
            result.filled_quantity,
            result.current_price,
            result.average_price,
            result.cost,
            dumps_json(result.fee) if result.fee is not None else None,
            result.leverage,
            result.order_id,
            result.client_order_id,
            result.market_type,
            result.exchange_status,
            result.price_source,
            result.price_timestamp_utc,
            result.price_deviation_pct,
            dumps_json(result.raw_order) if result.raw_order is not None else None,
            dumps_json(result.protective_orders),
            result.status,
            result.error,
            bool(result.order_id),
            now,
            result.reconciled,
            now,
            now,
            chat_id,
            message_id,
        ),
        busy_retries=busy_retries,
        busy_sleep_secs=busy_sleep_secs,
    )
    record_position_event(
        conn,
        chat_id,
        message_id,
        result.status,
        result.to_audit_dict(),
        busy_retries,
        busy_sleep_secs,
    )
    for order in result.protective_orders:
        upsert_protective_order(
            conn,
            chat_id,
            message_id,
            order,
            busy_retries,
            busy_sleep_secs,
        )


def upsert_protective_order(
    conn,
    chat_id: int,
    message_id: int,
    order: dict[str, Any],
    busy_retries: int,
    busy_sleep_secs: float,
) -> None:
    now = _now()
    sql_execute_with_retry(
        conn,
        """
        INSERT INTO protective_orders (
          chat_id, message_id, role, order_index, trigger_price, requested_quantity,
          client_order_id, order_id, exchange_status, status, error, raw_json,
          created_at_utc, updated_at_utc
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(chat_id, message_id, role, order_index) DO UPDATE SET
          order_id=COALESCE(excluded.order_id, protective_orders.order_id),
          exchange_status=excluded.exchange_status,
          status=excluded.status,
          error=excluded.error,
          raw_json=excluded.raw_json,
          updated_at_utc=excluded.updated_at_utc
        """,
        (
            chat_id,
            message_id,
            order["role"],
            order["index"],
            order["trigger_price"],
            order["quantity"],
            order["client_order_id"],
            order.get("order_id"),
            order.get("exchange_status"),
            order["status"],
            order.get("error"),
            dumps_json(order.get("raw")),
            now,
            now,
        ),
        busy_retries=busy_retries,
        busy_sleep_secs=busy_sleep_secs,
    )


def get_submitted_position(conn, chat_id: int, message_id: int) -> dict[str, Any] | None:
    cursor = conn.execute(
        "SELECT * FROM positions_submitted WHERE chat_id=? AND message_id=?",
        (chat_id, message_id),
    )
    row = cursor.fetchone()
    if not row:
        return None
    return dict(zip((column[0] for column in cursor.description), row))


def list_reconcilable_positions(conn) -> list[dict[str, Any]]:
    cursor = conn.execute(
        """
        SELECT * FROM positions_submitted
        WHERE status IN ('claimed', 'submitting', 'unknown_requires_reconciliation')
        ORDER BY created_at_utc
        """
    )
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def list_active_protective_orders(conn, chat_id: int, message_id: int) -> list[dict[str, Any]]:
    cursor = conn.execute(
        """
        SELECT * FROM protective_orders
        WHERE chat_id=? AND message_id=? AND status IN ('open', 'unknown')
        ORDER BY role, order_index
        """,
        (chat_id, message_id),
    )
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def list_all_active_protective_orders(conn) -> list[dict[str, Any]]:
    cursor = conn.execute(
        """
        SELECT p.*, s.symbol
        FROM protective_orders p
        JOIN positions_submitted s
          ON s.chat_id=p.chat_id AND s.message_id=p.message_id
        WHERE p.status IN ('open', 'unknown')
        ORDER BY p.chat_id, p.message_id, p.role, p.order_index
        """
    )
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def update_protective_order_state(
    conn,
    row_id: int,
    status: str,
    exchange_status: str | None,
    error: str | None,
    raw: dict[str, Any] | None,
    busy_retries: int,
    busy_sleep_secs: float,
) -> None:
    sql_execute_with_retry(
        conn,
        """
        UPDATE protective_orders SET
          status=?, exchange_status=?, error=?, raw_json=?, updated_at_utc=?
        WHERE id=?
        """,
        (
            status,
            exchange_status,
            error,
            dumps_json(raw) if raw is not None else None,
            _now(),
            row_id,
        ),
        busy_retries=busy_retries,
        busy_sleep_secs=busy_sleep_secs,
    )


def update_position_status(
    conn,
    chat_id: int,
    message_id: int,
    status: str,
    error: str | None,
    busy_retries: int,
    busy_sleep_secs: float,
) -> None:
    sql_execute_with_retry(
        conn,
        """
        UPDATE positions_submitted
        SET status=?, error=?, updated_at_utc=?
        WHERE chat_id=? AND message_id=?
        """,
        (status, error, _now(), chat_id, message_id),
        busy_retries=busy_retries,
        busy_sleep_secs=busy_sleep_secs,
    )
    record_position_event(
        conn,
        chat_id,
        message_id,
        status,
        {"error": error} if error else None,
        busy_retries,
        busy_sleep_secs,
    )
