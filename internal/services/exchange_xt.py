import time
from dataclasses import dataclass
from typing import Dict, Optional, List
import logging

import ccxt  # type: ignore

from configs.config import Config


logger = logging.getLogger(__name__)


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


class XTClient:
    def __init__(self, cfg: Config) -> None:
        params: Dict[str, str] = {}
        if cfg.xt_password:
            params['password'] = cfg.xt_password
        self.exchange = ccxt.xt({
            'apiKey': cfg.xt_api_key or '',
            'secret': cfg.xt_secret or '',
            'enableRateLimit': True,
            'options': params,
        })
        self.margin_mode = getattr(cfg, 'xt_margin_mode', 'cross')

    @staticmethod
    def _normalize_token_for_crypto(token: str) -> str:
        base = ''.join(ch for ch in (token or '').upper() if ch.isalnum())
        if base in {"GOLD", "XAU", "XAUUSD", "XAUUSDT"}:
            return "PAXG"
        return base

    def swap_symbol(self, token: str, quote: str) -> str:
        # Map gold references to Pax Gold (PAXG) since we trade crypto, not Forex
        base = self._normalize_token_for_crypto(token)
        quote = (quote or '').upper()
        return f"{base}/{quote}:{quote}"

    def fetch_price(self, symbol: str) -> Optional[float]:
        try:
            ticker = self.exchange.fetch_ticker(symbol)
            price = ticker.get('last') or ticker.get('close')
            return float(price) if price is not None else None
        except Exception as e:
            logger.warning("XT: Failed to fetch price for %s: %s", symbol, e)
            return None

    def get_available_balance(self, currency_code: str) -> Optional[float]:
        try:
            bal = self.exchange.fetch_balance()
            info = bal.get(currency_code.upper()) or bal.get(currency_code) or {}
            free = info.get('free')
            if free is None:
                free = 0.0
            return float(free)
        except Exception as e:
            logger.error("XT: Failed to fetch balance for %s: %s", currency_code, e)
            return None

    def _build_order_params(self, base: Optional[Dict] = None) -> Dict:
        params: Dict = dict(base or {})
        mode = (self.margin_mode or 'cross').lower()
        if mode in ('cross', 'isolated'):
            params['marginMode'] = mode
        return params

    def market_order(self, symbol: str, side: str, amount: float, params: Optional[Dict] = None) -> ExecutionResult:
        try:
            full_params = self._build_order_params(params)
            o = self.exchange.create_order(symbol=symbol, type='market', side=side, amount=amount, params=full_params)
            order_id = str(o.get('id') or '')
            logger.info("XT: Submitted market order id=%s symbol=%s side=%s amount=%s params=%s", order_id, symbol, side, amount, full_params)
            return ExecutionResult(success=True, order_id=order_id, status=str(o.get('status') or 'filled'), error=None)
        except Exception as e:
            name = e.__class__.__name__
            logger.error("XT: Market order failed symbol=%s side=%s amount=%s error=%s: %s", symbol, side, amount, name, e)
            return ExecutionResult(success=False, order_id=None, status='error', error=f"{name}: {e}")

    def limit_order(self, symbol: str, side: str, amount: float, price: float, params: Optional[Dict] = None) -> ExecutionResult:
        try:
            full_params = self._build_order_params(params)
            o = self.exchange.create_order(symbol=symbol, type='limit', side=side, amount=amount, price=price, params=full_params)
            order_id = str(o.get('id') or '')
            logger.info("XT: Submitted limit order id=%s symbol=%s side=%s amount=%s price=%s params=%s", order_id, symbol, side, amount, price, full_params)
            return ExecutionResult(success=True, order_id=order_id, status=str(o.get('status') or 'open'), error=None)
        except Exception as e:
            name = e.__class__.__name__
            logger.error("XT: Limit order failed symbol=%s side=%s amount=%s price=%s error=%s: %s", symbol, side, amount, price, name, e)
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


def _build_futures_order_params(leverage: Optional[float], stop_losses: Optional[List[float]], take_profits: Optional[List[float]]) -> Dict:
    """Prepare order params for futures. CCXT may not support SL/TP via unified params; pass hints if available."""
    params: Dict = {}
    if leverage is not None:
        # Some exchanges accept 'leverage' in params even if setLeverage is not supported
        params['leverage'] = float(leverage)
    # Use the first SL/TP levels if provided
    if stop_losses:
        try:
            params['stopLossPrice'] = float(stop_losses[0])
        except Exception:
            pass
    if take_profits:
        try:
            params['takeProfitPrice'] = float(take_profits[0])
        except Exception:
            pass
    return params


def execute_signal(
    cfg: Config,
    token: Optional[str],
    position_type: Optional[str],
    entry_price: Optional[float],
    leverage: Optional[float],
    precomputed_quantity: Optional[float] = None,
    stop_losses: Optional[List[float]] = None,
    take_profits: Optional[List[float]] = None,
    order_type: str = 'market',
) -> ExecutionResult:
    if is_anomalous_signal(token, position_type):
        logger.warning("XT: Rejected anomalous signal token=%s position=%s", token, position_type)
        return ExecutionResult(False, None, 'rejected_anomaly', 'Signal fields invalid or missing')
    
    client = XTClient(cfg)
    # Use swap symbol for futures
    symbol = client.swap_symbol(token, cfg.order_quote)
    
    side = 'buy' if position_type.lower() == 'long' else 'sell'

    # Fetch current price
    current_price = client.fetch_price(symbol)
    if price_deviation_too_high(entry_price, current_price, cfg.max_price_deviation_pct):
        logger.warning("XT: Rejected due to price deviation symbol=%s entry=%s current=%s", symbol, entry_price, current_price)
        return ExecutionResult(False, None, 'rejected_deviation', f'Price deviation too high: entry={entry_price}, current={current_price}')

    # Determine quantity
    ref_price = current_price or (entry_price or 0.0)
    if ref_price <= 0:
        logger.error("XT: No valid price available for symbol=%s", symbol)
        return ExecutionResult(False, None, 'rejected_no_price', 'No valid price available')

    quantity = float(precomputed_quantity) if (precomputed_quantity and precomputed_quantity > 0) else compute_order_amount(cfg.order_notional, ref_price)

    params = _build_futures_order_params(leverage, stop_losses, take_profits)

    attempts = 0
    while True:
        attempts += 1
        if order_type == 'limit' and entry_price and entry_price > 0:
            result = client.limit_order(symbol, side, quantity, float(entry_price), params=params)
        else:
            result = client.market_order(symbol, side, quantity, params=params)

        if result.success:
            return result
        if is_safe_retry(result.error) and attempts < 3:
            logger.warning("XT: Transient order error, retrying attempt=%d error=%s", attempts, result.error)
            time.sleep(1.0 * attempts)
            continue
        logger.critical("XT: Order submission failed permanently symbol=%s side=%s error=%s", symbol, side, result.error)