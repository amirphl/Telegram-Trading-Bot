import os
import sqlite3
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

if "dotenv" not in sys.modules:
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda: None
    sys.modules["dotenv"] = dotenv
if "ccxt" not in sys.modules:
    ccxt = types.ModuleType("ccxt")
    ccxt.lbank = lambda config: SimpleNamespace(config=config)
    sys.modules["ccxt"] = ccxt


from configs.config import (
    LIVE_TRADING_CONFIRMATION,
    _parse_bool,
    load_config,
    validate_execution_authorization,
)
from internal.db.sqlite import init_db
from internal.repositories.positions import (
    SubmittedPosition,
    claim_position_submission,
    get_submitted_position,
    update_position_plan,
)
from internal.repositories.signals import TradeSignal
from internal.services.exchange_lbank import (
    ExecutionRejected,
    LBankClient,
    normalize_token,
    stable_client_order_id,
)
from internal.services.executor import (
    reconcile_pending_positions,
    reconcile_protective_orders,
    submit_position_if_enabled,
)


class RequestTimeout(Exception):
    pass


def trade_config(**overrides):
    values = {
        "enable_auto_execution": True,
        "lbank_api_key": "key",
        "lbank_secret": "secret",
        "lbank_password": None,
        "trading_mode": "sandbox",
        "live_trading_confirmation": None,
        "execution_market_type": "swap",
        "margin_mode": "isolated",
        "require_protective_orders": True,
        "ticker_max_age_secs": 15,
        "balance_buffer_pct": 0.01,
        "max_leverage": 5.0,
        "order_quote": "USDT",
        "order_notional": 100.0,
        "max_price_deviation_pct": 0.02,
        "sql_busy_retries": 1,
        "sql_busy_sleep": 0.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def signal(**overrides):
    values = {
        "chat_id": -100123,
        "message_id": 77,
        "token": "BTCUSDT",
        "position_type": "long",
        "entry_price": 100.0,
        "leverage": None,
        "stop_losses": [90.0],
        "take_profits": [120.0],
        "model_name": "test",
    }
    values.update(overrides)
    return TradeSignal(**values)


class FakeExchange:
    def __init__(self, market_type="spot"):
        self.market_type = market_type
        symbol = "BTC/USDT" if market_type == "spot" else "BTC/USDT:USDT"
        self.market = {
            "id": "btcusdt",
            "symbol": symbol,
            "base": "BTC",
            "quote": "USDT",
            "settle": "USDT" if market_type == "swap" else None,
            "spot": market_type == "spot",
            "swap": market_type == "swap",
            "active": True,
            "contractSize": 1.0,
            "limits": {
                "amount": {"min": 0.001, "max": 1000.0},
                "cost": {"min": 10.0, "max": 1_000_000.0},
                "leverage": {"min": 1.0, "max": 10.0},
            },
        }
        self.has = {
            "createMarketOrder": True,
            "setLeverage": market_type == "swap",
            "fetchOrder": True,
            "fetchOrders": True,
            "fetchOpenOrders": True,
            "fetchClosedOrders": True,
            "cancelOrder": True,
        }
        self.orders = []
        self.canceled = []
        self.leverage_calls = []
        self.load_count = 0
        self.sandbox = False
        self.price = 100.0
        self.ticker_timestamp = int(time.time() * 1000)
        self.free_balance = 10_000.0
        self.feature_support = True
        self.entry_timeout_after_accept = False
        self.entry_timeout_without_accept = False
        self.protection_error = None

    def set_sandbox_mode(self, enabled):
        self.sandbox = enabled

    def load_markets(self):
        self.load_count += 1
        return {self.market["symbol"]: self.market}

    def feature_value(self, symbol, method, feature):
        return self.feature_support

    def fetch_ticker(self, symbol):
        return {"last": self.price, "timestamp": self.ticker_timestamp}

    def amount_to_precision(self, symbol, amount):
        return f"{amount:.8f}"

    def fetch_balance(self, params):
        return {"USDT": {"free": self.free_balance}}

    def set_leverage(self, leverage, symbol, params):
        self.leverage_calls.append((leverage, symbol, params))

    def create_order(self, symbol, order_type, side, amount, price, params):
        is_protection = "stopLossPrice" in params or "takeProfitPrice" in params
        is_emergency = "emergency" in params.get("clientOrderId", "")
        if is_protection and self.protection_error is not None:
            raise self.protection_error
        order = {
            "id": f"order-{len(self.orders) + 1}",
            "clientOrderId": params.get("clientOrderId"),
            "symbol": symbol,
            "side": side,
            "amount": amount,
            "filled": amount,
            "average": self.price,
            "cost": amount * self.price,
            "fee": {"cost": 0.1, "currency": "USDT"},
            "status": "closed" if not is_protection else "open",
            "params": dict(params),
            "emergency": is_emergency,
        }
        if not is_protection and not is_emergency and self.entry_timeout_without_accept:
            raise RequestTimeout("entry outcome unknown")
        self.orders.append(order)
        if not is_protection and not is_emergency and self.entry_timeout_after_accept:
            raise RequestTimeout("response lost after acceptance")
        return order

    def fetch_order(self, order_id, symbol):
        for order in self.orders:
            if order["id"] == order_id:
                return order
        raise LookupError(order_id)

    def fetch_orders(self, symbol):
        return list(self.orders)

    def fetch_open_orders(self, symbol):
        return [order for order in self.orders if order["status"] == "open"]

    def fetch_closed_orders(self, symbol):
        return [order for order in self.orders if order["status"] == "closed"]

    def cancel_order(self, order_id, symbol):
        self.canceled.append(order_id)
        for order in self.orders:
            if order["id"] == order_id:
                order["status"] = "canceled"
                return order
        raise LookupError(order_id)


class TradeTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:", isolation_level=None)
        self.addCleanup(self.conn.close)
        self.conn.execute("PRAGMA foreign_keys=ON")
        init_db(self.conn)
        self.conn.execute(
            """
            INSERT INTO messages(chat_id,message_id,date_utc,text,raw_json)
            VALUES(-100123,77,'now','test signal','{}')
            """
        )

    def client(self, cfg, exchange=None):
        return LBankClient(cfg, exchange=exchange or FakeExchange(cfg.execution_market_type))


class ConfigurationSafetyTests(TradeTestCase):
    def test_auto_execution_boolean_is_strict_and_defaults_off(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(_parse_bool("ENABLE_AUTO_EXECUTION", False))
        with patch.dict(os.environ, {"ENABLE_AUTO_EXECUTION": "FALSE"}, clear=True):
            self.assertFalse(_parse_bool("ENABLE_AUTO_EXECUTION", True))
        with patch.dict(os.environ, {"ENABLE_AUTO_EXECUTION": "maybe"}, clear=True):
            with self.assertRaises(ValueError):
                _parse_bool("ENABLE_AUTO_EXECUTION", False)

    def test_live_mode_requires_exact_confirmation(self):
        cfg = trade_config(trading_mode="live", live_trading_confirmation=None)
        with self.assertRaises(ValueError):
            validate_execution_authorization(cfg)
        cfg.live_trading_confirmation = LIVE_TRADING_CONFIRMATION
        validate_execution_authorization(cfg)

    def test_enabled_execution_requires_explicit_market_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            environment = {
                "API_ID": "1",
                "API_HASH": "hash",
                "CHANNEL_ID": "123",
                "MEDIA_DIR": str(Path(tmp) / "media"),
                "ENABLE_AUTO_EXECUTION": "true",
                "LBANK_API_KEY": "key",
                "LBANK_SECRET": "secret",
            }
            with patch.dict(os.environ, environment, clear=True):
                with self.assertRaisesRegex(ValueError, "explicit EXECUTION_MARKET_TYPE"):
                    load_config()

    def test_existing_position_table_is_upgraded_safely(self):
        old = sqlite3.connect(":memory:", isolation_level=None)
        self.addCleanup(old.close)
        old.execute(
            """
            CREATE TABLE positions_submitted (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              chat_id INTEGER NOT NULL,
              message_id INTEGER NOT NULL,
              symbol TEXT NOT NULL,
              side TEXT NOT NULL,
              quantity REAL NOT NULL,
              price REAL,
              leverage REAL,
              order_id TEXT,
              status TEXT NOT NULL,
              error TEXT,
              created_at_utc TEXT NOT NULL,
              updated_at_utc TEXT NOT NULL,
              UNIQUE(chat_id, message_id)
            )
            """
        )
        init_db(old)
        columns = {
            row[1] for row in old.execute("PRAGMA table_info(positions_submitted)").fetchall()
        }
        self.assertIn("client_order_id", columns)
        self.assertIn("price_timestamp_utc", columns)
        self.assertIn("protective_orders_json", columns)


class IdempotencyAndReconciliationTests(TradeTestCase):
    def test_duplicate_message_creates_only_one_entry_order(self):
        cfg = trade_config()
        exchange = FakeExchange(cfg.execution_market_type)
        client = self.client(cfg, exchange)
        first = submit_position_if_enabled(cfg, self.conn, signal(), client=client)
        second = submit_position_if_enabled(cfg, self.conn, signal(), client=client)

        entry_orders = [order for order in exchange.orders if not any(
            key in order["params"] for key in ("stopLossPrice", "takeProfitPrice")
        )]
        self.assertTrue(first.success)
        self.assertEqual(second.status, "duplicate_ignored")
        self.assertEqual(len(entry_orders), 1)

    def test_ambiguous_timeout_reconciles_without_resubmission(self):
        cfg = trade_config()
        exchange = FakeExchange(cfg.execution_market_type)
        exchange.entry_timeout_after_accept = True
        result = submit_position_if_enabled(cfg, self.conn, signal(), client=self.client(cfg, exchange))
        entry_orders = [order for order in exchange.orders if order["side"] == "buy" and not order["emergency"]]
        self.assertTrue(result.success)
        self.assertTrue(result.reconciled)
        self.assertEqual(len(entry_orders), 1)

    def test_unknown_timeout_is_not_retried_and_enters_reconciliation_state(self):
        cfg = trade_config()
        exchange = FakeExchange(cfg.execution_market_type)
        exchange.entry_timeout_without_accept = True
        result = submit_position_if_enabled(cfg, self.conn, signal(), client=self.client(cfg, exchange))
        self.assertEqual(result.status, "unknown_requires_reconciliation")
        self.assertEqual(exchange.orders, [])
        self.assertEqual(get_submitted_position(self.conn, -100123, 77)["status"], result.status)

    def test_reconciliation_worker_never_resubmits_unknown_order(self):
        cfg = trade_config(execution_market_type="spot", require_protective_orders=False)
        exchange = FakeExchange(cfg.execution_market_type)
        client = self.client(cfg, exchange)
        sig = signal(stop_losses=[], take_profits=[])
        client_id = stable_client_order_id(sig.chat_id, sig.message_id)
        candidate = SubmittedPosition(
            sig.chat_id, sig.message_id, "BTC/USDT", "buy", 0.0, 100.0,
            None, None, "claimed", client_order_id=client_id, market_type="spot"
        )
        self.assertTrue(claim_position_submission(self.conn, candidate, 1, 0.0))
        plan = client.prepare_signal("BTC", "long", 100, None, [], [], client_id)
        update_position_plan(self.conn, sig.chat_id, sig.message_id, plan, 1, 0.0)
        exchange.orders.append({
            "id": "accepted", "clientOrderId": client_id, "symbol": "BTC/USDT",
            "side": "buy", "filled": 1.0, "average": 100.0, "cost": 100.0,
            "status": "closed", "fee": None,
        })

        summary = reconcile_pending_positions(cfg, self.conn, client=client)
        self.assertEqual(summary["found"], 1)
        self.assertEqual(len(exchange.orders), 1)
        self.assertEqual(get_submitted_position(self.conn, sig.chat_id, sig.message_id)["status"], "reconciled_entry_found")


class MarketAndExecutionTests(TradeTestCase):
    def test_symbol_normalization_is_strict(self):
        self.assertEqual(normalize_token("BTCUSDT", "USDT"), "BTC")
        self.assertEqual(normalize_token("btc/usdt", "USDT"), "BTC")
        with self.assertRaises(ExecutionRejected):
            normalize_token("$BTC", "USDT")
        with self.assertRaises(ExecutionRejected):
            normalize_token("BTC/EUR", "USDT")

    def test_spot_short_and_spot_leverage_are_rejected(self):
        cfg = trade_config(execution_market_type="spot")
        exchange = FakeExchange(cfg.execution_market_type)
        short = submit_position_if_enabled(
            cfg,
            self.conn,
            signal(position_type="short", stop_losses=[], take_profits=[]),
            client=self.client(cfg, exchange),
        )
        self.assertEqual(short.status, "validation_rejected")
        self.assertEqual(exchange.orders, [])

    def test_swap_short_sets_leverage_and_uses_reduce_only_protection(self):
        cfg = trade_config(execution_market_type="swap")
        exchange = FakeExchange("swap")
        result = submit_position_if_enabled(
            cfg,
            self.conn,
            signal(position_type="short", leverage=3, stop_losses=[110], take_profits=[80]),
            client=self.client(cfg, exchange),
        )
        self.assertTrue(result.success)
        self.assertEqual(exchange.leverage_calls[0][0], 3.0)
        entry = exchange.orders[0]
        self.assertEqual(entry["side"], "sell")
        self.assertFalse(entry["params"]["reduceOnly"])
        protections = exchange.orders[1:]
        self.assertTrue(all(order["side"] == "buy" for order in protections))
        self.assertTrue(all(order["params"]["reduceOnly"] for order in protections))

    def test_missing_or_stale_ticker_fails_closed(self):
        cfg = trade_config(execution_market_type="spot", require_protective_orders=False)
        missing = FakeExchange(cfg.execution_market_type)
        missing.price = None
        bare_signal = signal(stop_losses=[], take_profits=[])
        result = submit_position_if_enabled(cfg, self.conn, bare_signal, client=self.client(cfg, missing))
        self.assertEqual(result.status, "validation_rejected")
        self.assertEqual(missing.orders, [])

        other_conn = sqlite3.connect(":memory:", isolation_level=None)
        self.addCleanup(other_conn.close)
        init_db(other_conn)
        other_conn.execute(
            """
            INSERT INTO messages(chat_id,message_id,date_utc,text,raw_json)
            VALUES(-100123,77,'now','test signal','{}')
            """
        )
        stale = FakeExchange(cfg.execution_market_type)
        stale.ticker_timestamp -= 60_000
        result = submit_position_if_enabled(cfg, other_conn, bare_signal, client=self.client(cfg, stale))
        self.assertIn("stale", result.error)
        self.assertEqual(stale.orders, [])

    def test_limits_balance_and_market_loading_are_enforced(self):
        cfg = trade_config(execution_market_type="spot", require_protective_orders=False)
        exchange = FakeExchange(cfg.execution_market_type)
        client = self.client(cfg, exchange)
        client.prepare_signal("BTC", "long", 100, None, [], [], "one")
        client.prepare_signal("BTC", "long", 100, None, [], [], "two")
        self.assertEqual(exchange.load_count, 1)

        exchange.free_balance = 1.0
        with self.assertRaisesRegex(ExecutionRejected, "insufficient"):
            client.prepare_signal("BTC", "long", 100, None, [], [], "three")

    def test_accurate_order_and_protection_details_are_persisted(self):
        cfg = trade_config()
        exchange = FakeExchange(cfg.execution_market_type)
        result = submit_position_if_enabled(cfg, self.conn, signal(), client=self.client(cfg, exchange))
        row = get_submitted_position(self.conn, -100123, 77)
        self.assertEqual(row["order_id"], result.order_id)
        self.assertEqual(row["client_order_id"], result.client_order_id)
        self.assertGreater(row["requested_quantity"], 0)
        self.assertEqual(row["filled_quantity"], result.filled_quantity)
        self.assertEqual(row["average_price"], 100.0)
        self.assertIsNotNone(row["fee_json"])
        self.assertIsNotNone(row["price_timestamp_utc"])
        self.assertEqual(row["price_source"], "lbank.fetch_ticker:last")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM protective_orders").fetchone()[0], 2
        )

    def test_protection_failure_closes_the_entry_instead_of_leaving_it_open(self):
        cfg = trade_config()
        exchange = FakeExchange(cfg.execution_market_type)
        exchange.protection_error = ValueError("unsupported trigger")
        result = submit_position_if_enabled(cfg, self.conn, signal(), client=self.client(cfg, exchange))
        self.assertFalse(result.success)
        self.assertEqual(result.status, "protection_failed_position_closed")
        emergency_id = stable_client_order_id(
            0, 0, f"{result.client_order_id}:emergency"
        )
        self.assertTrue(
            any(order["clientOrderId"] == emergency_id for order in exchange.orders)
        )

    def test_protective_reconciliation_cancels_siblings_after_trigger(self):
        cfg = trade_config()
        exchange = FakeExchange(cfg.execution_market_type)
        client = self.client(cfg, exchange)
        submit_position_if_enabled(cfg, self.conn, signal(), client=client)
        protective = [order for order in exchange.orders if any(
            key in order["params"] for key in ("stopLossPrice", "takeProfitPrice")
        )]
        protective[0]["status"] = "closed"
        summary = reconcile_protective_orders(cfg, self.conn, client=client)
        self.assertGreaterEqual(summary["siblings_canceled"], 1)


if __name__ == "__main__":
    unittest.main()
