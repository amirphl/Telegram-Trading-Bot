from __future__ import annotations

import hashlib
import logging
import math
import re
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import ccxt  # type: ignore
from ccxt.base.types import OrderSide

from configs.config import Config

AMBIGUOUS_ERROR_NAMES = {
    "NetworkError",
    "DDoSProtection",
    "RequestTimeout",
    "ExchangeNotAvailable",
    "RateLimitExceeded",
}
REJECTED_EXCHANGE_STATUSES = {"canceled", "cancelled", "expired", "rejected"}

logger = logging.getLogger(__name__)


class ExecutionRejected(ValueError):
    pass


class ProtectionCreationError(RuntimeError):
    def __init__(self, cause: Exception, created: list[dict[str, Any]]) -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.created = created


@dataclass(frozen=True)
class ExecutionPlan:
    symbol: str
    market_type: str
    side: str
    quantity: float
    current_price: float
    leverage: float
    client_order_id: str
    price_source: str
    price_timestamp_utc: str
    price_deviation_pct: float | None
    stop_losses: list[float]
    take_profits: list[float]
    market: dict[str, Any]


@dataclass
class ExecutionResult:
    success: bool
    order_id: str | None
    status: str
    error: str | None
    client_order_id: str | None = None
    symbol: str | None = None
    side: str | None = None
    market_type: str | None = None
    requested_quantity: float | None = None
    filled_quantity: float | None = None
    current_price: float | None = None
    average_price: float | None = None
    cost: float | None = None
    fee: Any = None
    exchange_status: str | None = None
    leverage: float | None = None
    price_source: str | None = None
    price_timestamp_utc: str | None = None
    price_deviation_pct: float | None = None
    raw_order: dict[str, Any] | None = None
    protective_orders: list[dict[str, Any]] = field(default_factory=list)
    reconciled: bool = False

    def to_audit_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "order_id": self.order_id,
            "client_order_id": self.client_order_id,
            "status": self.status,
            "exchange_status": self.exchange_status,
            "error": self.error,
            "symbol": self.symbol,
            "side": self.side,
            "market_type": self.market_type,
            "requested_quantity": self.requested_quantity,
            "filled_quantity": self.filled_quantity,
            "current_price": self.current_price,
            "average_price": self.average_price,
            "cost": self.cost,
            "fee": self.fee,
            "leverage": self.leverage,
            "price_source": self.price_source,
            "price_timestamp_utc": self.price_timestamp_utc,
            "price_deviation_pct": self.price_deviation_pct,
            "protective_orders": self.protective_orders,
            "reconciled": self.reconciled,
        }


def stable_client_order_id(chat_id: int, message_id: int, role: str = "entry") -> str:
    digest = hashlib.sha256(f"telegram:{chat_id}:{message_id}:{role}".encode()).hexdigest()
    return f"tg{digest[:26]}"


def normalize_token(token: str | None, quote: str) -> str:
    if token is None:
        raise ExecutionRejected("token is missing")
    raw = token.strip().upper()
    quote = quote.strip().upper()
    pair_match = re.fullmatch(r"([A-Z0-9]{2,15})[/_-]([A-Z0-9]{2,10})", raw)
    if pair_match:
        base, supplied_quote = pair_match.groups()
        if supplied_quote != quote:
            raise ExecutionRejected(f"signal quote {supplied_quote} does not match configured quote {quote}")
        raw = base
    elif not re.fullmatch(r"[A-Z0-9]{2,20}", raw):
        raise ExecutionRejected("token must contain only ASCII letters and digits")
    if raw.endswith(quote) and len(raw) > len(quote) + 1:
        raw = raw[: -len(quote)]
    if not re.fullmatch(r"[A-Z][A-Z0-9]{1,14}", raw):
        raise ExecutionRejected("normalized token format is invalid")
    return raw


