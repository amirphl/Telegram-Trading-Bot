import json
import time
import hashlib
import uuid
from typing import Any, Dict, List, Optional, Callable
import logging

import requests

from configs.config import Config
from internal.services.exchange import ExecutionResult


logger = logging.getLogger(__name__)


def _sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _build_query_concat(params: Optional[Dict[str, Any]]) -> str:
    if not params:
        return ""
    items = sorted(params.items(), key=lambda kv: str(kv[0]))
    parts: List[str] = []
    for k, v in items:
        parts.append(str(k))
        parts.append(str(v))
    return "".join(parts)


def _infer_margin_coin_from_symbol(symbol: str) -> str:
    s = (symbol or "").upper()
    for coin in ("USDT", "USD", "USDC", "BTC", "ETH"):
        if s.endswith(coin):
            return coin
    return "USDT"


class BitunixClient:
    def __init__(
        self,
        cfg: Config,
        signer: Optional[Callable[[str, str, str, str, str], str]] = None,
    ) -> None:
        self.base_url = cfg.bitunix_base_url.rstrip("/")
        self.api_key = cfg.bitunix_api_key or ""
        self.secret = cfg.bitunix_secret or ""
        self.language = cfg.bitunix_language or "en-US"
        self.signer = signer or self._default_signer

        # Build and enforce proxy usage
        if not (cfg.proxy_host and cfg.proxy_port and cfg.proxy_type):
            raise RuntimeError(
                "Bitunix client requires a proxy (set PROXY_TYPE/PROXY_HOST/PROXY_PORT)"
            )
        ptype = (cfg.proxy_type or "").upper()
        scheme = "http"
        if ptype.startswith("SOCKS"):
            scheme = "socks5h"  # ensure DNS via proxy to avoid SSL EOFs
        auth = ""
        if cfg.proxy_username and cfg.proxy_password:
            auth = f"{cfg.proxy_username}:{cfg.proxy_password}@"
        proxy_url = f"{scheme}://{auth}{cfg.proxy_host}:{int(cfg.proxy_port)}"
        self._proxies: Dict[str, str] = {"http": proxy_url, "https": proxy_url}

        self.session = requests.Session()
        # Do not inherit env proxies; always use configured proxy
        self.session.trust_env = False
        self.timeout = 20

    @staticmethod
    def _normalize_token_for_crypto(token: str) -> str:
        base = "".join(ch for ch in (token or "").upper() if ch.isalnum())
        if base in {"GOLD", "XAU", "XAUUSD", "XAUUSDT"}:
            return "PAXG"
        return base

    def swap_symbol(self, token: str, quote: str) -> str:
        base = self._normalize_token_for_crypto(token)
        quote = (quote or "").upper()
        if base.endswith(quote):
            return base
        return f"{base}{quote}"

    def _default_signer(
        self,
        nonce: str,
        timestamp_ms: str,
        api_key: str,
        query_concat: str,
        body_string: str,
    ) -> str:
        digest = _sha256_hex(
            f"{nonce}{timestamp_ms}{api_key}{query_concat}{body_string}"
        )
        sign = _sha256_hex(f"{digest}{self.secret}")
        return sign

    def _request(
        self,
        method: str,
        path: str,
        query: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
        auth: bool = False,
    ) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        query = dict(query or {})

        # Prepare body string without spaces if present
        body_string = ""
        data: Optional[str] = None
        if body is not None:
            body_string = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
            data = body_string

        headers = {
            "Content-Type": "application/json",
            "language": self.language,
        }

        if auth:
            if not (self.api_key and self.secret):
                raise RuntimeError(
                    "Bitunix private endpoint requires BITUNIX_API_KEY and BITUNIX_SECRET"
                )
            timestamp_ms = str(int(time.time() * 1000))
            nonce = uuid.uuid4().hex
            query_concat = _build_query_concat(query)
            signature = self.signer(
                nonce, timestamp_ms, self.api_key, query_concat, body_string
            )
            headers.update(
                {
                    "api-key": self.api_key,
                    "sign": signature,
                    "nonce": nonce,
                    "timestamp": timestamp_ms,
                }
            )

        resp = self.session.request(
            method=method.upper(),
            url=url,
            params=query,
            data=data,
            headers=headers,
            timeout=self.timeout,
            proxies=self._proxies,
        )
        resp.raise_for_status()
        return resp.json()

    # Public endpoints
    def fetch_tickers(
        self, symbols: Optional[List[str]] = None
    ) -> Dict[str, Dict[str, Any]]:
        params: Dict[str, Any] = {}
        if symbols:
            params["symbols"] = ",".join(symbols)
        data = self._request(
            "GET", "/api/v1/futures/market/tickers", query=params, auth=False
        )
        if data.get("code") != 0:
            raise RuntimeError(f"Bitunix tickers error: {data}")
        out: Dict[str, Dict[str, Any]] = {}
        for item in data.get("data", []) or []:
            out[str(item.get("symbol"))] = item
        return out

    def fetch_price(self, symbol_pair: str) -> Optional[float]:
        try:
            tick = self.fetch_tickers([symbol_pair]).get(symbol_pair)
            if not tick:
                return None
            lp = tick.get("lastPrice") or tick.get("last") or tick.get("markPrice")
            return float(lp) if lp is not None else None
        except Exception as e:
            logger.warning("Bitunix: failed to fetch price for %s: %s", symbol_pair, e)
            return None

    # Private endpoints
    def get_account(self, margin_coin: str) -> Dict[str, Any]:
        data = self._request(
            "GET",
            "/api/v1/futures/account",
            query={"marginCoin": margin_coin},
            auth=True,
        )
        if data.get("code") != 0:
            raise RuntimeError(f"Bitunix account error: {data}")
        return data

    def get_available_balance(self, margin_coin: str) -> Optional[float]:
        try:
            data = self.get_account(margin_coin)
            arr = data.get("data") or {}
            if not arr:
                return None
            available = arr.get("available")
            return float(available) if available is not None else None
        except Exception as e:
            logger.error("Bitunix: failed to fetch balance for %s: %s", margin_coin, e)
            return None

    def change_leverage(self, margin_coin: str, symbol: str, leverage: int) -> bool:
        body = {
            "symbol": symbol,
            "leverage": int(leverage),
            "marginCoin": margin_coin,
        }
        try:
            data = self._request(
                "POST", "/api/v1/futures/account/change_leverage", body=body, auth=True
            )
            if data.get("code") == 0:
                logger.info(
                    "Bitunix: Changed leverage symbol=%s margin=%s lev=%s",
                    symbol,
                    margin_coin,
                    leverage,
                )
                return True
            logger.warning("Bitunix: Change leverage failed %s", json.dumps(data))
            return False
        except Exception as e:
            logger.warning(
                "Bitunix: Change leverage error symbol=%s error=%s", symbol, e
            )
            return False

    def market_order(
        self, symbol: str, side: str, amount: float, params: Optional[Dict] = None
    ) -> ExecutionResult:
        try:
            # Change leverage first if provided
            lev = (params or {}).get("leverage")
            if lev is not None:
                margin_coin = (params or {}).get(
                    "marginCoin"
                ) or _infer_margin_coin_from_symbol(symbol)
                self.change_leverage(margin_coin, symbol, int(lev))

            reduce_only = bool((params or {}).get("reduceOnly", False))
            tp_price = (params or {}).get(
                "stopLossPrice"
            )  # maintain naming parity if passed
            sl_price = (params or {}).get("takeProfitPrice")
            tp_price = (params or {}).get("tpPrice", tp_price)
            sl_price = (params or {}).get("slPrice", sl_price)
            effect = (params or {}).get("effect")
            client_id = (params or {}).get("clientId")
            r = self.place_order(
                symbol=symbol,
                side=side.upper(),
                qty=amount,
                order_type="MARKET",
                trade_side="OPEN",
                reduce_only=reduce_only,
                tp_price=tp_price,
                tp_stop_type=(params or {}).get("tpStopType"),
                tp_order_type=(params or {}).get("tpOrderType"),
                tp_order_price=(params or {}).get("tpOrderPrice"),
                sl_price=sl_price,
                sl_stop_type=(params or {}).get("slStopType"),
                sl_order_type=(params or {}).get("slOrderType"),
                sl_order_price=(params or {}).get("slOrderPrice"),
                client_id=client_id,
                effect=effect,
            )
            if r.success:
                logger.info(
                    "Bitunix: Submitted market order id=%s symbol=%s side=%s amount=%s",
                    r.order_id,
                    symbol,
                    side,
                    amount,
                )
            else:
                logger.error(
                    "Bitunix: Market order failed symbol=%s side=%s amount=%s error=%s",
                    symbol,
                    side,
                    amount,
                    r.error,
                )
            return r
        except Exception as e:
            return ExecutionResult(False, None, "error", str(e))

    def limit_order(
        self,
        symbol: str,
        side: str,
        amount: float,
        price: float,
        params: Optional[Dict] = None,
    ) -> ExecutionResult:
        try:
            # Change leverage first if provided
            lev = (params or {}).get("leverage")
            if lev is not None:
                margin_coin = (params or {}).get(
                    "marginCoin"
                ) or _infer_margin_coin_from_symbol(symbol)
                self.change_leverage(margin_coin, symbol, int(lev))

            reduce_only = bool((params or {}).get("reduceOnly", False))
            tp_price = (params or {}).get("tpPrice")
            sl_price = (params or {}).get("slPrice")
            effect = (params or {}).get("effect") or "GTC"
            client_id = (params or {}).get("clientId")
            r = self.place_order(
                symbol=symbol,
                side=side.upper(),
                qty=amount,
                order_type="LIMIT",
                price=price,
                trade_side="OPEN",
                reduce_only=reduce_only,
                tp_price=tp_price,
                tp_stop_type=(params or {}).get("tpStopType"),
                tp_order_type=(params or {}).get("tpOrderType"),
                tp_order_price=(params or {}).get("tpOrderPrice"),
                sl_price=sl_price,
                sl_stop_type=(params or {}).get("slStopType"),
                sl_order_type=(params or {}).get("slOrderType"),
                sl_order_price=(params or {}).get("slOrderPrice"),
                client_id=client_id,
                effect=effect,
            )
            if r.success:
                logger.info(
                    "Bitunix: Submitted limit order id=%s symbol=%s side=%s amount=%s price=%s",
                    r.order_id,
                    symbol,
                    side,
                    amount,
                    price,
                )
            else:
                logger.error(
                    "Bitunix: Limit order failed symbol=%s side=%s amount=%s price=%s error=%s",
                    symbol,
                    side,
                    amount,
                    price,
                    r.error,
                )
            return r
        except Exception as e:
            return ExecutionResult(False, None, "error", str(e))

    def place_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        order_type: str,
        price: Optional[float] = None,
        trade_side: str = "OPEN",
        reduce_only: bool = False,
        tp_price: Optional[float] = None,
        tp_stop_type: Optional[str] = None,
        tp_order_type: Optional[str] = None,
        tp_order_price: Optional[float] = None,
        sl_price: Optional[float] = None,
        sl_stop_type: Optional[str] = None,
        sl_order_type: Optional[str] = None,
        sl_order_price: Optional[float] = None,
        client_id: Optional[str] = None,
        effect: Optional[str] = None,
    ) -> ExecutionResult:
        body: Dict[str, Any] = {
            "symbol": symbol,
            "side": side.upper(),
            "qty": str(qty),
            "orderType": order_type.upper(),
            "tradeSide": trade_side.upper(),
            "reduceOnly": bool(reduce_only),
        }
        if price is not None:
            body["price"] = str(price)
        if client_id:
            body["clientId"] = client_id
        if effect:
            body["effect"] = effect
        if tp_price is not None:
            body["tpPrice"] = str(tp_price)
        if tp_stop_type:
            body["tpStopType"] = tp_stop_type
        if tp_order_type:
            body["tpOrderType"] = tp_order_type
        if tp_order_price is not None:
            body["tpOrderPrice"] = str(tp_order_price)
        if sl_price is not None:
            body["slPrice"] = str(sl_price)
        if sl_stop_type:
            body["slStopType"] = sl_stop_type
        if sl_order_type:
            body["slOrderType"] = sl_order_type
        if sl_order_price is not None:
            body["slOrderPrice"] = str(sl_order_price)

        try:
            logger.info("Bitunix: Placing order body=%s", json.dumps(body, indent=2))
            data = self._request(
                "POST", "/api/v1/futures/trade/place_order", body=body, auth=True
            )
            logger.info("Bitunix: Placed order data=%s", json.dumps(data, indent=2))
            if data.get("code") == 0:
                od = data.get("data") or {}
                return ExecutionResult(
                    True, str(od.get("orderId") or ""), "submitted", None
                )
            return ExecutionResult(False, None, "error", json.dumps(data))
        except Exception as e:
            return ExecutionResult(False, None, "error", str(e))
