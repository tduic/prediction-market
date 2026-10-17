"""Tests for core.task_supervisor — crash logging, alerting and restart."""

import asyncio

import pytest

from core.alerting import Severity, set_alert_manager
from core.task_supervisor import supervise, supervised_task


class _RecordingAlerts:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send_nowait(self, **kwargs) -> None:
        self.sent.append(kwargs)


@pytest.fixture
def alerts():
    rec = _RecordingAlerts()
    set_alert_manager(rec)  # type: ignore[arg-type]
    yield rec
    set_alert_manager(None)


@pytest.mark.asyncio
async def test_crash_is_alerted_and_restarted(alerts):
    stop = asyncio.Event()
    calls = 0

    async def flaky():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("boom")
        stop.set()  # second run: behave, then request shutdown

    await asyncio.wait_for(
        supervise("ws:test", flaky, stop, initial_backoff_s=0.01), timeout=2
    )
    assert calls == 2
    assert len(alerts.sent) == 1
    assert alerts.sent[0]["severity"] == Severity.CRITICAL
    assert "ws:test" in alerts.sent[0]["title"]
    assert "RuntimeError" in alerts.sent[0]["message"]
    assert "boom" in alerts.sent[0]["message"]


@pytest.mark.asyncio
async def test_crash_alert_truncates_exception_text(alerts):
    stop = asyncio.Event()

    async def crash_long():
        raise RuntimeError("x" * 1000)

    await supervise("one", crash_long, stop, restart=False)
    message = alerts.sent[0]["message"]
    assert "x" * 200 in message
    assert "x" * 201 not in message


@pytest.mark.asyncio
async def test_unexpected_return_of_loop_is_restarted(alerts):
    stop = asyncio.Event()
    calls = 0

    async def returns_early():
        nonlocal calls
        calls += 1
        if calls >= 3:
            stop.set()

    await asyncio.wait_for(
        supervise("loop", returns_early, stop, initial_backoff_s=0.01), timeout=2
    )
    assert calls == 3
    assert len(alerts.sent) == 2


@pytest.mark.asyncio
async def test_clean_exit_after_stop_is_silent(alerts):
    stop = asyncio.Event()

    async def runs_until_stop():
        stop.set()

    await supervise("loop", runs_until_stop, stop)
    assert alerts.sent == []


@pytest.mark.asyncio
async def test_one_shot_failure_alerts_without_restart(alerts):
    stop = asyncio.Event()
    calls = 0

    async def one_shot():
        nonlocal calls
        calls += 1
        raise ValueError("nope")

    await supervise("warmup", one_shot, stop, restart=False)
    assert calls == 1
    assert len(alerts.sent) == 1


@pytest.mark.asyncio
async def test_one_shot_success_is_silent(alerts):
    stop = asyncio.Event()

    async def one_shot():
        return None

    await supervise("warmup", one_shot, stop, restart=False)
    assert alerts.sent == []


@pytest.mark.asyncio
async def test_stop_during_backoff_ends_supervision(alerts):
    stop = asyncio.Event()

    async def always_crash():
        raise RuntimeError("x")

    task = supervised_task(
        always_crash, name="crashy", stop_event=stop, initial_backoff_s=30
    )
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=1)
    assert len(alerts.sent) == 1


@pytest.mark.asyncio
async def test_cancellation_propagates(alerts):
    stop = asyncio.Event()

    async def forever():
        await asyncio.sleep(3600)

    task = supervised_task(forever, name="forever", stop_event=stop)
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert alerts.sent == []


# ── SIGTERM / SIGINT → graceful shutdown ─────────────────────────────────────


@pytest.fixture
async def restore_signal_handlers():
    import signal as _signal

    loop = asyncio.get_running_loop()
    yield
    for sig in (_signal.SIGTERM, _signal.SIGINT):
        loop.remove_signal_handler(sig)


@pytest.mark.asyncio
async def test_sigterm_while_streaming_sets_stop_event(restore_signal_handlers):
    import os
    import signal as _signal

    from core.task_supervisor import install_shutdown_handlers

    stop = asyncio.Event()
    main_like = asyncio.create_task(asyncio.sleep(3600))
    install_shutdown_handlers(stop, main_like, {"streaming": True})
    os.kill(os.getpid(), _signal.SIGTERM)
    await asyncio.wait_for(stop.wait(), timeout=2)
    assert not main_like.cancelled()
    main_like.cancel()


@pytest.mark.asyncio
async def test_sigterm_before_streaming_cancels_main(restore_signal_handlers):
    import os
    import signal as _signal

    from core.task_supervisor import install_shutdown_handlers

    stop = asyncio.Event()
    main_like = asyncio.create_task(asyncio.sleep(3600))
    phase = {"streaming": False}
    install_shutdown_handlers(stop, main_like, phase)
    os.kill(os.getpid(), _signal.SIGTERM)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(main_like, timeout=2)
    assert stop.is_set()
    # A re-raised signal (uvicorn) must not cancel a second time.
    os.kill(os.getpid(), _signal.SIGTERM)
    await asyncio.sleep(0.05)
    assert phase["cancelled"] is True
