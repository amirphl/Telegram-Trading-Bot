from typing import Dict, Optional
import logging

import ccxt
from ccxt.base.types import OrderSide

from configs.config import Config
from internal.services.exchange import ExecutionResult


logger = logging.getLogger(__name__)


class LBankClient:
    def __init__(self, cfg: Config) -> None:
        params: Dict[str, str] = {}
        if cfg.lbank_password:
            params["password"] = cfg.lbank_password
        self.exchange = ccxt.lbank(
            {
                "apiKey": cfg.lbank_api_key or "",
                "secret": cfg.lbank_secret or "",
                "enableRateLimit": True,
                "options": params,
            }
        )

    @staticmethod
    def _normalize_token_for_crypto(token: str) -> str:
        base = "".join(ch for ch in (token or "").upper() if ch.isalnum())
        if base in {"GOLD", "XAU", "XAUUSD", "XAUUSDT"}:
            return "PAXG"
        return base

    def swap_symbol(self, token: str, quote: str) -> str:
        # CCXT swap symbol format: BASE/QUOTE:SETTLE (linear USDT perps)
        token = self._normalize_token_for_crypto(token)
        quote = (quote or "").upper()
        return f"{token}/{quote}:{quote}"

    def fetch_price(self, symbol: str) -> Optional[float]:
        try:
            ticker = self.exchange.fetch_ticker(symbol)
            price = ticker.get("last") or ticker.get("close")
            return float(price) if price is not None else None
        except Exception as e:
            logger.warning("Failed to fetch price for %s: %s", symbol, e)
            return None

    def get_available_balance(self, currency_code: str) -> Optional[float]:
        try:
            bal = self.exchange.fetch_balance()
            info = bal.get(currency_code.upper()) or bal.get(currency_code) or {}
            free = info.get("free")
            if free is None:
                free = 0.0
            free_val = float(free)
            logger.info(
                "Fetched available balance %s=%f", currency_code.upper(), free_val
            )
            return free_val
        except Exception as e:
            logger.error("Failed to fetch balance for %s: %s", currency_code, e)
            return None

    def market_order(
        self, symbol: str, side: OrderSide, amount: float, params: Optional[Dict] = None
    ) -> ExecutionResult:
        try:
            o = self.exchange.create_order(
                symbol=symbol,
                type="market",
                side=side,
                amount=amount,
                params=params or {},
            )
            order_id = str(o.get("id") or "")
            logger.info(
                "LBank: Submitted market order id=%s symbol=%s side=%s amount=%s params=%s",
                order_id,
                symbol,
                side,
                amount,
                (params or {}),
            )
            return ExecutionResult(
                success=True,
                order_id=order_id,
                status=str(o.get("status") or "filled"),
                error=None,
            )
        except Exception as e:
            name = e.__class__.__name__
            logger.error(
                "LBank: Market order failed symbol=%s side=%s amount=%s error=%s: %s",
                symbol,
                side,
                amount,
                name,
                e,
            )
            return ExecutionResult(
                success=False, order_id=None, status="error", error=f"{name}: {e}"
            )

    def limit_order(
        self,
        symbol: str,
        side: OrderSide,
        amount: float,
        price: float,
        params: Optional[Dict] = None,
    ) -> ExecutionResult:
        try:
            o = self.exchange.create_order(
                symbol=symbol,
                type="limit",
                side=side,
                amount=amount,
                price=price,
                params=params or {},
            )
            order_id = str(o.get("id") or "")
            logger.info(
                "LBank: Submitted limit order id=%s symbol=%s side=%s amount=%s price=%s params=%s",
                order_id,
                symbol,
                side,
                amount,
                price,
                (params or {}),
            )
            return ExecutionResult(
                success=True,
                order_id=order_id,
                status=str(o.get("status") or "open"),
                error=None,
            )
        except Exception as e:
            name = e.__class__.__name__
            logger.error(
                "LBank: Limit order failed symbol=%s side=%s amount=%s price=%s error=%s: %s",
                symbol,
                side,
                amount,
                price,
                name,
                e,
            )
            return ExecutionResult(
                success=False, order_id=None, status="error", error=f"{name}: {e}"
            )
