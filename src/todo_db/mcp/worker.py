"""The single dedicated worker thread that owns all database and git work.

FastMCP does not serialise tool calls, ``sqlite3`` connections are thread-affine
(``check_same_thread=True``), and ``GitScopeEngine`` runs blocking subprocesses
and reads every untracked file's bytes (``workspace_fingerprint``). ADR 0006 G4:
every ``TodoDatabase`` and ``GitScopeEngine`` interaction MUST run on one
dedicated worker thread, reached only through :func:`run_in_worker`.

Tools (a later migration item) are ``async def`` and ``await run_in_worker(...)``.
Never touch a ``sqlite3`` object, a ``TodoDatabase``, or a ``GitScopeEngine``
from the event loop or from any other thread.
"""

from __future__ import annotations

import asyncio
import functools
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, TypeVar

_T = TypeVar("_T")

# A 1-worker pool *is* the intra-process mutation guard (every DB/git call is
# serialised through this single thread). WORKER_LOCK is kept as an explicit,
# documented handle for code that wants to assert/hold the guard directly.
_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()
WORKER_LOCK = threading.RLock()


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="todo-db-mcp-worker"
            )
        return _executor


def _run_guarded(fn: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
    with WORKER_LOCK:
        return fn(*args, **kwargs)


async def run_in_worker(fn: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
    """Await ``fn(*args, **kwargs)`` executed on the dedicated worker thread."""

    loop = asyncio.get_running_loop()
    call = functools.partial(_run_guarded, fn, *args, **kwargs)
    return await loop.run_in_executor(_get_executor(), call)


def run_in_worker_sync(fn: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
    """Blocking variant for startup paths that run before the event loop."""

    future = _get_executor().submit(_run_guarded, fn, *args, **kwargs)
    return future.result()


def shutdown_worker(wait: bool = True) -> None:
    """Tear the worker down (test hygiene / clean process exit)."""

    global _executor
    with _executor_lock:
        if _executor is not None:
            _executor.shutdown(wait=wait)
            _executor = None
