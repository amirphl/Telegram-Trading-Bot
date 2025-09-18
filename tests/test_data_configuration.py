import os
import sqlite3
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
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
if "telethon" not in sys.modules:
    telethon = types.ModuleType("telethon")
    sys.modules["telethon"] = telethon
    for name in ("telethon.tl", "telethon.tl.custom"):
        sys.modules[name] = types.ModuleType(name)
    message_module = types.ModuleType("telethon.tl.custom.message")
    message_module.Message = object
    sys.modules["telethon.tl.custom.message"] = message_module


from configs.config import ConfigValidationError, load_config
from internal.db.sqlite import init_db
from internal.repositories.messages import (
    MediaPolicy,
    cleanup_media_storage,
    persist_message,
)
from internal.services.exchange_lbank import ExecutionRejected, LBankClient

PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    + b"\x00\x00\x00\rIHDR"
    + (2).to_bytes(4, "big")
    + (3).to_bytes(4, "big")
    + b"\x08\x02\x00\x00\x00"
)


def valid_environment(media_dir: Path, **overrides):
    values = {
        "API_ID": "1",
        "API_HASH": "hash",
        "CHANNEL_ID": "123",
        "MEDIA_DIR": str(media_dir),
    }
    values.update(overrides)
    return values


def media_policy():
    return MediaPolicy(
        max_file_bytes=1024,
        max_total_bytes=2048,
        max_pixels=100,
        max_images=4,
        max_disk_bytes=4096,
        retention_days=0,
    )


class FailingMediaMessage:
    def __init__(self, *, fail: bool) -> None:
        self.id = 1
        self.chat_id = -100
        self.message = "image"
        self.date = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.edit_date = None
        self.entities = None
        self.fwd_from = None
        self.views = None
        self.forwards = None
        self.replies = None
        self.post_author = None
        self.grouped_id = None
        self.reply_to = None
        self.via_bot_id = None
        self.media = object()
        self.file = SimpleNamespace(mime_type="image/png", size=len(PNG_BYTES), width=2, height=3)
        self.photo = None
        self.document = SimpleNamespace(attributes=[SimpleNamespace(w=2, h=3)])
        self.sticker = self.video = self.video_note = self.voice = None
        self.audio = self.gif = None
        self.fail = fail

    def to_dict(self):
        return {"id": self.id, "chat_id": self.chat_id, "message": self.message}

    async def download_media(self, file):
        path = Path(file).with_suffix(".png")
        path.write_bytes(PNG_BYTES)
        if self.fail:
            raise OSError("simulated interrupted download")
        return str(path)


class ConfigurationValidationTests(unittest.TestCase):
    def test_missing_and_mistyped_required_values_are_actionable(self):
        with tempfile.TemporaryDirectory() as tmp:
            media_dir = Path(tmp) / "must-not-exist"
            environment = valid_environment(media_dir)
            environment.pop("API_ID")
            with patch.dict(os.environ, environment, clear=True):
                with self.assertRaisesRegex(ConfigValidationError, "API_ID is required"):
                    load_config()
            self.assertFalse(media_dir.exists())

            environment["API_ID"] = "not-an-int"
            with patch.dict(os.environ, environment, clear=True):
                with self.assertRaisesRegex(ConfigValidationError, "API_ID must be an integer"):
                    load_config()

    def test_ranges_are_aggregated_and_validation_has_no_filesystem_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            media_dir = Path(tmp) / "not-created"
            environment = valid_environment(
                media_dir,
                BACKFILL="-1",
                HEARTBEAT_SECS="0",
                SQL_BUSY_RETRIES="-2",
                ORDER_NOTIONAL="-10",
                BALANCE_BUFFER_PCT="1.5",
                MEDIA_MAX_BYTES="0",
            )
            with patch.dict(os.environ, environment, clear=True):
                with self.assertRaises(ConfigValidationError) as raised:
                    load_config()
            message = str(raised.exception)
            for name in (
                "BACKFILL", "HEARTBEAT_SECS", "SQL_BUSY_RETRIES",
                "ORDER_NOTIONAL", "BALANCE_BUFFER_PCT", "MEDIA_MAX_BYTES",
            ):
                self.assertIn(name, message)
            self.assertFalse(media_dir.exists())

    def test_valid_loading_still_defers_directory_creation_to_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            media_dir = Path(tmp) / "runtime-owned"
            with patch.dict(os.environ, valid_environment(media_dir), clear=True):
                cfg = load_config()
            self.assertEqual(cfg.media_dir, media_dir)
            self.assertFalse(media_dir.exists())


class PersistenceRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_interrupted_download_is_explicit_and_cleanup_removes_staging_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            media_dir = Path(tmp) / "media"
            conn = sqlite3.connect(":memory:", isolation_level=None)
            self.addCleanup(conn.close)
            init_db(conn)
            with self.assertRaises(OSError):
                await persist_message(
                    conn, FailingMediaMessage(fail=True), "channel", media_dir,
                    1, 0.0, media_policy=media_policy(),
                )
            state = conn.execute(
                "SELECT persistence_status,persistence_error FROM messages"
            ).fetchone()
            self.assertEqual(state[0], "repair_required")
            self.assertIn("OSError", state[1])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM media_files").fetchone()[0], 0)
            result = cleanup_media_storage(conn, media_dir)
            self.assertGreaterEqual(result["removed_files"], 1)
            self.assertFalse(any(path.is_file() for path in media_dir.rglob("*")))

    async def test_success_uses_canonical_final_path_and_atomic_terminal_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            media_dir = Path(tmp) / "media"
            conn = sqlite3.connect(":memory:", isolation_level=None)
            self.addCleanup(conn.close)
            init_db(conn)
            paths = await persist_message(
                conn, FailingMediaMessage(fail=False), "channel", media_dir,
                1, 0.0, media_policy=media_policy(),
            )
            self.assertEqual(paths[0].name, "message_1.png")
            self.assertFalse(any("pending" in path.name for path in media_dir.rglob("*")))
            state, stored = conn.execute(
                """
                SELECT m.persistence_status,f.local_path FROM messages m
                JOIN media_files f USING(chat_id,message_id)
                """
            ).fetchone()
            self.assertEqual(state, "complete")
            self.assertEqual(Path(stored), paths[0])

    async def test_startup_reconciles_pending_database_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            media_dir = Path(tmp) / "media"
            conn = sqlite3.connect(":memory:", isolation_level=None)
            self.addCleanup(conn.close)
            init_db(conn)
            conn.execute(
                """
                INSERT INTO messages
                  (chat_id,message_id,date_utc,raw_json,persistence_status)
                VALUES(-100,1,'now','{}','pending_media')
                """
            )
            result = cleanup_media_storage(conn, media_dir)
            self.assertEqual(result["repaired_messages"], 1)
            self.assertEqual(
                conn.execute("SELECT persistence_status FROM messages").fetchone()[0],
                "repair_required",
            )


