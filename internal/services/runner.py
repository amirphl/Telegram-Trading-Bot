import asyncio
import logging
import random
import sys
from collections.abc import Iterable

from telethon.errors import (
    AuthKeyUnregisteredError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    rpcerrorlist,
)

from api.telegram.client import build_client
from api.telegram.utils import canonical_peer_id, resolve_channel
from configs.config import Config
from internal.db.sqlite import attach_runtime, close_db, connect_db, init_db
from internal.repositories.extraction_jobs import recover_interrupted_jobs
from internal.repositories.messages import cleanup_media_storage
from internal.services.backfill import BackfillStopped, backfill_recent
from internal.services.blocking import BlockingWorkPool, run_blocking, run_db
from internal.services.exchange_lbank import (
    check_lbank_authentication,
    close_lbank_clients,
)
from internal.services.executor import (
    reconcile_pending_positions,
    reconcile_protective_orders,
)
from internal.services.heartbeat import heartbeat_task
from internal.services.signal_extraction import extraction_worker_task
from internal.types.context import BotContext, ChannelConfig, SignalDiscoveryPolicy

logger = logging.getLogger(__name__)

TERMINAL_AUTH_ERRORS = (
    AuthKeyUnregisteredError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
)


class AuthenticationRequired(RuntimeError):
    pass


async def _resolve_and_setup_channels(client, cfg: Config) -> dict[int, ChannelConfig]:
    """Resolve configured channels into the marked peer IDs used by events."""
    channels: dict[int, ChannelConfig] = {}

    for policy_config in cfg.channels:
        try:
            resolution_config = type(
                "ChannelResolutionConfig",
                (),
                {
                    "channel_id": policy_config.channel_id,
                    "channel_title": policy_config.channel_title,
                },
            )()
            entity = await resolve_channel(client, resolution_config)
            policy = SignalDiscoveryPolicy(policy_config.policy)
            channel_config = ChannelConfig(
                channel_id=policy_config.channel_id,
                channel_title=(
                    getattr(entity, "title", policy_config.channel_title)
                    or policy_config.channel_title
                ),
                policy=policy,
                window_size=policy_config.window_size,
                enabled=policy_config.enabled,
                prompt=getattr(policy_config, "prompt", None),
            )
            peer_id = canonical_peer_id(entity)
            channels[peer_id] = channel_config
            logger.info(
                "Configured channel '%s' (peer_id=%s, policy=%s, window_size=%d)",
                channel_config.channel_title,
                peer_id,
                policy.value,
                channel_config.window_size,
            )
        except Exception as exc:
            logger.error(
                "Failed to resolve channel '%s' (%s): %s",
                policy_config.channel_title,
                policy_config.channel_id,
                exc,
            )

    if not channels:
        raise ValueError("No channels could be resolved successfully")
    return channels


async def _backfill_all_channels(
    client,
    channels: dict[int, ChannelConfig],
    ctx: BotContext,
    backfill_count: int,
) -> None:
    """Resume durable backfill for every enabled configured channel."""
    for chat_id, channel_config in channels.items():
        if not channel_config.enabled:
            continue
        try:
            entity = await client.get_entity(chat_id)
            result = await backfill_recent(client, entity, ctx, backfill_count)
            if result.completed:
                logger.info(
                    "Backfilled channel '%s' through message %d (%d processed)",
                    channel_config.channel_title,
                    result.last_message_id,
                    result.processed,
                )
            else:
                logger.warning(
                    "Backfill for channel '%s' remains incomplete; live monitoring "
                    "will continue and backfill will resume on reconnect",
                    channel_config.channel_title,
                )
        except (asyncio.CancelledError, BackfillStopped):
            raise
        except Exception:
            logger.exception(
                "Failed to backfill channel '%s'", channel_config.channel_title
            )


async def cancel_and_await(tasks: Iterable[asyncio.Future | None]) -> None:
    owned = [task for task in tasks if task is not None]
    for task in owned:
        if not task.done():
            task.cancel()
    if owned:
        await asyncio.gather(*owned, return_exceptions=True)


