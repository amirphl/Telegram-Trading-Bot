import json
import sqlite3
import struct
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


def _install_dependency_stubs() -> None:
    if "dotenv" not in sys.modules:
        dotenv = types.ModuleType("dotenv")
        dotenv.load_dotenv = lambda: None
        sys.modules["dotenv"] = dotenv

    if "ccxt" not in sys.modules:
        ccxt = types.ModuleType("ccxt")
        ccxt.lbank = lambda config: SimpleNamespace(config=config)
        sys.modules["ccxt"] = ccxt

    if "telethon" in sys.modules:
        return

    class _Builder:
        pass

    events = SimpleNamespace(
        NewMessage=type("NewMessage", (_Builder,), {}),
        Album=type("Album", (_Builder,), {}),
        MessageEdited=type("MessageEdited", (_Builder,), {}),
        MessageDeleted=type("MessageDeleted", (_Builder,), {}),
    )
    telethon = types.ModuleType("telethon")
    telethon.events = events
    telethon.TelegramClient = object
    telethon.utils = SimpleNamespace(get_peer_id=lambda entity: -1_000_000_000_000 - entity.id)
    sys.modules["telethon"] = telethon

    tl = types.ModuleType("telethon.tl")
    custom = types.ModuleType("telethon.tl.custom")
    message_module = types.ModuleType("telethon.tl.custom.message")
    message_module.Message = object
    types_module = types.ModuleType("telethon.tl.types")
    types_module.PeerChannel = lambda value: value
    sys.modules["telethon.tl"] = tl
    sys.modules["telethon.tl.custom"] = custom
    sys.modules["telethon.tl.custom.message"] = message_module
    sys.modules["telethon.tl.types"] = types_module


_install_dependency_stubs()

from api.telegram import handlers
from api.telegram.utils import canonical_peer_id
from internal.db.sqlite import init_db
from internal.repositories.messages import (
    MediaPolicy,
    cleanup_media_storage,
    delete_message_and_media,
    inspect_telegram_image,
    persist_message,
)
from internal.services.openai_client import OpenAIExtractor
from internal.services.signal_extraction import classify_signal_input
from pkg.media import inspect_image_file, is_path_within

PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    + b"\x00\x00\x00\rIHDR"
    + struct.pack(">II", 2, 3)
    + b"\x08\x02\x00\x00\x00"
)


def media_policy(**overrides):
    values = {
        "max_file_bytes": 1024,
        "max_total_bytes": 2048,
        "max_pixels": 100,
        "max_images": 4,
        "max_disk_bytes": 4096,
        "retention_days": 0,
    }
    values.update(overrides)
    return MediaPolicy(**values)


