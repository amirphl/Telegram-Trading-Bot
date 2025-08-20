import math
import time
from dataclasses import dataclass
from typing import Dict, Optional

import ccxt  # type: ignore

from configs.config import Config


SAFE_RETRY_ERRORS = (
    'NetworkError',
    'DDoSProtection',
    'RequestTimeout',
    'ExchangeNotAvailable',
    'RateLimitExceeded',
)


@dataclass
class ExecutionResult:
    success: bool
    order_id: Optional[str]
    status: str
    error: Optional[str]


class LBankClient:
    def __init__(self, cfg: Config) -> None:
        params: Dict[str, str] = {}
        if cfg.lbank_password:
            params['password'] = cfg.lbank_password
        self.exchange = ccxt.lbank({
            'apiKey': cfg.lbank_api_key or '',
            'secret': cfg.lbank_secret or '',
            'enableRateLimit': True,
            'options': params,
        })

    def _symbol_from_token(self, token: str, quote: str) -> str:
        token = token.upper().replace('/', '').replace('-', '').replace('_', '')
        quote = quote.upper()
        return f"{token}/{quote}"

    def fetch_price(self, symbol: str) -> Optional[float]:
        try:
            ticker = self.exchange.fetch_ticker(symbol)
            price = ticker.get('last') or ticker.get('close')
            return float(price) if price is not None else None
        except Exception:
            return None

    def market_order(self, symbol: str, side: str, amount: float) -> ExecutionResult:
        try:
            o = self.exchange.create_order(symbol=symbol, type='market', side=side, amount=amount)
            return ExecutionResult(success=True, order_id=str(o.get('id') or ''), status=str(o.get('status') or 'filled'), error=None)
        except Exception as e:
            name = e.__class__.__name__
            return ExecutionResult(success=False, order_id=None, status='error', error=f"{name}: {e}")


def is_safe_retry(error_msg: Optional[str]) -> bool:
    if not error_msg:
        return False
    return any(code in error_msg for code in SAFE_RETRY_ERRORS)


def compute_order_amount(notional_usdt: float, price: float, min_amount: float = 0.0) -> float:
    if price <= 0:
        return 0.0
    amount = notional_usdt / price
    if min_amount > 0.0:
        amount = max(amount, min_amount)
    return float(amount)


def price_deviation_too_high(detected_entry: Optional[float], current_price: Optional[float], max_pct: float) -> bool:
    if not detected_entry or not current_price or detected_entry <= 0 or current_price <= 0:
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


def execute_signal(cfg: Config, token: Optional[str], position_type: Optional[str], entry_price: Optional[float], leverage: Optional[float]) -> ExecutionResult:
    if is_anomalous_signal(token, position_type):
        return ExecutionResult(False, None, 'rejected_anomaly', 'Signal fields invalid or missing')

    client = LBankClient(cfg)
    symbol = client._symbol_from_token(token or '', cfg.order_quote)

    side = 'buy' if (position_type or '').lower() == 'long' else 'sell'

    # Fetch current price
    current_price = client.fetch_price(symbol)
    if price_deviation_too_high(entry_price, current_price, cfg.max_price_deviation_pct):
        return ExecutionResult(False, None, 'rejected_deviation', f'Price deviation too high: entry={entry_price}, current={current_price}')

    # Compute quantity by notional in quote currency
    ref_price = current_price or (entry_price or 0.0)
    if ref_price <= 0:
        return ExecutionResult(False, None, 'rejected_no_price', 'No valid price available')
    quantity = compute_order_amount(cfg.order_notional, ref_price)

    # Simple, safe retry for transient network errors only
    attempts = 0
    while True:
        attempts += 1
        result = client.market_order(symbol, side, quantity)
        if result.success:
            return result
        if is_safe_retry(result.error) and attempts < 3:
            time.sleep(1.0 * attempts)
            continue
        return result 