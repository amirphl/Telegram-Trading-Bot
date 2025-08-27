import json
import logging
import os
import uuid
import pytest

from configs.config import load_config
from internal.services.exchange_bitunix import BitunixClient


logger = logging.getLogger(__name__)

REQUIRED_ENVS = [
    "BITUNIX_API_KEY",
    "BITUNIX_SECRET",
    "PROXY_TYPE",
    "PROXY_HOST",
    "PROXY_PORT",
]


def _require_bitunix_creds_and_proxy():
    missing = [k for k in REQUIRED_ENVS if not os.getenv(k)]
    if missing:
        pytest.skip(f"Missing Bitunix/Proxy envs: {', '.join(missing)}")


def _client_and_symbol():
    cfg = load_config()
    client = BitunixClient(cfg)
    token = os.getenv("TEST_TOKEN", "ALGO")
    symbol = client.swap_symbol(token, cfg.order_quote)
    return cfg, client, symbol


@pytest.mark.live
def test_fetch_tickers():
    _require_bitunix_creds_and_proxy()
    _, client, symbol = _client_and_symbol()
    logger.info(f"Testing fetch_tickers for symbol: {symbol}")
    base = symbol  # e.g., BTCUSDT
    data = client.fetch_tickers([base])
    logger.info(f"Received ticker data keys: {list(data.keys())}")
    logger.info(f"Fetch tickers data={json.dumps(data, indent=2)}")
    assert isinstance(data, dict)
    assert base in data


@pytest.mark.live
def test_fetch_price():
    _require_bitunix_creds_and_proxy()
    _, client, symbol = _client_and_symbol()
    logger.info(f"Testing fetch_price for symbol: {symbol}")
    price = client.fetch_price(symbol)
    logger.info(f"Current price for {symbol}: {price}")
    assert price is not None and price > 0


@pytest.mark.live
def test_get_account_and_balance():
    _require_bitunix_creds_and_proxy()
    cfg, client, _ = _client_and_symbol()
    logger.info(f"Testing account info for margin coin: {cfg.order_quote}")
    acct = client.get_account(cfg.order_quote)
    assert acct and acct.get("code") == 0
    bal = client.get_available_balance(cfg.order_quote)
    logger.info(f"Available balance for {cfg.order_quote}: {bal}")
    assert bal is not None and bal >= 0


@pytest.mark.live
def test_limit_order_buy():
    _require_bitunix_creds_and_proxy()
    cfg, client, symbol = _client_and_symbol()
    logger.info(f"Testing limit order for symbol: {symbol}")
    price = client.fetch_price(symbol)
    assert price and price > 0
    budget = float(os.getenv("TEST_BUDGET_USDT", "10"))
    qty = budget / price
    qty = max(qty, 1e-6)

    # Place a limit slightly away to reduce immediate fill
    limit_price = price * 1.01
    tp_price = price * 1.02
    sl_price = price * 0.99
    lev = int(os.getenv("TEST_LEVERAGE", "3"))

    logger.info(
        f"Order details: price={price}, limit_price={limit_price}, qty={qty}, leverage={lev}"
    )

    params = {
        "reduceOnly": False,
        "tpPrice": float(tp_price),
        "tpStopType": "LAST_PRICE",
        "tpOrderType": "LIMIT",
        "tpOrderPrice": float(tp_price),
        "slPrice": float(sl_price),
        "slStopType": "LAST_PRICE",
        "slOrderType": "MARKET",
        "slOrderPrice": None,
        "clientId": f"test-{uuid.uuid4().hex[:12]}",
        "effect": "GTC",
        "leverage": lev,
        "marginCoin": cfg.order_quote,
    }

    logger.info(f"Submitting limit order with params: {json.dumps(params, indent=2)}")
    res = client.limit_order(
        symbol, side="BUY", amount=qty, price=float(limit_price), params=params
    )
    logger.info(
        f"Order result: success={res.success}, order_id={res.order_id}, status={res.status}"
    )
    assert res is not None
    assert res.success, f"Limit order failed: status={res.status} error={res.error}"