class MigrationIntegrityTests(unittest.TestCase):
    def test_legacy_schema_is_versioned_repeatable_and_retains_orphan_audit(self):
        conn = sqlite3.connect(":memory:", isolation_level=None)
        self.addCleanup(conn.close)
        conn.execute(
            """
            CREATE TABLE positions_submitted (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
              symbol TEXT NOT NULL, side TEXT NOT NULL, quantity REAL NOT NULL,
              price REAL, leverage REAL, order_id TEXT, status TEXT NOT NULL,
              error TEXT, created_at_utc TEXT NOT NULL, updated_at_utc TEXT NOT NULL,
              UNIQUE(chat_id,message_id)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO positions_submitted
              (chat_id,message_id,symbol,side,quantity,status,created_at_utc,updated_at_utc)
            VALUES(-100,7,'BTC/USDT','buy',1,'open','now','now')
            """
        )
        init_db(conn)
        self.assertEqual(
            conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall(),
            [(1,), (2,), (3,)],
        )
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 3)
        foreign_tables = {
            row[2] for row in conn.execute("PRAGMA foreign_key_list(positions_submitted)")
        }
        self.assertIn("messages", foreign_tables)
        placeholder = conn.execute(
            "SELECT persistence_status FROM messages WHERE chat_id=-100 AND message_id=7"
        ).fetchone()
        self.assertEqual(placeholder, ("repair_required",))
        init_db(conn)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM positions_submitted").fetchone()[0], 1)
        self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_constraints_reject_invalid_lifecycle_values_and_relationships(self):
        conn = sqlite3.connect(":memory:", isolation_level=None)
        self.addCleanup(conn.close)
        init_db(conn)
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO positions_submitted
                  (chat_id,message_id,symbol,side,quantity,status,created_at_utc,updated_at_utc)
                VALUES(-1,1,'BTC/USDT','buy',1,'open','now','now')
                """
            )
        conn.execute(
            "INSERT INTO messages(chat_id,message_id,date_utc,raw_json) VALUES(-1,1,'now','{}')"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO trade_signals
                  (chat_id,message_id,position_type,entry_price,created_at_utc)
                VALUES(-1,1,'sideways',10,'now')
                """
            )
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO positions_submitted
                  (chat_id,message_id,symbol,side,quantity,status,created_at_utc,updated_at_utc)
                VALUES(-1,1,'BTC/USDT','buy',-1,'open','now','now')
                """
            )


class CredentialMappingTests(unittest.TestCase):
    def config(self, **overrides):
        values = {
            "lbank_api_key": "api-key-value",
            "lbank_secret": "secret-value",
            "lbank_password": "password-value",
            "exchange_timeout_secs": 30,
            "execution_market_type": "spot",
            "trading_mode": "sandbox",
            "order_quote": "USDT",
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_password_is_top_level_required_credentials_are_checked_and_auth_is_read_only(self):
        captured = {}

        class Exchange:
            requiredCredentials = {"apiKey": True, "secret": True, "password": True}

            def __init__(self, config):
                captured.update(config)
                self.checked = 0
                self.balance_calls = 0

            def check_required_credentials(self):
                self.checked += 1

            def set_sandbox_mode(self, enabled):
                self.sandbox = enabled

            def fetch_balance(self, params):
                self.balance_calls += 1
                return {"USDT": {"free": 1}}

        exchange = Exchange({})
        with patch(
            "internal.services.exchange_lbank.ccxt.lbank",
            side_effect=lambda config: (Exchange(config)),
        ):
            client = LBankClient(self.config())
        self.assertEqual(captured["password"], "password-value")
        self.assertNotIn("password", captured["options"])
        client.check_authentication()
        self.assertGreaterEqual(client.exchange.checked, 2)
        self.assertEqual(client.exchange.balance_calls, 1)
        self.assertFalse(hasattr(client.exchange, "orders"))

    def test_missing_required_password_and_auth_errors_never_expose_secrets(self):
        class Exchange:
            requiredCredentials = {"apiKey": True, "secret": True, "password": True}

            def set_sandbox_mode(self, enabled):
                pass

        with self.assertRaisesRegex(ExecutionRejected, "password") as missing:
            LBankClient(self.config(lbank_password=None), exchange=Exchange())
        self.assertNotIn("secret-value", str(missing.exception))

        class RejectingExchange(Exchange):
            requiredCredentials = {"apiKey": True, "secret": True, "password": False}

            def fetch_balance(self, params):
                raise RuntimeError("server echoed secret-value")

        client = LBankClient(
            self.config(lbank_password=None), exchange=RejectingExchange()
        )
        with self.assertRaises(ExecutionRejected) as rejected:
            client.check_authentication()
        self.assertNotIn("secret-value", str(rejected.exception))


if __name__ == "__main__":
    unittest.main()
