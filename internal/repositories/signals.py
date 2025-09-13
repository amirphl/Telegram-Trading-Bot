from dataclasses import dataclass
from datetime import datetime, timezone

from internal.db.sqlite import sql_execute_with_retry
from pkg.serialization import dumps_json


@dataclass
class TradeSignal:
    chat_id: int
    message_id: int
    token: str | None
    position_type: str | None  # "long" or "short"
    entry_price: float | None
    leverage: float | None
    stop_losses: list[float]
    take_profits: list[float]
    model_name: str | None


def insert_trade_signal(conn, sig: TradeSignal, busy_retries: int, busy_sleep_secs: float) -> None:
    sql = """
    INSERT INTO trade_signals (
      chat_id, message_id, token, position_type, entry_price, leverage,
      stop_losses_json, take_profits_json, model_name, created_at_utc
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(chat_id, message_id) DO UPDATE SET
      token=excluded.token,
      position_type=excluded.position_type,
      entry_price=excluded.entry_price,
      leverage=excluded.leverage,
      stop_losses_json=excluded.stop_losses_json,
      take_profits_json=excluded.take_profits_json,
      model_name=excluded.model_name
    ;
    """
    now = datetime.now(timezone.utc).isoformat()
    sql_execute_with_retry(
        conn,
        sql,
        (
            sig.chat_id,
            sig.message_id,
            sig.token,
            sig.position_type,
            sig.entry_price,
            sig.leverage,
            dumps_json(sig.stop_losses),
            dumps_json(sig.take_profits),
            sig.model_name,
            now,
        ),
        busy_retries=busy_retries,
        busy_sleep_secs=busy_sleep_secs,
    )
