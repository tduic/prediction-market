"""
Supervision for long-running asyncio background tasks.

``asyncio.create_task`` swallows failures until someone awaits the task. The
trading session used to await its background tasks only at shutdown, so a
crashed websocket stream, scheduler or refresh loop died silently and the bot
kept running blind. ``supervised_task`` wraps a coroutine *factory* so that an
unexpected exit is logged, raises a CRITICAL alert and (for long-running loops)
is restarted with exponential backoff until the session's stop event is set.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import time
from collections.abc import Awaitable, Callable
from typing import Any

from core.alerting import Severity, get_alert_manager

logger = logging.getLogger(__name__)

# A run that lasted at least this long is considered healthy, so the backoff
# resets to the initial delay instead of continuing to grow.
_HEALTHY_RUN_S = 300.0


def _alert(name: str, message: str) -> None:
    try:
        get_alert_manager().send_nowait(
            title=f"Background task exited: {name}",
            message=message,
            severity=Severity.CRITICAL,
            component="trading_session",
        )
    except Exception:  # noqa: BLE001 — alerting must never break supervision
        logger.exception("failed to send supervision alert for %s", name)


async def supervise(
    name: str,
    factory: Callable[[], Awaitable[Any]],
    stop_event: asyncio.Event,
    *,
    restart: bool = True,
    initial_backoff_s: float = 1.0,
    max_backoff_s: float = 60.0,
) -> None:
    """Run ``factory()`` and handle unexpected exits.

    - Cancellation propagates (normal shutdown path).
    - If ``stop_event`` is set when the coroutine finishes, it's a clean exit.
    - Otherwise an exception — or, for ``restart=True`` loops, any return —
      is unexpected: log it, send a CRITICAL alert and, when ``restart`` is
      true, start a fresh coroutine after an exponential backoff.
    - With ``restart=False`` (one-shot jobs) a normal return is expected and
      an exception is logged/alerted once.
    """
    backoff = initial_backoff_s
    while True:
        started = time.monotonic()
        try:
            await factory()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — any crash must be surfaced
            if stop_event.is_set():
                logger.info("task %s raised during shutdown: %r", name, exc)
                return
            logger.exception("background task %s crashed", name)
            reason = f"crashed with {type(exc).__name__}"
        else:
            if stop_event.is_set() or not restart:
                return
            logger.error("background task %s returned unexpectedly", name)
            reason = "returned unexpectedly"

        if not restart:
            _alert(name, f"{name} {reason}; not restarted (one-shot task)")
            return

        if time.monotonic() - started >= _HEALTHY_RUN_S:
            backoff = initial_backoff_s
        _alert(name, f"{name} {reason}; restarting in {backoff:.0f}s")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=backoff)
            return  # stop requested while backing off
        except TimeoutError:
            pass
        backoff = min(backoff * 2, max_backoff_s)
        logger.warning("restarting background task %s", name)


def supervised_task(
    factory: Callable[[], Awaitable[Any]],
    *,
    name: str,
    stop_event: asyncio.Event,
    restart: bool = True,
    initial_backoff_s: float = 1.0,
    max_backoff_s: float = 60.0,
) -> asyncio.Task[None]:
    """``asyncio.create_task`` with :func:`supervise` around the coroutine."""
    return asyncio.create_task(
        supervise(
            name,
            factory,
            stop_event,
            restart=restart,
            initial_backoff_s=initial_backoff_s,
            max_backoff_s=max_backoff_s,
        ),
        name=name,
    )


def install_shutdown_handlers(
    stop_event: asyncio.Event, main_task: asyncio.Task, phase: dict
) -> None:
    """Translate SIGTERM/SIGINT into a graceful shutdown.

    During streaming (``phase["streaming"]``) the handler sets ``stop_event``
    so every loop exits and main()'s finally-block flushes the engine and
    closes the DB — this is what ``systemctl stop`` sends. Before streaming
    starts (refresh, initial sweep) nothing watches ``stop_event``, so the
    main task is cancelled instead; its finally-blocks still run.

    The handler may fire twice for one signal when the embedded uvicorn
    server re-raises the signal it captured, so it is idempotent.
    """
    loop = asyncio.get_running_loop()

    def _on_signal(sig: signal.Signals) -> None:
        if not phase.get("streaming") and not phase.get("cancelled"):
            logger.info("Received %s before streaming — cancelling startup", sig.name)
            phase["cancelled"] = True
            main_task.cancel()
        elif not stop_event.is_set():
            logger.info("Received %s — shutting down gracefully", sig.name)
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _on_signal, sig)
        except (NotImplementedError, RuntimeError):
            # Windows, or not running in the main thread.
            logger.debug("signal handler for %s not installed", sig.name)