async def monitor_connection(client, ctx: BotContext) -> None:
    """Supervise connection health and guarantee all child tasks are reaped."""
    disconnected = None
    heartbeat = None
    extraction_worker = None
    try:
        disconnected = asyncio.ensure_future(client.run_until_disconnected())
        heartbeat = asyncio.create_task(
            heartbeat_task(
                client,
                ctx.cfg.heartbeat_secs,
                int(getattr(ctx.cfg, "heartbeat_failure_threshold", 3)),
            ),
            name="telegram-heartbeat",
        )
        watched = {disconnected, heartbeat}
        if ctx.cfg.signal_extraction_enabled:
            extraction_worker = asyncio.create_task(
                extraction_worker_task(ctx),
                name="signal-extraction-worker",
            )
            watched.add(extraction_worker)
        done, _ = await asyncio.wait(watched, return_when=asyncio.FIRST_COMPLETED)
        if heartbeat in done:
            await heartbeat
        if extraction_worker is not None and extraction_worker in done:
            await extraction_worker
            raise RuntimeError("signal extraction worker stopped unexpectedly")
        await disconnected
        raise ConnectionError("Telegram disconnected")
    finally:
        await cancel_and_await((disconnected, heartbeat, extraction_worker))


def _auth_retry_error(exc: BaseException) -> bool:
    # AuthRestartError is transient and is absent from some Telethon releases.
    return exc.__class__.__name__ == "AuthRestartError"


def _backoff(cfg: Config, attempts: int) -> float:
    return min(
        cfg.max_backoff_secs,
        (2 ** min(attempts, 6)) + random.uniform(0, 1),
    )


async def _disconnect(client) -> None:
    try:
        await client.disconnect()
    except asyncio.CancelledError:
        task = asyncio.create_task(client.disconnect())
        await asyncio.gather(task, return_exceptions=True)
        raise
    except Exception as exc:
        print(f"[!] Telegram disconnect cleanup failed: {exc.__class__.__name__}: {exc}")


