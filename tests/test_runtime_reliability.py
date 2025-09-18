import asyncio
import sqlite3
import sys
import tempfile
import threading
import time
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

# Other test modules install intentionally small dependency stubs. Extend those
# stubs before importing the runner so this file also runs independently.
errors = sys.modules.get("telethon.errors")
if errors is not None:
    for name in (
        "AuthKeyUnregisteredError",
        "PhoneCodeExpiredError",
        "PhoneCodeInvalidError",
    ):
        if not hasattr(errors, name):
            setattr(errors, name, type(name, (Exception,), {}))
    if not hasattr(errors, "rpcerrorlist"):
        errors.rpcerrorlist = SimpleNamespace(FloodWaitError=errors.FloodWaitError)
    if "telethon.network" not in sys.modules:
        client_stub = types.ModuleType("api.telegram.client")
        client_stub.build_client = lambda cfg: None
        sys.modules["api.telegram.client"] = client_stub


from internal.db.sqlite import init_db, sql_execute_with_retry
from internal.repositories.checkpoints import (
    advance_live_checkpoint,
    get_channel_checkpoint,
    save_channel_checkpoint,
)
from internal.services.backfill import BackfillResult, BackfillStopped, backfill_recent
from internal.services.blocking import (
    BlockingWorkPool,
    BlockingWorkSaturated,
    BlockingWorkTimeout,
)
from internal.services.heartbeat import HeartbeatFailure, heartbeat_task


class FakeMessage:
    def __init__(self, message_id: int, chat_id: int = -100) -> None:
        self.id = message_id
        self.chat_id = chat_id
        self.message = f"message {message_id}"
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
        self.media = None
        self.file = None
        self.photo = None
        self.document = None

    def to_dict(self):
        return {"id": self.id, "chat_id": self.chat_id, "message": self.message}


def runtime_cfg(media_dir: Path, **overrides):
    values = {
        "media_dir": media_dir,
        "sql_busy_retries": 1,
        "sql_busy_sleep": 0.0,
        "historical_signal_policy": "store_only",
        "extraction_max_attempts": 2,
        "max_backoff_secs": 0,
        "backfill_max_attempts": 2,
        "backfill_retry_base_secs": 0,
        "backfill_failure_policy": "continue_live",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class BlockingIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_pool_bounds_queue_and_times_out_operations(self):
        pool = BlockingWorkPool(
            max_workers=1,
            queue_limit=0,
            submit_timeout_secs=0.02,
            operation_timeout_secs=0.02,
        )
        gate = threading.Event()
        occupied = asyncio.create_task(pool.run(gate.wait, timeout_secs=1))
        await asyncio.sleep(0.01)
        with self.assertRaises(BlockingWorkSaturated):
            await pool.run(lambda: None)
        gate.set()
        await occupied
        with self.assertRaises(BlockingWorkTimeout):
            await pool.run(time.sleep, 0.05)
        await pool.close()

    async def test_sqlite_retry_never_sleeps_on_event_loop(self):
        class LockedConnection:
            def execute(self, sql, params):
                raise sqlite3.OperationalError("database is locked")

        started = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "use run_db"):
            sql_execute_with_retry(
                LockedConnection(),
                "UPDATE example SET value=1",
                busy_retries=5,
                busy_sleep_secs=1,
            )
        self.assertLess(time.monotonic() - started, 0.1)


class BackfillRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = sqlite3.connect(":memory:", isolation_level=None)
        self.addCleanup(self.conn.close)
        self.conn.execute("PRAGMA foreign_keys=ON")
        init_db(self.conn)
        self.ctx = SimpleNamespace(
            db_conn=self.conn,
            target_id=-100,
            channel_title_for_path="channel",
            cfg=runtime_cfg(Path(self.tmp.name)),
            ingestion_lock=asyncio.Lock(),
        )

    async def test_checkpoint_pages_every_message_after_an_outage(self):
        class Client:
            def __init__(self, messages):
                self.messages = messages
                self.min_ids = []

            async def iter_messages(self, entity, limit, min_id=0, reverse=False):
                self.min_ids.append(min_id)
                selected = [msg for msg in self.messages if msg.id > min_id]
                selected.sort(key=lambda msg: msg.id, reverse=not reverse)
                for msg in selected[:limit]:
                    yield msg

        seed = Client([FakeMessage(3), FakeMessage(2)])
        first = await backfill_recent(seed, object(), self.ctx, 2)
        self.assertTrue(first.completed)
        self.assertEqual(get_channel_checkpoint(self.conn, -100)["last_message_id"], 3)

        recovery = Client([FakeMessage(4), FakeMessage(5), FakeMessage(6)])
        second = await backfill_recent(recovery, object(), self.ctx, 2)
        self.assertEqual(second.processed, 3)
        self.assertEqual(recovery.min_ids, [3, 5])
        self.assertEqual(get_channel_checkpoint(self.conn, -100)["last_message_id"], 6)
        ids = [row[0] for row in self.conn.execute(
            "SELECT message_id FROM messages ORDER BY message_id"
        )]
        self.assertEqual(ids, [2, 3, 4, 5, 6])

    async def test_retry_exhaustion_is_durable_and_policy_is_explicit(self):
        class BrokenClient:
            async def iter_messages(self, entity, limit, **kwargs):
                raise ConnectionError("unavailable")
                yield  # pragma: no cover

        save_channel_checkpoint(self.conn, -100, 3)
        result = await backfill_recent(BrokenClient(), object(), self.ctx, 2)
        self.assertFalse(result.completed)
        self.assertTrue(result.continued_live)
        checkpoint = get_channel_checkpoint(self.conn, -100)
        self.assertEqual(checkpoint["status"], "retry_exhausted")
        self.assertFalse(advance_live_checkpoint(self.conn, -100, 99))
        self.assertEqual(get_channel_checkpoint(self.conn, -100)["last_message_id"], 3)

        self.ctx.cfg.backfill_failure_policy = "stop"
        with self.assertRaises(BackfillStopped):
            await backfill_recent(BrokenClient(), object(), self.ctx, 2)


class HeartbeatAndOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_transient_heartbeat_recovers_but_threshold_notifies_runner(self):
        outcomes = [ConnectionError("one"), None, ConnectionError("two"), ConnectionError("three")]

        class Client:
            async def get_me(self):
                outcome = outcomes.pop(0)
                if outcome:
                    raise outcome

        with patch("internal.services.heartbeat.asyncio.sleep", AsyncMock()):
            with self.assertRaises(HeartbeatFailure):
                await heartbeat_task(Client(), 60, failure_threshold=2)
        self.assertEqual(outcomes, [])

    async def test_monitor_reaps_every_child_on_cancellation(self):
        # Import after test dependency stubs have been completed above.
        from internal.services.runner import monitor_connection

        stopped = {"connection": False, "heartbeat": False, "worker": False}
        started = {name: asyncio.Event() for name in stopped}

        class Client:
            async def run_until_disconnected(self):
                started["connection"].set()
                try:
                    await asyncio.Event().wait()
                finally:
                    stopped["connection"] = True

        async def forever(name):
            started[name].set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped[name] = True

        ctx = SimpleNamespace(
            cfg=SimpleNamespace(
                heartbeat_secs=1,
                heartbeat_failure_threshold=3,
                signal_extraction_enabled=True,
            )
        )
        with patch(
            "internal.services.runner.heartbeat_task",
            new=lambda *args: forever("heartbeat"),
        ), patch(
            "internal.services.runner.extraction_worker_task",
            new=lambda *args: forever("worker"),
        ):
            task = asyncio.create_task(monitor_connection(Client(), ctx))
            await asyncio.gather(*(event.wait() for event in started.values()))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(stopped, {"connection": True, "heartbeat": True, "worker": True})

    async def test_monitor_reaps_tasks_on_disconnect_and_heartbeat_error(self):
        from internal.services.runner import monitor_connection

        async def heartbeat_forever(*args):
            await asyncio.Event().wait()

        disconnected_client = SimpleNamespace(
            run_until_disconnected=AsyncMock(return_value=None)
        )
        ctx = SimpleNamespace(
            cfg=SimpleNamespace(
                heartbeat_secs=1,
                heartbeat_failure_threshold=2,
                signal_extraction_enabled=False,
            )
        )
        with patch(
            "internal.services.runner.heartbeat_task",
            new=heartbeat_forever,
        ), self.assertRaisesRegex(ConnectionError, "disconnected"):
            await monitor_connection(disconnected_client, ctx)
        self.assertFalse(any(
            task.get_name() == "telegram-heartbeat" and not task.done()
            for task in asyncio.all_tasks()
        ))

        connection_stopped = asyncio.Event()

        class ConnectedClient:
            async def run_until_disconnected(self):
                try:
                    await asyncio.Event().wait()
                finally:
                    connection_stopped.set()

        async def failed_heartbeat(*args):
            raise HeartbeatFailure("unhealthy")

        with patch(
            "internal.services.runner.heartbeat_task",
            new=failed_heartbeat,
        ), self.assertRaises(HeartbeatFailure):
            await monitor_connection(ConnectedClient(), ctx)
        self.assertTrue(connection_stopped.is_set())


class RunnerRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def _config(self, root: Path):
        return SimpleNamespace(
            db_path=str(root / "runtime.db"),
            media_dir=root / "media",
            media_retention_days=0,
            blocking_workers=2,
            blocking_queue_limit=2,
            blocking_submit_timeout_secs=1,
            blocking_operation_timeout_secs=1,
            enable_auto_execution=False,
            trading_mode="sandbox",
            execution_market_type="spot",
            order_notional=10,
            order_quote="USDT",
            signal_extraction_enabled=False,
            openai_model="test",
            signal_approval_mode="manual",
            lbank_api_key=None,
            lbank_secret=None,
            backfill=2,
            max_backoff_secs=1,
            heartbeat_secs=1,
            heartbeat_failure_threshold=2,
            auth_retry_max_attempts=2,
        )

    async def test_healthy_milestone_resets_backoff_and_shutdown_closes_db(self):
        from internal.services import runner

        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config(Path(tmp))
            closed = []
            original_close = runner.close_db

            class Client:
                def __init__(self):
                    self.connects = 0
                    self.disconnects = 0

                async def connect(self):
                    self.connects += 1
                    if self.connects in {1, 2}:
                        raise ConnectionError("connect failed")
                    if self.connects == 4:
                        raise asyncio.CancelledError()

                async def disconnect(self):
                    self.disconnects += 1

                async def is_user_authorized(self):
                    return True

            client = Client()
            attempts = []

            def tracked_close(conn):
                closed.append(True)
                original_close(conn)

            with patch.object(runner, "build_client", return_value=client):
                with patch("api.telegram.handlers.register_handlers"):
                    with patch.object(
                        runner,
                        "resolve_channel",
                        AsyncMock(return_value=SimpleNamespace(id=100, title="channel")),
                    ):
                        with patch.object(
                            runner,
                            "backfill_recent",
                            AsyncMock(return_value=BackfillResult(True, 0, 1, 1)),
                        ):
                            with patch.object(
                                runner,
                                "monitor_connection",
                                AsyncMock(side_effect=ConnectionError("disconnected")),
                            ):
                                with patch.object(runner.asyncio, "sleep", AsyncMock()):
                                    with patch.object(
                                        runner,
                                        "_backoff",
                                        side_effect=lambda cfg, value: attempts.append(value) or 0,
                                    ):
                                        with patch.object(runner, "close_db", side_effect=tracked_close):
                                            with self.assertRaises(asyncio.CancelledError):
                                                await runner.run_forever(cfg)

            self.assertEqual(attempts, [1, 2, 1])
            self.assertTrue(closed)
            self.assertGreaterEqual(client.disconnects, 4)

    async def test_terminal_authentication_stops_without_retry_loop(self):
        from internal.services import runner

        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config(Path(tmp))

            class Client:
                async def connect(self):
                    return None

                async def disconnect(self):
                    return None

                async def is_user_authorized(self):
                    raise runner.AuthKeyUnregisteredError("expired")

                async def start(self):
                    raise AssertionError("interactive login must not repeat")

            sleep = AsyncMock()
            with patch.object(runner, "build_client", return_value=Client()):
                with patch("api.telegram.handlers.register_handlers"):
                    with patch.object(runner.asyncio, "sleep", sleep):
                        await runner.run_forever(cfg)
            sleep.assert_not_awaited()

    async def test_retryable_authentication_has_bounded_backoff_without_prompt(self):
        from internal.services import runner

        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config(Path(tmp))

            class AuthRestartError(Exception):
                pass

            class Client:
                def __init__(self):
                    self.connects = 0
                    self.starts = 0

                async def connect(self):
                    self.connects += 1
                    raise AuthRestartError("retry")

                async def disconnect(self):
                    return None

                async def start(self):
                    self.starts += 1

            client = Client()
            sleep = AsyncMock()
            with patch.object(runner, "build_client", return_value=client):
                with patch("api.telegram.handlers.register_handlers"):
                    with patch.object(runner.asyncio, "sleep", sleep):
                        await runner.run_forever(cfg)
            self.assertEqual(client.connects, 2)
            self.assertEqual(client.starts, 0)
            self.assertEqual(sleep.await_count, 1)


if __name__ == "__main__":
    unittest.main()
