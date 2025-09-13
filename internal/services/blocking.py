"""Bounded isolation for synchronous database and network clients."""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar

T = TypeVar("T")


class BlockingWorkSaturated(RuntimeError):
    """Raised when the bounded worker queue cannot accept work in time."""


class BlockingWorkTimeout(TimeoutError):
    """Raised when blocking work exceeds its configured deadline."""


class BlockingWorkPool:
    def __init__(
        self,
        *,
        max_workers: int = 4,
        queue_limit: int = 16,
        submit_timeout_secs: float = 5.0,
        operation_timeout_secs: float = 60.0,
    ) -> None:
        if max_workers <= 0 or queue_limit < 0:
            raise ValueError("worker count must be positive and queue limit cannot be negative")
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="bot-blocking",
        )
        self._capacity = asyncio.Semaphore(max_workers + queue_limit)
        self._submit_timeout = submit_timeout_secs
        self._operation_timeout = operation_timeout_secs
        self._draining: set[asyncio.Task[None]] = set()
        self._closed = False

    async def _release_when_done(self, future: asyncio.Future[Any]) -> None:
        try:
            await future
        except BaseException:
            pass
        finally:
            self._capacity.release()

    def _defer_release(self, future: asyncio.Future[Any]) -> None:
        task = asyncio.create_task(self._release_when_done(future))
        self._draining.add(task)
        task.add_done_callback(self._draining.discard)

    async def run(
        self,
        func: Callable[..., T],
        *args: Any,
        timeout_secs: float | None = None,
        **kwargs: Any,
    ) -> T:
        if self._closed:
            raise RuntimeError("blocking worker pool is closed")
        try:
            await asyncio.wait_for(
                self._capacity.acquire(),
                timeout=max(0.0, self._submit_timeout),
            )
        except asyncio.TimeoutError as exc:
            raise BlockingWorkSaturated(
                "blocking worker capacity was not available before the submit deadline"
            ) from exc

        if self._closed:
            self._capacity.release()
            raise RuntimeError("blocking worker pool is closed")
        loop = asyncio.get_running_loop()
        call = functools.partial(func, *args, **kwargs)
        try:
            submitted = self._executor.submit(call)
        except BaseException:
            self._capacity.release()
            raise
        future = asyncio.wrap_future(submitted, loop=loop)
        release_now = True
        deadline = self._operation_timeout if timeout_secs is None else timeout_secs
        try:
            if deadline is None or deadline <= 0:
                return await asyncio.shield(future)
            try:
                return await asyncio.wait_for(asyncio.shield(future), timeout=deadline)
            except asyncio.TimeoutError as exc:
                release_now = False
                self._defer_release(future)
                raise BlockingWorkTimeout(
                    f"blocking operation exceeded its {deadline:g}s deadline"
                ) from exc
        except asyncio.CancelledError:
            if not future.done():
                release_now = False
                self._defer_release(future)
            raise
        finally:
            if release_now:
                self._capacity.release()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._draining:
            await asyncio.gather(*tuple(self._draining), return_exceptions=True)
        loop = asyncio.get_running_loop()
        shutdown: Callable[[], None] = functools.partial(
            self._executor.shutdown,
            wait=True,
            cancel_futures=True,
        )
        await loop.run_in_executor(None, shutdown)


async def run_blocking(
    pool: BlockingWorkPool | None,
    func: Callable[..., T],
    *args: Any,
    timeout_secs: float | None = None,
    **kwargs: Any,
) -> T:
    if pool is not None:
        return await pool.run(func, *args, timeout_secs=timeout_secs, **kwargs)
    call = functools.partial(func, *args, **kwargs)
    if timeout_secs is None or timeout_secs <= 0:
        return await asyncio.to_thread(call)
    return await asyncio.wait_for(asyncio.to_thread(call), timeout=timeout_secs)


async def run_db(connection: Any, func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run a complete synchronous DB operation without occupying the event loop."""
    pool = getattr(connection, "worker_pool", None)
    lock = getattr(connection, "async_lock", None)
    if lock is None:
        # Compatibility for caller-owned sqlite3 connections, which normally
        # enforce same-thread use. Production connections created by connect_db
        # always carry a worker pool and async lock.
        if pool is None:
            return func(*args, **kwargs)
        return await run_blocking(pool, func, *args, timeout_secs=0, **kwargs)
    async with lock:
        # A cancelled coroutine must not release the connection lock while its
        # thread is still using SQLite. Network calls in DB transactions have
        # their own client timeout, so waiting here remains bounded in practice.
        task = asyncio.create_task(
            run_blocking(pool, func, *args, timeout_secs=0, **kwargs)
        )
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await task
            except BaseException:
                pass
            raise