async def run_forever(cfg: Config) -> None:
    """Run Telegram monitoring with multi-channel support and bounded recovery."""
    pool = BlockingWorkPool(
        max_workers=int(getattr(cfg, "blocking_workers", 4)),
        queue_limit=int(getattr(cfg, "blocking_queue_limit", 16)),
        submit_timeout_secs=float(getattr(cfg, "blocking_submit_timeout_secs", 5)),
        operation_timeout_secs=float(
            getattr(cfg, "blocking_operation_timeout_secs", 60)
        ),
    )
    db_conn = None
    client = None
    try:
        db_conn = connect_db(cfg.db_path)
        attach_runtime(db_conn, pool)
        await run_db(db_conn, init_db, db_conn)
        extraction_recovery = await run_db(
            db_conn, recover_interrupted_jobs, db_conn
        )
        if extraction_recovery["requeued"] or extraction_recovery["failed"]:
            print(f"[*] Extraction recovery: {extraction_recovery}")
        cleanup = await run_db(
            db_conn,
            cleanup_media_storage,
            db_conn,
            cfg.media_dir,
            cfg.media_retention_days,
        )
        if (
            cleanup["removed_files"]
            or cleanup["removed_rows"]
            or cleanup.get("repaired_messages")
        ):
            print(f"[*] Media cleanup: {cleanup}")

        if cfg.enable_auto_execution:
            print(
                "[!] Automatic execution ENABLED: "
                f"mode={cfg.trading_mode} market={cfg.execution_market_type} "
                f"notional={cfg.order_notional} {cfg.order_quote}"
            )
        else:
            print("[*] Automatic execution DISABLED")
        if cfg.signal_extraction_enabled:
            print(
                "[*] Signal extraction ENABLED: "
                f"model={cfg.openai_model} approval={cfg.signal_approval_mode}"
            )
        else:
            print(
                "[*] Signal extraction DISABLED "
                "(set SIGNAL_EXTRACTION_ENABLED=true to enable)"
            )

        if cfg.lbank_api_key and cfg.lbank_secret:
            try:
                exchange_client = await run_blocking(
                    pool,
                    check_lbank_authentication,
                    cfg,
                    timeout_secs=float(
                        getattr(cfg, "blocking_operation_timeout_secs", 60)
                    ),
                )
                print("[*] LBank authenticated read-only check passed")
                pending = await run_db(
                    db_conn,
                    reconcile_pending_positions,
                    cfg,
                    db_conn,
                    client=exchange_client,
                )
                protective = await run_db(
                    db_conn,
                    reconcile_protective_orders,
                    cfg,
                    db_conn,
                    client=exchange_client,
                )
                if pending["checked"] or protective["checked"]:
                    print(
                        f"[*] Order reconciliation: pending={pending} "
                        f"protective={protective}"
                    )
            except Exception as exc:
                print(f"[!] Order reconciliation failed safely: {exc}")

        client = build_client(cfg)
        ctx = BotContext(db_conn=db_conn, cfg=cfg)
        from api.telegram.handlers import register_handlers

        register_handlers(client, ctx)

        attempts = 0
        auth_attempts = 0
        interactive_authorization_used = False
        while True:
            try:
                print("[*] Connecting (or re-connecting)…")
                await client.connect()

                if not await client.is_user_authorized():
                    if interactive_authorization_used:
                        raise AuthenticationRequired(
                            "Telegram remains unauthorized after one interactive login "
                            "attempt. Remove or repair the session and restart in an "
                            "interactive terminal."
                        )
                    interactive_authorization_used = True
                    print("[*] Authorizing once… (enter your phone/code/2FA)")
                    await client.start()
                    if not await client.is_user_authorized():
                        raise AuthenticationRequired(
                            "Telegram authorization did not complete. Repair "
                            "credentials/session and restart interactively."
                        )

                configured_channels = getattr(cfg, "channels", None)
                if configured_channels:
                    channels = await _resolve_and_setup_channels(client, cfg)
                    ctx.channels = channels
                    ctx.target_id = next(iter(channels))
                    ctx.channel_title_for_path = f"channel_{abs(ctx.target_id)}"
                    logger.info("Successfully configured %d channels", len(channels))
                    await _backfill_all_channels(
                        client, channels, ctx, cfg.backfill
                    )
                else:
                    # Compatibility for callers using the original single-channel
                    # configuration shape.
                    entity = await resolve_channel(client, cfg)
                    ctx.target_id = canonical_peer_id(entity)
                    ctx.channel_title_for_path = f"channel_{abs(ctx.target_id)}"
                    print(
                        f"[*] Watching channel: {getattr(entity, 'title', 'Unknown')} "
                        f"(id={entity.id})"
                    )
                    backfill_result = await backfill_recent(
                        client, entity, ctx, cfg.backfill
                    )
                    if not backfill_result.completed:
                        print(
                            "[!] Live monitoring is continuing; durable backfill "
                            "will resume on reconnect."
                        )
                print(f"[*] DB: {cfg.db_path} | Media: {cfg.media_dir}")

                if attempts:
                    print(
                        f"[*] Connection healthy; reconnect backoff reset from "
                        f"{attempts}."
                    )
                attempts = 0
                auth_attempts = 0

                recovery = await run_db(db_conn, recover_interrupted_jobs, db_conn)
                if recovery["requeued"] or recovery["failed"]:
                    print(f"[*] Extraction reconnect recovery: {recovery}")
                await monitor_connection(client, ctx)

            except AuthenticationRequired as exc:
                print(f"[!] Authentication stopped: {exc}")
                return
            except BackfillStopped as exc:
                print(f"[!] Live monitoring stopped by backfill policy: {exc}")
                return
            except TERMINAL_AUTH_ERRORS as exc:
                print(
                    f"[!] Authentication stopped ({exc.__class__.__name__}). "
                    "Repair Telegram credentials/session and restart interactively; "
                    "automatic login retries are disabled."
                )
                return
            except rpcerrorlist.FloodWaitError as exc:
                wait = min(
                    cfg.max_backoff_secs,
                    int(getattr(exc, "seconds", 60)) + 1,
                )
                print(f"[!] FloodWait: sleeping {wait}s before reconnect…")
                await asyncio.sleep(wait)
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if _auth_retry_error(exc):
                    auth_attempts += 1
                    maximum = int(getattr(cfg, "auth_retry_max_attempts", 3))
                    if auth_attempts >= maximum:
                        print(
                            f"[!] Retryable authentication failed {auth_attempts} "
                            "times; stopping. Restart interactively after checking "
                            "Telegram service/session."
                        )
                        return
                    delay = _backoff(cfg, auth_attempts)
                    print(
                        "[!] Transient authentication error "
                        f"({auth_attempts}/{maximum}); retrying in {delay:.1f}s "
                        "without another interactive prompt."
                    )
                    await asyncio.sleep(delay)
                    continue

                attempts += 1
                delay = _backoff(cfg, attempts)
                print(
                    f"[!] Connection error: {exc.__class__.__name__}: {exc}. "
                    f"Reconnecting in {delay:.1f}s…"
                )
                await asyncio.sleep(delay)
            finally:
                await _disconnect(client)
    finally:
        if client is not None:
            await _disconnect(client)
        try:
            try:
                await run_blocking(
                    pool,
                    close_lbank_clients,
                    timeout_secs=float(
                        getattr(cfg, "blocking_operation_timeout_secs", 60)
                    ),
                )
            except Exception as exc:
                print(f"[!] Exchange resource cleanup failed: {exc}")
            if db_conn is not None:
                await run_db(db_conn, close_db, db_conn)
        finally:
            await pool.close()
            sys.stdout.flush()
            sys.stderr.flush()
