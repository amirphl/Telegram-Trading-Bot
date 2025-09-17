import io
import json
import os
import sqlite3
import sys
import tempfile
import types
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def _install_stubs():
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
        telethon.TelegramClient = object
        builder = type("Builder", (), {})
        telethon.events = SimpleNamespace(
            NewMessage=type("NewMessage", (builder,), {}),
            Album=type("Album", (builder,), {}),
            MessageEdited=type("MessageEdited", (builder,), {}),
            MessageDeleted=type("MessageDeleted", (builder,), {}),
        )
        telethon.utils = SimpleNamespace(get_peer_id=lambda entity: -1_000_000_000_000 - entity.id)
        sys.modules["telethon"] = telethon
        errors = types.ModuleType("telethon.errors")
        errors.FloodWaitError = type("FloodWaitError", (Exception,), {})
        sys.modules["telethon.errors"] = errors
        for name in ("telethon.tl", "telethon.tl.custom"):
            sys.modules[name] = types.ModuleType(name)
        message_module = types.ModuleType("telethon.tl.custom.message")
        message_module.Message = object
        sys.modules["telethon.tl.custom.message"] = message_module
        types_module = types.ModuleType("telethon.tl.types")
        types_module.PeerChannel = lambda value: value
        sys.modules["telethon.tl.types"] = types_module


_install_stubs()

from configs.config import load_config
from internal.db.sqlite import init_db
from internal.repositories.extraction_jobs import (
    claim_extraction_job,
    enqueue_extraction_job,
    fail_extraction_job,
    recover_interrupted_jobs,
    replay_failed_job,
)
from internal.services.backfill import backfill_recent
from internal.services.openai_client import OpenAIExtractionError, OpenAIExtractor
from internal.services.signal_extraction import (
    process_extraction_job,
    process_signal_message,
)
from internal.services.signal_validation import validate_signal_output


