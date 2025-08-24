import logging
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from configs.config import Config


logger = logging.getLogger(__name__)


SAFE_RETRY_ERRORS = (
    "NetworkError",
    "DDoSProtection",
    "RequestTimeout",
    "ExchangeNotAvailable",
    "RateLimitExceeded",
)


@dataclass
class ExecutionResult:
    success: bool
    order_id: Optional[str]
    status: str
    error: Optional[str]


def is_safe_retry(error_msg: Optional[str]) -> bool:
    if not error_msg:
        return False
    return any(code in error_msg for code in SAFE_RETRY_ERRORS)


def compute_order_amount(
    notional_usdt: float, price: float, min_amount: float = 0.0
) -> float:
    if price <= 0:
        return 0.0
    amount = notional_usdt / price
    if min_amount > 0.0:
        amount = max(amount, min_amount)
    return float(amount)


def price_deviation_too_high(
    detected_entry: Optional[float], current_price: Optional[float], max_pct: float
) -> bool:
    if (
        not detected_entry
        or not current_price
        or detected_entry <= 0
        or current_price <= 0
    ):
        return False
    dev = abs(current_price - detected_entry) / detected_entry
    return dev > max_pct


def is_anomalous_signal(token: Optional[str], position_type: Optional[str]) -> bool:
    if not token or not position_type:
        return True
    if position_type.lower() not in ("long", "short"):
        return True
    if len(token) < 2 or len(token) > 15:
        return True
    return False


def build_futures_order_params(
    leverage: Optional[float],
    stop_losses: Optional[List[float]],
    take_profits: Optional[List[float]],
) -> Dict:
    params: Dict = {}
    if leverage is not None:
        params["leverage"] = float(leverage)
    if stop_losses:
        try:
            params["stopLossPrice"] = float(stop_losses[0])
        except Exception:
            pass
    if take_profits:
        try:
            params["takeProfitPrice"] = float(take_profits[0])
        except Exception:
            pass
    return params


def execute_signal(
    cfg: Config,
    client,
    token: Optional[str],
    position_type: Optional[str],
    entry_price: Optional[float],
    leverage: Optional[float],
    precomputed_quantity: Optional[float] = None,
    stop_losses: Optional[List[float]] = None,
    take_profits: Optional[List[float]] = None,
    order_type: str = "market",
) -> ExecutionResult:
    if is_anomalous_signal(token, position_type):
        logger.warning(
            "Rejected anomalous signal token=%s position=%s",
            token,
            position_type,
        )
        return ExecutionResult(
            False, None, "rejected_anomaly", "Signal fields invalid or missing"
        )

    # Use swap symbol for futures
    symbol = client.swap_symbol(token or "", cfg.order_quote)

    side = "buy" if (position_type or "").lower() == "long" else "sell"

    # Fetch current price
    current_price = client.fetch_price(symbol)
    if price_deviation_too_high(
        entry_price, current_price, cfg.max_price_deviation_pct
    ):
        logger.warning(
            "Rejected due to price deviation symbol=%s entry=%s current=%s",
            symbol,
            entry_price,
            current_price,
        )
        return ExecutionResult(
            False,
            None,
            "rejected_deviation",
            f"Price deviation too high: entry={entry_price}, current={current_price}",
        )

    # Determine quantity
    ref_price = current_price or (entry_price or 0.0)
    if ref_price <= 0:
        logger.error("No valid price available for symbol=%s", symbol)
        return ExecutionResult(
            False, None, "rejected_no_price", "No valid price available"
        )

    quantity = (
        float(precomputed_quantity)
        if (precomputed_quantity and precomputed_quantity > 0)
        else compute_order_amount(cfg.order_notional, ref_price)
    )

    params = build_futures_order_params(leverage, stop_losses, take_profits)

    # Simple, safe retry for transient network errors only
    attempts = 0
    while True:
        attempts += 1
        if order_type == "limit" and entry_price and entry_price > 0:
            result = client.limit_order(
                symbol, side, quantity, float(entry_price), params=params
            )
        else:
            result = client.market_order(symbol, side, quantity, params=params)

        if result.success:
            return result
        if is_safe_retry(result.error) and attempts < 3:
            logger.warning(
                "Transient order error; retrying attempt=%d error=%s",
                attempts,
                result.error,
            )
            time.sleep(1.0 * attempts)
            continue
        logger.critical(
            "Order submission failed permanently; symbol=%s side=%s error=%s",
            symbol,
            side,
            result.error,
        )
        return result