def _finite_positive(value: Any, field_name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ExecutionRejected(f"{field_name} is not numeric") from exc
    if not math.isfinite(number) or number <= 0:
        raise ExecutionRejected(f"{field_name} must be finite and positive")
    return number


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    return _finite_positive(value, "numeric value")


def _timestamp_iso(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000.0, tz=timezone.utc).isoformat()


def _free_balance(balance: dict[str, Any], currency: str) -> float | None:
    direct = balance.get(currency)
    if isinstance(direct, dict) and direct.get("free") is not None:
        return float(direct["free"])
    free = balance.get("free")
    if isinstance(free, dict) and free.get(currency) is not None:
        return float(free[currency])
    return None


class LBankClient:
    def __init__(self, cfg: Config, exchange=None) -> None:
        self.cfg = cfg
        if exchange is None:
            exchange_config: dict[str, Any] = {
                "apiKey": cfg.lbank_api_key or "",
                "secret": cfg.lbank_secret or "",
                "enableRateLimit": True,
                "timeout": int(getattr(cfg, "exchange_timeout_secs", 30) * 1000),
                "options": {"defaultType": cfg.execution_market_type},
            }
            if cfg.lbank_password:
                exchange_config["password"] = cfg.lbank_password
            exchange = ccxt.lbank(exchange_config)
        self.exchange = exchange
        self._markets: dict[str, dict[str, Any]] | None = None
        self._validate_required_credentials()
        if cfg.trading_mode == "sandbox":
            sandbox = getattr(self.exchange, "set_sandbox_mode", None)
            if not callable(sandbox):
                raise ExecutionRejected("LBank adapter does not expose sandbox mode")
            try:
                sandbox(True)
            except Exception as exc:
                raise ExecutionRejected(
                    f"LBank sandbox mode is unavailable ({exc.__class__.__name__})"
                ) from exc

    @staticmethod
    def _normalize_token_for_crypto(token: str) -> str:
        """Normalize legacy crypto aliases used by the generic execution path."""
        base = "".join(ch for ch in (token or "").upper() if ch.isalnum())
        if base in {"GOLD", "XAU", "XAUUSD", "XAUUSDT"}:
            return "PAXG"
        return base

    def swap_symbol(self, token: str, quote: str) -> str:
        """Return CCXT's linear-swap symbol format for compatibility clients."""
        base = self._normalize_token_for_crypto(token)
        normalized_quote = (quote or "").upper()
        return f"{base}/{normalized_quote}:{normalized_quote}"

    def fetch_price(self, symbol: str) -> float | None:
        try:
            ticker = self.exchange.fetch_ticker(symbol)
            price = ticker.get("last") or ticker.get("close")
            return float(price) if price is not None else None
        except Exception as exc:
            logger.warning("Failed to fetch price for %s: %s", symbol, exc)
            return None

    def get_available_balance(self, currency_code: str) -> float | None:
        try:
            balance = self.exchange.fetch_balance()
            info = balance.get(currency_code.upper()) or balance.get(currency_code) or {}
            free = info.get("free")
            free_value = float(free) if free is not None else 0.0
            logger.info(
                "Fetched available balance %s=%f", currency_code.upper(), free_value
            )
            return free_value
        except Exception as exc:
            logger.error("Failed to fetch balance for %s: %s", currency_code, exc)
            return None

    def market_order(
        self,
        symbol: str,
        side: OrderSide,
        amount: float,
        params: dict[str, Any] | None = None,
    ) -> ExecutionResult:
        try:
            order = self.exchange.create_order(
                symbol=symbol,
                type="market",
                side=side,
                amount=amount,
                params=params or {},
            )
            return ExecutionResult(
                success=True,
                order_id=str(order.get("id") or ""),
                status=str(order.get("status") or "filled"),
                error=None,
                symbol=symbol,
                side=str(side),
                requested_quantity=amount,
                raw_order=order,
            )
        except Exception as exc:
            name = exc.__class__.__name__
            logger.error(
                "LBank market order failed symbol=%s side=%s amount=%s error=%s: %s",
                symbol,
                side,
                amount,
                name,
                exc,
            )
            return ExecutionResult(False, None, "error", f"{name}: {exc}")

    def limit_order(
        self,
        symbol: str,
        side: OrderSide,
        amount: float,
        price: float,
        params: dict[str, Any] | None = None,
    ) -> ExecutionResult:
        try:
            order = self.exchange.create_order(
                symbol=symbol,
                type="limit",
                side=side,
                amount=amount,
                price=price,
                params=params or {},
            )
            return ExecutionResult(
                success=True,
                order_id=str(order.get("id") or ""),
                status=str(order.get("status") or "open"),
                error=None,
                symbol=symbol,
                side=str(side),
                requested_quantity=amount,
                current_price=price,
                raw_order=order,
            )
        except Exception as exc:
            name = exc.__class__.__name__
            logger.error(
                "LBank limit order failed symbol=%s side=%s amount=%s price=%s error=%s: %s",
                symbol,
                side,
                amount,
                price,
                name,
                exc,
            )
            return ExecutionResult(False, None, "error", f"{name}: {exc}")

    def _validate_required_credentials(self) -> None:
        required = getattr(self.exchange, "requiredCredentials", None) or {}
        configured = {
            "apiKey": self.cfg.lbank_api_key,
            "secret": self.cfg.lbank_secret,
            "password": self.cfg.lbank_password,
        }
        missing = sorted(
            name
            for name, is_required in required.items()
            if is_required and not (configured.get(name) or getattr(self.exchange, name, None))
        )
        if missing:
            raise ExecutionRejected(
                "LBank credentials are incomplete; required fields: " + ", ".join(missing)
            )
        checker = getattr(self.exchange, "check_required_credentials", None)
        if callable(checker):
            try:
                checker()
            except Exception as exc:
                # CCXT exceptions may include request context. Keep the operator
                # message field-only so credentials can never enter logs.
                names = sorted(name for name, required_value in required.items() if required_value)
                detail = ", ".join(names) if names else "exchange-defined credentials"
                raise ExecutionRejected(
                    f"LBank credentials are incomplete; check: {detail}"
                ) from exc

    def check_authentication(self) -> None:
        """Perform an authenticated, read-only request without exposing balances."""
        self._validate_required_credentials()
        try:
            balance = self.exchange.fetch_balance(
                {"type": self.cfg.execution_market_type}
            )
        except Exception as exc:
            raise ExecutionRejected(
                f"LBank authenticated read-only check failed ({exc.__class__.__name__})"
            ) from exc
        if not isinstance(balance, dict):
            raise ExecutionRejected(
                "LBank authenticated read-only check returned an invalid response"
            )

    def load_markets(self) -> dict[str, dict[str, Any]]:
        if self._markets is None:
            markets = self.exchange.load_markets()
            if not isinstance(markets, dict) or not markets:
                raise ExecutionRejected("exchange returned no markets")
            self._markets = markets
        return self._markets

    def resolve_market(self, token: str | None) -> dict[str, Any]:
        base = self._normalize_token_for_crypto(
            normalize_token(token, self.cfg.order_quote)
        )
        quote = self.cfg.order_quote.upper()
        matches = []
        for market in self.load_markets().values():
            if str(market.get("base") or "").upper() != base:
                continue
            if str(market.get("quote") or "").upper() != quote:
                continue
            if self.cfg.execution_market_type == "spot" and not market.get("spot"):
                continue
            if self.cfg.execution_market_type == "swap" and not market.get("swap"):
                continue
            matches.append(market)
        if not matches:
            raise ExecutionRejected(
                f"no {self.cfg.execution_market_type} market for {base}/{quote}"
            )
        active = [market for market in matches if market.get("active") is not False]
        if not active:
            raise ExecutionRejected(f"market for {base}/{quote} is inactive")
        if len(active) > 1:
            settled = [
                market
                for market in active
                if str(market.get("settle") or quote).upper() == quote
            ]
            if len(settled) == 1:
                return settled[0]
            raise ExecutionRejected(f"market for {base}/{quote} is ambiguous")
        return active[0]

    def _feature(self, symbol: str, feature: str) -> bool:
        feature_value = getattr(self.exchange, "feature_value", None)
        if callable(feature_value):
            try:
                value = feature_value(symbol, "createOrder", feature)
                if value is not None:
                    return bool(value)
            except Exception:
                pass
        return bool((getattr(self.exchange, "has", {}) or {}).get(feature))

    def _validate_protective_prices(
        self,
        side: str,
        current_price: float,
        stop_losses: Sequence[float],
        take_profits: Sequence[float],
    ) -> tuple[list[float], list[float]]:
        stops = [_finite_positive(value, "stop loss") for value in stop_losses]
        targets = [_finite_positive(value, "take profit") for value in take_profits]
        if self.cfg.require_protective_orders and (not stops or not targets):
            raise ExecutionRejected("both stop loss and take profit are required")
        if side == "buy":
            if any(value >= current_price for value in stops):
                raise ExecutionRejected("long stop losses must be below current price")
            if any(value <= current_price for value in targets):
                raise ExecutionRejected("long take profits must be above current price")
        else:
            if any(value <= current_price for value in stops):
                raise ExecutionRejected("short stop losses must be above current price")
            if any(value >= current_price for value in targets):
                raise ExecutionRejected("short take profits must be below current price")
        return sorted(set(stops)), sorted(set(targets))

    def prepare_signal(
        self,
        token: str | None,
        position_type: str | None,
        entry_price: float | None,
        leverage: float | None,
        stop_losses: Sequence[float],
        take_profits: Sequence[float],
        client_order_id: str,
    ) -> ExecutionPlan:
        _finite_positive(self.cfg.order_notional, "ORDER_NOTIONAL")
        if not math.isfinite(self.cfg.balance_buffer_pct) or not 0 <= self.cfg.balance_buffer_pct < 1:
            raise ExecutionRejected("BALANCE_BUFFER_PCT must be between 0 and 1")
        if self.cfg.ticker_max_age_secs <= 0:
            raise ExecutionRejected("TICKER_MAX_AGE_SECS must be positive")
        if not math.isfinite(self.cfg.max_leverage) or self.cfg.max_leverage < 1:
            raise ExecutionRejected("MAX_LEVERAGE must be finite and at least 1")
        if not math.isfinite(self.cfg.max_price_deviation_pct) or self.cfg.max_price_deviation_pct < 0:
            raise ExecutionRejected("MAX_PRICE_DEVIATION_PCT cannot be negative")
        direction = (position_type or "").strip().lower()
        if direction not in {"long", "short"}:
            raise ExecutionRejected("position_type must be long or short")
        market = self.resolve_market(token)
        symbol = str(market["symbol"])
        market_type = self.cfg.execution_market_type
        if market_type == "spot" and direction == "short":
            raise ExecutionRejected("spot mode cannot open a short position")
        side = "buy" if direction == "long" else "sell"

        configured_leverage = _optional_float(leverage) or 1.0
        if market_type == "spot" and configured_leverage != 1.0:
            raise ExecutionRejected("spot mode does not support leverage")
        if configured_leverage > self.cfg.max_leverage:
            raise ExecutionRejected(
                f"leverage exceeds configured maximum ({configured_leverage} > {self.cfg.max_leverage})"
            )
        leverage_limits = (market.get("limits") or {}).get("leverage") or {}
        if leverage_limits.get("min") is not None and configured_leverage < float(leverage_limits["min"]):
            raise ExecutionRejected("leverage is below the market minimum")
        if leverage_limits.get("max") is not None and configured_leverage > float(leverage_limits["max"]):
            raise ExecutionRejected("leverage exceeds the market maximum")
        if market_type == "swap" and not (getattr(self.exchange, "has", {}) or {}).get("setLeverage"):
            raise ExecutionRejected("exchange adapter cannot set leverage for this swap market")
        if not (getattr(self.exchange, "has", {}) or {}).get("createMarketOrder", True):
            raise ExecutionRejected("market orders are not supported")

        received_ms = int(time.time() * 1000)
        ticker = self.exchange.fetch_ticker(symbol)
        if not isinstance(ticker, dict):
            raise ExecutionRejected("ticker response is invalid")
        price_value = ticker.get("last") if ticker.get("last") is not None else ticker.get("close")
        current_price = _finite_positive(price_value, "current ticker price")
        ticker_ms = ticker.get("timestamp")
        if ticker_ms is not None:
            ticker_ms = int(ticker_ms)
            age_secs = (received_ms - ticker_ms) / 1000.0
            if age_secs < -5.0:
                raise ExecutionRejected("ticker timestamp is implausibly far in the future")
            age_secs = max(0.0, age_secs)
            if age_secs > self.cfg.ticker_max_age_secs:
                raise ExecutionRejected(
                    f"ticker is stale ({age_secs:.3f}s > {self.cfg.ticker_max_age_secs}s)"
                )
            price_source = "lbank.fetch_ticker:last"
        else:
            ticker_ms = received_ms
            price_source = "lbank.fetch_ticker:last_received_at"

        detected_entry = _optional_float(entry_price)
        deviation = None
        if detected_entry is not None:
            deviation = abs(current_price - detected_entry) / detected_entry
            if deviation > self.cfg.max_price_deviation_pct:
                raise ExecutionRejected(
                    f"price deviation too high ({deviation:.8f} > {self.cfg.max_price_deviation_pct})"
                )

        stops, targets = self._validate_protective_prices(
            side, current_price, stop_losses, take_profits
        )
        if market_type == "spot" and (stops or targets):
            raise ExecutionRejected(
                "safe reduce-only/OCO protective orders are unavailable in spot mode; use swap mode"
            )
        if stops and not self._feature(symbol, "stopLossPrice"):
            raise ExecutionRejected("exchange does not advertise stop-loss order support")
        if targets and not self._feature(symbol, "takeProfitPrice"):
            raise ExecutionRejected("exchange does not advertise take-profit order support")

        base_amount = self.cfg.order_notional / current_price
        contract_size = float(market.get("contractSize") or 1.0)
        raw_amount = base_amount / contract_size if market_type == "swap" else base_amount
        try:
            quantity = float(self.exchange.amount_to_precision(symbol, raw_amount))
        except Exception as exc:
            raise ExecutionRejected(f"cannot format order amount: {exc}") from exc
        if not math.isfinite(quantity) or quantity <= 0:
            raise ExecutionRejected("order quantity rounds to zero or is invalid")

        limits = market.get("limits") or {}
        amount_limits = limits.get("amount") or {}
        cost_limits = limits.get("cost") or {}
        estimated_cost = quantity * contract_size * current_price
        if amount_limits.get("min") is not None and quantity < float(amount_limits["min"]):
            raise ExecutionRejected("order quantity is below the market minimum")
        if amount_limits.get("max") is not None and quantity > float(amount_limits["max"]):
            raise ExecutionRejected("order quantity exceeds the market maximum")
        if cost_limits.get("min") is not None and estimated_cost < float(cost_limits["min"]):
            raise ExecutionRejected("order cost is below the market minimum")
        if cost_limits.get("max") is not None and estimated_cost > float(cost_limits["max"]):
            raise ExecutionRejected("order cost exceeds the market maximum")

        balance = self.exchange.fetch_balance({"type": market_type})
        balance_currency = str(market.get("settle") or market.get("quote") or self.cfg.order_quote)
        free_balance = _free_balance(balance, balance_currency)
        if free_balance is None:
            raise ExecutionRejected(f"free {balance_currency} balance is unavailable")
        required_balance = self.cfg.order_notional
        if market_type == "swap":
            required_balance /= configured_leverage
        required_balance *= 1.0 + self.cfg.balance_buffer_pct
        if free_balance < required_balance:
            raise ExecutionRejected(
                f"insufficient {balance_currency} balance ({free_balance} < {required_balance})"
            )

        return ExecutionPlan(
            symbol=symbol,
            market_type=market_type,
            side=side,
            quantity=quantity,
            current_price=current_price,
            leverage=configured_leverage,
            client_order_id=client_order_id,
            price_source=price_source,
            price_timestamp_utc=_timestamp_iso(ticker_ms),
            price_deviation_pct=deviation,
            stop_losses=stops,
            take_profits=targets,
            market=market,
        )

    def set_leverage(self, plan: ExecutionPlan) -> None:
        if plan.market_type != "swap":
            return
        self.exchange.set_leverage(
            plan.leverage,
            plan.symbol,
            {"marginMode": self.cfg.margin_mode},
        )

    def create_entry_order(self, plan: ExecutionPlan) -> dict[str, Any]:
        params: dict[str, Any] = {"clientOrderId": plan.client_order_id}
        if plan.market_type == "swap":
            params.update({"reduceOnly": False, "marginMode": self.cfg.margin_mode})
        return self.exchange.create_order(
            plan.symbol,
            "market",
            plan.side,
            plan.quantity,
            None,
            params,
        )

    def find_order(
        self,
        symbol: str,
        client_order_id: str,
        order_id: str | None = None,
    ) -> dict[str, Any] | None:
        capabilities = getattr(self.exchange, "has", {}) or {}
        if order_id and capabilities.get("fetchOrder"):
            try:
                return self.exchange.fetch_order(order_id, symbol)
            except Exception:
                pass
        for method_name, capability in (
            ("fetch_orders", "fetchOrders"),
            ("fetch_open_orders", "fetchOpenOrders"),
            ("fetch_closed_orders", "fetchClosedOrders"),
        ):
            if not capabilities.get(capability):
                continue
            try:
                orders = getattr(self.exchange, method_name)(symbol)
            except Exception:
                continue
            for order in orders or []:
                candidate = order.get("clientOrderId") or order.get("client_order_id")
                if candidate == client_order_id:
                    return order
        return None

    def _protective_order(
        self,
        plan: ExecutionPlan,
        role: str,
        index: int,
        trigger_price: float,
    ) -> dict[str, Any]:
        close_side = "sell" if plan.side == "buy" else "buy"
        client_id = stable_client_order_id(0, 0, f"{plan.client_order_id}:{role}:{index}")
        params: dict[str, Any] = {"clientOrderId": client_id}
        params["stopLossPrice" if role == "stop_loss" else "takeProfitPrice"] = trigger_price
        if plan.market_type == "swap":
            params.update({"reduceOnly": True, "marginMode": self.cfg.margin_mode})
        raw = self.exchange.create_order(
            plan.symbol,
            "market",
            close_side,
            plan.quantity,
            None,
            params,
        )
        exchange_status = str(raw.get("status") or "open").lower()
        if exchange_status in REJECTED_EXCHANGE_STATUSES:
            raise ExecutionRejected(
                f"protective order returned terminal status {exchange_status}"
            )
        return {
            "role": role,
            "index": index,
            "trigger_price": trigger_price,
            "quantity": plan.quantity,
            "client_order_id": client_id,
            "order_id": str(raw.get("id")) if raw.get("id") is not None else None,
            "exchange_status": exchange_status,
            "status": "closed" if exchange_status == "closed" else "open",
            "error": None,
            "raw": raw,
        }

    def create_protective_orders(self, plan: ExecutionPlan) -> list[dict[str, Any]]:
        created = []
        try:
            specifications = [
                ("stop_loss", index, trigger)
                for index, trigger in enumerate(plan.stop_losses)
            ] + [
                ("take_profit", index, trigger)
                for index, trigger in enumerate(plan.take_profits)
            ]
            for role, index, trigger in specifications:
                order = self._protective_order(plan, role, index, trigger)
                created.append(order)
                if order["status"] == "closed":
                    break
        except Exception as exc:
            raise ProtectionCreationError(exc, created) from exc
        return created

    def cancel_orders(self, symbol: str, orders: Iterable[dict[str, Any]]) -> None:
        if not (getattr(self.exchange, "has", {}) or {}).get("cancelOrder"):
            return
        for order in orders:
            order_id = order.get("order_id")
            if not order_id:
                continue
            try:
                self.exchange.cancel_order(order_id, symbol)
                order["status"] = "canceled"
                order["exchange_status"] = "canceled"
            except Exception as exc:
                order["error"] = f"cancel failed: {exc}"

    def emergency_close(self, plan: ExecutionPlan) -> dict[str, Any] | None:
        close_side = "sell" if plan.side == "buy" else "buy"
        params: dict[str, Any] = {
            "clientOrderId": stable_client_order_id(0, 0, f"{plan.client_order_id}:emergency")
        }
        if plan.market_type == "swap":
            params["reduceOnly"] = True
            params["marginMode"] = self.cfg.margin_mode
        try:
            order = self.exchange.create_order(
                plan.symbol, "market", close_side, plan.quantity, None, params
            )
            if str(order.get("status") or "unknown").lower() in {
                "canceled",
                "cancelled",
                "expired",
                "rejected",
            }:
                return None
            return order
        except Exception:
            return None


_CLIENT_CACHE: dict[tuple, LBankClient] = {}


def close_lbank_clients() -> int:
    """Close cached HTTP sessions and drop clients during application shutdown."""
    clients = list(_CLIENT_CACHE.values())
    _CLIENT_CACHE.clear()
    closed = 0
    for client in clients:
        close = getattr(client.exchange, "close", None)
        if not callable(close):
            continue
        try:
            close()
            closed += 1
        except Exception as exc:
            print(f"[!] LBank client cleanup failed ({exc.__class__.__name__})")
    return closed


def get_lbank_client(cfg: Config) -> LBankClient:
    key = (
        cfg.lbank_api_key,
        cfg.lbank_secret,
        cfg.lbank_password,
        cfg.trading_mode,
        cfg.execution_market_type,
        cfg.margin_mode,
        cfg.order_quote,
        cfg.require_protective_orders,
        cfg.max_leverage,
        cfg.ticker_max_age_secs,
    )
    if key not in _CLIENT_CACHE:
        _CLIENT_CACHE[key] = LBankClient(cfg)
    return _CLIENT_CACHE[key]


def check_lbank_authentication(
    cfg: Config,
    *,
    client: LBankClient | None = None,
) -> LBankClient:
    client = client or get_lbank_client(cfg)
    client.check_authentication()
    return client


def _result_from_order(
    order: dict[str, Any],
    plan: ExecutionPlan,
    *,
    status: str,
    protective_orders: list[dict[str, Any]] | None = None,
    reconciled: bool = False,
) -> ExecutionResult:
    return ExecutionResult(
        success=True,
        order_id=str(order.get("id")) if order.get("id") is not None else None,
        client_order_id=str(order.get("clientOrderId") or plan.client_order_id),
        status=status,
        error=None,
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
        protective_orders=protective_orders or [],
        reconciled=reconciled,
    )


def rejected_result(
    status: str,
    error: str,
    client_order_id: str | None = None,
    plan: ExecutionPlan | None = None,
) -> ExecutionResult:
    return ExecutionResult(
        False,
        None,
        status,
        error,
        client_order_id=client_order_id,
        symbol=plan.symbol if plan else None,
        side=plan.side if plan else None,
        market_type=plan.market_type if plan else None,
        requested_quantity=plan.quantity if plan else None,
        current_price=plan.current_price if plan else None,
        leverage=plan.leverage if plan else None,
        price_source=plan.price_source if plan else None,
        price_timestamp_utc=plan.price_timestamp_utc if plan else None,
        price_deviation_pct=plan.price_deviation_pct if plan else None,
    )


def execute_plan(client: LBankClient, plan: ExecutionPlan) -> ExecutionResult:
    try:
        client.set_leverage(plan)
        entry = client.create_entry_order(plan)
    except Exception as exc:
        error = f"{exc.__class__.__name__}: {exc}"
        if exc.__class__.__name__ in AMBIGUOUS_ERROR_NAMES:
            found = client.find_order(plan.symbol, plan.client_order_id)
            if found is None:
                return rejected_result(
                    "unknown_requires_reconciliation", error, plan.client_order_id, plan
                )
            entry = found
            reconciled = True
        else:
            return rejected_result("entry_rejected", error, plan.client_order_id, plan)
    else:
        reconciled = False

    entry_status = str(entry.get("status") or "unknown").lower()
    if entry_status in {"canceled", "cancelled", "expired", "rejected"}:
        result = _result_from_order(
            entry,
            plan,
            status="entry_rejected",
            reconciled=reconciled,
        )
        result.success = False
        result.error = f"entry order returned terminal status {entry_status}"
        return result

    protective_orders: list[dict[str, Any]] = []
    if plan.stop_losses or plan.take_profits:
        try:
            protective_orders = client.create_protective_orders(plan)
        except ProtectionCreationError as exc:
            protective_orders = exc.created
            cause = exc.cause
            error = f"{cause.__class__.__name__}: {cause}"
            if cause.__class__.__name__ in AMBIGUOUS_ERROR_NAMES:
                return ExecutionResult(
                    **{
                        **_result_from_order(
                            entry,
                            plan,
                            status="protection_unknown_manual_review",
                            protective_orders=protective_orders,
                            reconciled=reconciled,
                        ).__dict__,
                        "success": False,
                        "error": error,
                    }
                )
            close_order = client.emergency_close(plan)
            client.cancel_orders(plan.symbol, protective_orders)
            status = "protection_failed_position_closed" if close_order else "unprotected_position"
            result = _result_from_order(
                entry,
                plan,
                status=status,
                protective_orders=protective_orders,
                reconciled=reconciled,
            )
            result.success = False
            result.error = error
            return result

    immediately_closed = [order for order in protective_orders if order["status"] == "closed"]
    if immediately_closed:
        client.cancel_orders(
            plan.symbol,
            [order for order in protective_orders if order["status"] == "open"],
        )
        return _result_from_order(
            entry,
            plan,
            status="position_closed_by_immediate_protection",
            protective_orders=protective_orders,
            reconciled=reconciled,
        )

    status = "entry_submitted_protected" if protective_orders else "entry_submitted_unprotected"
    return _result_from_order(
        entry,
        plan,
        status=status,
        protective_orders=protective_orders,
        reconciled=reconciled,
    )


def execute_signal(
    cfg: Config,
    token: str | None,
    position_type: str | None,
    entry_price: float | None,
    leverage: float | None,
    stop_losses: Sequence[float] = (),
    take_profits: Sequence[float] = (),
    client_order_id: str | None = None,
    client: LBankClient | None = None,
) -> ExecutionResult:
    client_order_id = client_order_id or stable_client_order_id(0, 0)
    try:
        client = client or get_lbank_client(cfg)
        plan = client.prepare_signal(
            token,
            position_type,
            entry_price,
            leverage,
            stop_losses,
            take_profits,
            client_order_id,
        )
    except Exception as exc:
        return rejected_result("validation_rejected", str(exc), client_order_id)
    return execute_plan(client, plan)


def plan_signal(
    cfg: Config,
    token: str | None,
    position_type: str | None,
    entry_price: float | None,
    leverage: float | None,
    stop_losses: Sequence[float],
    take_profits: Sequence[float],
    client_order_id: str,
    client: LBankClient | None = None,
) -> tuple[LBankClient, ExecutionPlan]:
    client = client or get_lbank_client(cfg)
    return client, client.prepare_signal(
        token,
        position_type,
        entry_price,
        leverage,
        stop_losses,
        take_profits,
        client_order_id,
    )