def cfg(**overrides):
    values = {
        "openai_api_key": "test-key",
        "openai_model": "test-model",
        "openai_timeout_secs": 1,
        "openai_base_url": None,
        "media_max_bytes": 1024,
        "media_max_total_bytes": 2048,
        "media_max_pixels": 1000,
        "media_max_images": 4,
        "media_max_disk_bytes": 4096,
        "media_retention_days": 0,
        "media_dir": Path(tempfile.gettempdir()),
        "sql_busy_retries": 1,
        "sql_busy_sleep": 0.0,
        "signal_extraction_enabled": True,
        "signal_approval_mode": "automatic",
        "signal_token_allowlist": ("BTC",),
        "signal_max_age_secs": 300,
        "signal_min_confidence": 0.75,
        "signal_max_open_positions": 3,
        "signal_max_total_notional": 1000.0,
        "extraction_max_attempts": 2,
        "extraction_retry_base_secs": 1,
        "enable_auto_execution": False,
        "order_notional": 10.0,
        "max_leverage": 5.0,
        "historical_signal_policy": "store_only",
        "max_backoff_secs": 1,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def model_result(**overrides):
    value = {
        "is_signal": True,
        "token": "BTC",
        "position_type": "long",
        "entry_price": 100.0,
        "leverage": 2.0,
        "stop_losses": [90.0, 95.0],
        "take_profits": [110.0, 120.0],
        "confidence": 0.9,
    }
    value.update(overrides)
    return value


def add_message(conn, message_id=1, text="BTC long", date=None):
    timestamp = (date or datetime.now(timezone.utc)).isoformat()
    conn.execute(
        "INSERT INTO messages(chat_id,message_id,date_utc,text,raw_json) VALUES(-100,?,?,?,'{}')",
        (message_id, timestamp, text),
    )


class FakeMessage:
    def __init__(self, message_id=1, text="BTC long"):
        self.chat_id = -100
        self.id = message_id
        self.message = text
        self.date = datetime.now(timezone.utc)
        self.edit_date = None
        self.media = None
        self.entities = None
        self.fwd_from = None
        self.views = None
        self.forwards = None
        self.replies = None
        self.post_author = None
        self.grouped_id = None
        self.reply_to = None
        self.via_bot_id = None

    def to_dict(self):
        return {"id": self.id, "message": self.message}


class StrictOutputTests(unittest.TestCase):
    def test_request_uses_strict_json_schema(self):
        extractor = OpenAIExtractor(cfg())
        captured = {}
        extractor._request = lambda payload: captured.update(payload) or {
            "choices": [{"message": {"content": json.dumps(model_result())}}]
        }
        extractor.extract_signal("signal", [])
        response_format = captured["response_format"]
        self.assertEqual(response_format["type"], "json_schema")
        self.assertTrue(response_format["json_schema"]["strict"])
        self.assertFalse(response_format["json_schema"]["schema"]["additionalProperties"])

    def test_local_validation_rejects_types_nonfinite_and_extra_fields(self):
        value = model_result(entry_price=float("nan"), leverage=True, extra="bad")
        result = validate_signal_output(value, cfg(), message_date=datetime.now(timezone.utc))
        self.assertFalse(result.valid)
        self.assertIn("entry_price_must_be_finite", result.errors)
        self.assertIn("leverage_must_be_number", result.errors)
        self.assertIn("unexpected_field:extra", result.errors)

    def test_local_validation_enforces_cross_field_and_order_rules(self):
        value = model_result(stop_losses=[95, 90], take_profits=[90])
        result = validate_signal_output(value, cfg(), message_date=datetime.now(timezone.utc))
        self.assertFalse(result.valid)
        self.assertIn("stop_losses_must_be_strictly_ascending", result.errors)
        self.assertIn("long_targets_must_be_above_entry", result.errors)

    def test_execution_requires_fresh_allowlisted_approved_signal_and_exposure(self):
        old = datetime.now(timezone.utc) - timedelta(hours=1)
        result = validate_signal_output(
            model_result(token="ETH", confidence=0.2),
            cfg(signal_approval_mode="manual"), message_date=old, historical=True,
        )
        self.assertTrue(result.valid)
        self.assertFalse(result.executable)
        self.assertIn("token_not_allowlisted", result.execution_blockers)
        self.assertIn("signal_too_old", result.execution_blockers)
        self.assertIn("manual_approval_required", result.execution_blockers)
        self.assertIn("historical_signal_non_executable", result.execution_blockers)

    def test_exposure_uses_configured_notional_when_exchange_cost_is_unknown(self):
        conn = sqlite3.connect(":memory:", isolation_level=None)
        self.addCleanup(conn.close)
        init_db(conn)
        conn.execute(
            "INSERT INTO messages(chat_id,message_id,date_utc,text,raw_json) "
            "VALUES(-100,9,'now','existing position','{}')"
        )
        conn.execute(
            """
            INSERT INTO positions_submitted
              (chat_id,message_id,symbol,side,quantity,status,created_at_utc,updated_at_utc)
            VALUES(-100,9,'BTC/USDT','buy',1,'open','now','now')
            """
        )
        result = validate_signal_output(
            model_result(), cfg(enable_auto_execution=True, order_notional=60,
                                signal_max_total_notional=100),
            message_date=datetime.now(timezone.utc), conn=conn,
        )
        self.assertIn("total_notional_limit_exceeded", result.execution_blockers)


class FailureClassificationTests(unittest.TestCase):
    def _http_error(self, status):
        return urllib.error.HTTPError(
            "https://api.openai.com", status, "error", {}, io.BytesIO(b"{}")
        )

    def test_auth_and_rate_limit_failures_are_classified(self):
        extractor = OpenAIExtractor(cfg())
        with patch("urllib.request.urlopen", side_effect=self._http_error(401)):
            with self.assertRaises(OpenAIExtractionError) as caught:
                extractor._request({})
        self.assertEqual(caught.exception.code, "authentication_error")
        self.assertFalse(caught.exception.retryable)
        caught.exception.__cause__.close()
        with patch("urllib.request.urlopen", side_effect=self._http_error(429)):
            with self.assertRaises(OpenAIExtractionError) as caught:
                extractor._request({})
        self.assertEqual(caught.exception.code, "rate_limit_or_quota")
        self.assertTrue(caught.exception.retryable)
        caught.exception.__cause__.close()

    def test_refusal_and_bad_shape_are_not_silently_dropped(self):
        extractor = OpenAIExtractor(cfg())
        extractor._request = lambda payload: {"choices": [{"message": {"refusal": "no"}}]}
        with self.assertRaises(OpenAIExtractionError) as caught:
            extractor.extract_signal("signal", [])
        self.assertEqual(caught.exception.code, "model_refusal")


class DurableQueueTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:", isolation_level=None)
        self.addCleanup(self.conn.close)
        self.conn.execute("PRAGMA foreign_keys=ON")
        init_db(self.conn)
        self.context = SimpleNamespace(
            db_conn=self.conn, cfg=cfg(), target_id=-100, channel_title_for_path="channel"
        )

    def test_retry_is_bounded_and_replay_disables_execution(self):
        add_message(self.conn)
        enqueue_extraction_job(
            self.conn, -100, 1, 2, historical=False, allow_execution=True,
        )
        self.assertIsNotNone(claim_extraction_job(self.conn, -100, 1))
        status = fail_extraction_job(
            self.conn, -100, 1, "rate_limit_or_quota", "quota reached",
            retryable=True, retry_base_secs=1,
        )
        self.assertEqual(status, "retrying")
        self.conn.execute("UPDATE signal_extraction_jobs SET next_attempt_at_utc=NULL")
        self.assertIsNotNone(claim_extraction_job(self.conn, -100, 1))
        status = fail_extraction_job(
            self.conn, -100, 1, "rate_limit_or_quota", "quota reached",
            retryable=True, retry_base_secs=1,
        )
        self.assertEqual(status, "failed")
        self.assertTrue(replay_failed_job(self.conn, -100, 1))
        row = self.conn.execute(
            "SELECT status,attempts,allow_execution FROM signal_extraction_jobs"
        ).fetchone()
        self.assertEqual(row, ("pending", 0, 0))

    def test_interrupted_processing_is_recovered_after_restart(self):
        add_message(self.conn)
        enqueue_extraction_job(
            self.conn, -100, 1, 2, historical=False, allow_execution=True,
        )
        self.assertIsNotNone(claim_extraction_job(self.conn, -100, 1))
        summary = recover_interrupted_jobs(self.conn)
        self.assertEqual(summary, {"requeued": 1, "failed": 0})
        self.assertEqual(
            self.conn.execute("SELECT status,last_error_code FROM signal_extraction_jobs").fetchone(),
            ("retrying", "interrupted"),
        )

    async def test_processing_is_idempotent_and_persists_validation(self):
        message = FakeMessage()
        add_message(self.conn)
        with patch.object(OpenAIExtractor, "extract_signal", return_value=model_result()):
            await process_signal_message(self.context, message)
            await process_signal_message(self.context, message)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM trade_signals").fetchone()[0], 1)
        row = self.conn.execute(
            "SELECT status,validation_json FROM signal_extraction_jobs"
        ).fetchone()
        self.assertEqual(row[0], "completed")
        self.assertTrue(json.loads(row[1])["valid"])

    async def test_failure_code_is_stored_without_sensitive_input(self):
        add_message(self.conn, text="PRIVATE SIGNAL secret-token")
        enqueue_extraction_job(
            self.conn, -100, 1, 2, historical=False, allow_execution=False,
            input_text="PRIVATE SIGNAL secret-token",
        )
        error = OpenAIExtractionError("authentication_error", "OpenAI authentication failed", retryable=False)
        with patch.object(OpenAIExtractor, "extract_signal", side_effect=error):
            await process_extraction_job(self.context, -100, 1)
        row = self.conn.execute(
            "SELECT status,last_error_code,last_error FROM signal_extraction_jobs"
        ).fetchone()
        self.assertEqual(row, ("failed", "authentication_error", "OpenAI authentication failed"))
        event_detail = self.conn.execute(
            "SELECT detail FROM signal_extraction_events ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
        self.assertNotIn("secret-token", event_detail)


class HistoricalAndCredentialTests(unittest.IsolatedAsyncioTestCase):
    async def test_backfill_store_only_marks_jobs_non_executable(self):
        conn = sqlite3.connect(":memory:", isolation_level=None)
        self.addCleanup(conn.close)
        conn.execute("PRAGMA foreign_keys=ON")
        init_db(conn)
        context = SimpleNamespace(
            db_conn=conn, cfg=cfg(), target_id=-100, channel_title_for_path="channel"
        )

        class Client:
            async def iter_messages(self, entity, limit):
                yield FakeMessage()

        await backfill_recent(Client(), object(), context, 1)
        row = conn.execute(
            "SELECT status,historical,allow_execution FROM signal_extraction_jobs"
        ).fetchone()
        self.assertEqual(row, ("historical_skipped", 1, 0))

    def test_selected_extraction_mode_requires_openai_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            environment = {
                "API_ID": "1", "API_HASH": "hash", "CHANNEL_ID": "123",
                "MEDIA_DIR": str(Path(tmp) / "media"),
                "SIGNAL_EXTRACTION_ENABLED": "true",
            }
            with patch.dict(os.environ, environment, clear=True):
                with self.assertRaisesRegex(ValueError, "OPENAI_API_KEY"):
                    load_config()


if __name__ == "__main__":
    unittest.main()
