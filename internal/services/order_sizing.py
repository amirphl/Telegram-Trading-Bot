from typing import Optional
import logging

from configs.config import Config
from internal.services.exchange_xt import XTClient
from internal.services.exchange import compute_order_amount
from internal.services.exchange_bitunix import BitunixClient


logger = logging.getLogger(__name__)


def _symbol_pair_bitunix(token: str, quote: str) -> str:
    norm = BitunixClient._normalize_token_for_crypto(token)
    base = "".join(ch for ch in (norm or "").upper() if ch.isalnum())
    quote = (quote or "").upper()
    if base.endswith(quote):
        return base
    return f"{base}{quote}"


def determine_order_quantity(
    cfg: Config, token: Optional[str], entry_price: Optional[float]
) -> float:
    """
    Determine order quantity as 90% of available quote balance divided by current price

    Returns 0.0 if budget is less than $10 minimum.

    quantity = 0.9 * free_quote_balance / current_price
    """
    MIN_BUDGET_USD = 10.0

    if not token:
        logger.warning("Cannot determine order quantity: token is missing")
        return 0.0

    if cfg.exchange == "bitunix":
        client = BitunixClient(cfg)
        symbol_pair = _symbol_pair_bitunix(token, cfg.order_quote)
        current_price = client.fetch_price(symbol_pair) or entry_price
        if not current_price or current_price <= 0:
            logger.error(
                "Bitunix: Cannot determine order quantity: no valid price for symbol=%s",
                symbol_pair,
            )
            return 0.0
        free_quote = client.get_available_balance(cfg.order_quote)
        if free_quote is None or free_quote <= 0:
            logger.warning(
                "Bitunix: No available quote balance for %s", cfg.order_quote
            )
            return 0.0
        budget = free_quote * 0.9

        if budget < MIN_BUDGET_USD:
            logger.error(
                "Bitunix: Budget %.2f %s is below minimum threshold of $%.2f",
                budget,
                cfg.order_quote,
                MIN_BUDGET_USD,
            )
            return 0.0

        quantity = compute_order_amount(budget, current_price)
        logger.info(
            "Bitunix: Order sizing computed: symbol=%s price=%.8f budget=%.6f %s quantity=%.8f",
            symbol_pair,
            current_price,
            budget,
            cfg.order_quote,
            quantity,
        )
        return quantity
    else:
        # default to XT
        client = XTClient(cfg)
        symbol = client.swap_symbol(token, cfg.order_quote)
        current_price = client.fetch_price(symbol) or entry_price
        if not current_price or current_price <= 0:
            logger.error(
                "XT: Cannot determine order quantity: no valid price for symbol=%s",
                symbol,
            )
            return 0.0
        free_quote = client.get_available_balance(cfg.order_quote)
        if free_quote is None or free_quote <= 0:
            logger.warning("XT: No available quote balance for %s", cfg.order_quote)
            return 0.0

        budget = free_quote * 0.9

        if budget < MIN_BUDGET_USD:
            logger.error(
                "XT: Budget %.2f %s is below minimum threshold of $%.2f",
                budget,
                cfg.order_quote,
                MIN_BUDGET_USD,
            )
            return 0.0

        quantity = compute_order_amount(budget, current_price)
        logger.info(
            "XT: Order sizing computed: symbol=%s price=%.8f budget=%.6f %s quantity=%.8f",
            symbol,
            current_price,
            budget,
            cfg.order_quote,
            quantity,
        )
        return quantity
    return 0.0