def config_for(media_dir: Path, **overrides):
    values = {
        "media_dir": media_dir,
        "media_max_bytes": 1024,
        "media_max_total_bytes": 2048,
        "media_max_pixels": 100,
        "media_max_images": 4,
        "media_max_disk_bytes": 4096,
        "media_retention_days": 0,
        "sql_busy_retries": 1,
        "sql_busy_sleep": 0.0,
        "openai_api_key": "test-key",
        "openai_model": "test-model",
        "openai_timeout_secs": 1,
        "openai_base_url": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeMessage:
    def __init__(
        self,
        message_id=1,
        chat_id=-100123,
        text="",
        media=None,
        mime_type=None,
        file_size=None,
        width=2,
        height=3,
        grouped_id=None,
    ):
        self.id = message_id
        self.chat_id = chat_id
        self.message = text
        self.media = media
        self.date = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.edit_date = None
        self.entities = None
        self.fwd_from = None
        self.views = None
        self.forwards = None
        self.replies = None
        self.post_author = None
        self.grouped_id = grouped_id
        self.reply_to = None
        self.via_bot_id = None
        self.sticker = None
        self.video = None
        self.video_note = None
        self.voice = None
        self.audio = None
        self.gif = None
        self.download_count = 0
        if media:
            self.file = SimpleNamespace(mime_type=mime_type, size=file_size, width=width, height=height)
            self.photo = SimpleNamespace(sizes=[SimpleNamespace(w=width, h=height)]) if mime_type == "image/jpeg" else None
            self.document = None if self.photo else SimpleNamespace(attributes=[SimpleNamespace(w=width, h=height)])
        else:
            self.file = None
            self.photo = None
            self.document = None

    def to_dict(self):
        return {"id": self.id, "chat_id": self.chat_id, "message": self.message}

    async def download_media(self, file):
        self.download_count += 1
        path = Path(file).with_suffix(".png")
        path.write_bytes(PNG_BYTES)
        return str(path)


class FakeClient:
    def __init__(self):
        self.callbacks = {}

    def on(self, builder):
        def decorator(callback):
            self.callbacks[type(builder).__name__] = callback
            return callback

        return decorator


class TelegramHandlerTests(unittest.IsolatedAsyncioTestCase):
    def test_canonical_peer_id_uses_telethon_marking(self):
        self.assertEqual(canonical_peer_id(SimpleNamespace(id=123)), -1_000_000_000_123)

    async def test_marked_target_id_reaches_persistence(self):
        client = FakeClient()
        cfg = config_for(Path(tempfile.gettempdir()))
        ctx = SimpleNamespace(
            target_id=-100123,
            db_conn=object(),
            channel_title_for_path="ignored",
            cfg=cfg,
        )
        handlers.register_handlers(client, ctx)
        message = FakeMessage(text="BTC long")
        event = SimpleNamespace(chat_id=-100123, message=message)

        with patch.object(handlers, "persist_message", AsyncMock(return_value=[])) as persist_mock:
            with patch.object(handlers, "process_signal_message", AsyncMock()) as process_mock:
                await client.callbacks["NewMessage"](event)

        persist_mock.assert_awaited_once()
        process_mock.assert_awaited_once()

    async def test_album_over_limit_is_rejected_before_download(self):
        client = FakeClient()
        cfg = config_for(Path(tempfile.gettempdir()), media_max_images=1)
        ctx = SimpleNamespace(
            target_id=-100123,
            db_conn=object(),
            channel_title_for_path="ignored",
            cfg=cfg,
        )
        handlers.register_handlers(client, ctx)
        messages = [
            FakeMessage(1, media=object(), mime_type="image/jpeg", file_size=10, grouped_id=7),
            FakeMessage(2, media=object(), mime_type="image/jpeg", file_size=10, grouped_id=7),
        ]
        event = SimpleNamespace(chat_id=-100123, messages=messages)

        with patch.object(handlers, "persist_message", AsyncMock(return_value=[])) as persist_mock:
            with patch.object(handlers, "process_signal_message", AsyncMock()) as process_mock:
                await client.callbacks["Album"](event)

        self.assertEqual(persist_mock.await_count, 2)
        self.assertTrue(all(call.kwargs["skip_media_reason"] for call in persist_mock.await_args_list))
        process_mock.assert_not_awaited()

    async def test_edit_never_enables_automatic_execution(self):
        client = FakeClient()
        cfg = config_for(Path(tempfile.gettempdir()))
        ctx = SimpleNamespace(
            target_id=-100123,
            db_conn=object(),
            channel_title_for_path="ignored",
            cfg=cfg,
        )
        handlers.register_handlers(client, ctx)
        event = SimpleNamespace(chat_id=-100123, message=FakeMessage(text="edited signal"))

        with patch.object(handlers, "persist_message", AsyncMock(return_value=[])):
            with patch.object(handlers, "archive_message_revision"):
                with patch.object(handlers, "delete_unsubmitted_signal", return_value=True):
                    with patch.object(handlers, "process_signal_message", AsyncMock()) as process_mock:
                        await client.callbacks["MessageEdited"](event)

        self.assertFalse(process_mock.await_args.kwargs["allow_execution"])

    async def test_edit_after_order_is_retained_for_review_without_reextracting(self):
        client = FakeClient()
        cfg = config_for(Path(tempfile.gettempdir()))
        ctx = SimpleNamespace(
            target_id=-100123,
            db_conn=object(),
            channel_title_for_path="ignored",
            cfg=cfg,
        )
        handlers.register_handlers(client, ctx)
        event = SimpleNamespace(chat_id=-100123, message=FakeMessage(text="edited after trade"))

        with patch.object(handlers, "persist_message", AsyncMock(return_value=[])):
            with patch.object(handlers, "archive_message_revision"):
                with patch.object(handlers, "delete_unsubmitted_signal", return_value=False):
                    with patch.object(handlers, "record_processing_status") as status_mock:
                        with patch.object(handlers, "process_signal_message", AsyncMock()) as process_mock:
                            await client.callbacks["MessageEdited"](event)

        process_mock.assert_not_awaited()
        self.assertEqual(status_mock.call_args.args[3], "edited_after_order_review_required")


class SignalClassificationTests(unittest.TestCase):
    def test_text_only_and_captioned_images_are_supported(self):
        text_only = FakeMessage(text="BTC long")
        captioned = FakeMessage(text="ETH short", media=object(), mime_type="image/jpeg", file_size=10)
        self.assertEqual(classify_signal_input(text_only).kind, "text_only")
        self.assertEqual(classify_signal_input(captioned, [Path("image.jpg")]).kind, "captioned_image")

    def test_unsupported_and_oversized_media_are_rejected(self):
        pdf = FakeMessage(media=object(), mime_type="application/pdf", file_size=10)
        large = FakeMessage(media=object(), mime_type="image/png", file_size=2048)
        self.assertFalse(inspect_telegram_image(pdf, media_policy()).supported)
        self.assertIn("media_too_large", inspect_telegram_image(large, media_policy()).reason)


class MediaPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_storage_is_safe_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            media_dir = Path(tmp) / "media"
            conn = sqlite3.connect(":memory:", isolation_level=None)
            self.addCleanup(conn.close)
            conn.execute("PRAGMA foreign_keys=ON")
            init_db(conn)
            message = FakeMessage(media=object(), mime_type="image/png", file_size=len(PNG_BYTES))

            first = await persist_message(
                conn,
                message,
                "../../unsafe title",
                media_dir,
                1,
                0.0,
                media_policy=media_policy(),
            )
            second = await persist_message(
                conn,
                message,
                "different title",
                media_dir,
                1,
                0.0,
                media_policy=media_policy(),
            )

            self.assertEqual(first, second)
            self.assertEqual(message.download_count, 1)
            self.assertTrue(is_path_within(first[0], media_dir))
            self.assertNotIn("unsafe", str(first[0]))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM media_files").fetchone()[0], 1)

    async def test_rejection_reason_is_durable_and_orphans_are_cleaned(self):
        with tempfile.TemporaryDirectory() as tmp:
            media_dir = Path(tmp) / "media"
            media_dir.mkdir()
            conn = sqlite3.connect(":memory:", isolation_level=None)
            self.addCleanup(conn.close)
            conn.execute("PRAGMA foreign_keys=ON")
            init_db(conn)
            message = FakeMessage(media=object(), mime_type="application/pdf", file_size=20)

            paths = await persist_message(
                conn, message, "ignored", media_dir, 1, 0.0, media_policy=media_policy()
            )
            self.assertEqual(paths, [])
            event = conn.execute(
                "SELECT status, reason FROM message_processing_events WHERE message_id=1"
            ).fetchone()
            self.assertEqual(event[0], "rejected_media")
            self.assertIn("application/pdf", event[1])

            orphan = media_dir / "orphan.bin"
            orphan.write_bytes(b"orphan")
            result = cleanup_media_storage(conn, media_dir)
            self.assertFalse(orphan.exists())
            self.assertEqual(result["removed_files"], 1)

    async def test_deletion_removes_unexecuted_data_but_retains_order_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            media_dir = Path(tmp) / "media"
            conn = sqlite3.connect(":memory:", isolation_level=None)
            self.addCleanup(conn.close)
            conn.execute("PRAGMA foreign_keys=ON")
            init_db(conn)

            unexecuted = FakeMessage(1, media=object(), mime_type="image/png", file_size=len(PNG_BYTES))
            paths = await persist_message(
                conn, unexecuted, "ignored", media_dir, 1, 0.0, media_policy=media_policy()
            )
            self.assertEqual(delete_message_and_media(conn, -100123, 1, media_dir, 1, 0.0), "deleted")
            self.assertFalse(paths[0].exists())
            self.assertIsNone(conn.execute("SELECT 1 FROM messages WHERE message_id=1").fetchone())

            executed = FakeMessage(2, media=object(), mime_type="image/png", file_size=len(PNG_BYTES))
            paths = await persist_message(
                conn, executed, "ignored", media_dir, 1, 0.0, media_policy=media_policy()
            )
            conn.execute(
                """
                INSERT INTO positions_submitted
                (chat_id,message_id,symbol,side,quantity,status,created_at_utc,updated_at_utc)
                VALUES (-100123,2,'BTC/USDT','buy',1,'submitted','now','now')
                """
            )
            result = delete_message_and_media(conn, -100123, 2, media_dir, 1, 0.0)
            self.assertEqual(result, "retained_order_audit")
            self.assertFalse(paths[0].exists())
            self.assertIsNotNone(conn.execute("SELECT 1 FROM messages WHERE message_id=2").fetchone())
            status = conn.execute(
                "SELECT status FROM message_processing WHERE message_id=2"
            ).fetchone()[0]
            self.assertEqual(status, "source_deleted_order_retained")


class OpenAIMediaTests(unittest.TestCase):
    def test_all_images_are_included_with_detected_mime(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = [Path(tmp) / "one.png", Path(tmp) / "two.png"]
            for path in paths:
                path.write_bytes(PNG_BYTES)
            extractor = OpenAIExtractor(config_for(Path(tmp)))
            captured = {}

            def fake_request(payload):
                captured.update(payload)
                return {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "token": None,
                                        "position_type": None,
                                        "entry_price": None,
                                        "leverage": None,
                                        "stop_losses": [],
                                        "take_profits": [],
                                    }
                                )
                            }
                        }
                    ]
                }

            extractor._request = fake_request
            extractor.extract_signal("caption", paths)
            content = captured["messages"][1]["content"]
            images = [part for part in content if part["type"] == "image_url"]
            self.assertEqual(len(images), 2)
            self.assertTrue(all(part["image_url"]["url"].startswith("data:image/png") for part in images))

    def test_image_file_pixel_limit_is_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "image.png"
            path.write_bytes(PNG_BYTES)
            with self.assertRaisesRegex(ValueError, "pixel limit"):
                inspect_image_file(path, max_bytes=1024, max_pixels=5)


if __name__ == "__main__":
    unittest.main()
