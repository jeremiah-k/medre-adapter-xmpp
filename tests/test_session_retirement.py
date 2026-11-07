"""Cleanup ownership and admission fences survive concurrent cancellation."""

import asyncio

import pytest

from medre.adapters.xmpp.adapter import XMPPAdapter
from medre.adapters.xmpp.session import XMPPSession, _SessionPhase
from medre.core.contracts.typing import TypingActivity
from test_session_machine import _Connection


@pytest.mark.parametrize("through_adapter", [False, True])
@pytest.mark.parametrize("timeout", [0, 0.01])
async def test_concurrent_stop_uses_deadline_while_waiting_for_lock(
    config, through_adapter, timeout
):
    entered, release = asyncio.Event(), asyncio.Event()

    class SlowClose(_Connection):
        async def close(self):
            entered.set()
            await release.wait()
            await super().close()

    session = XMPPSession(
        config, lambda _: asyncio.sleep(0), connection_factory=SlowClose
    )
    await session.start()
    adapter = XMPPAdapter(config)
    adapter.session = session
    owner = adapter if through_adapter else session
    stopping = asyncio.create_task(owner.stop(timeout=10))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        with pytest.raises(RuntimeError, match="deadline"):
            await asyncio.wait_for(owner.stop(timeout=timeout), 1)
        assert not stopping.done()
        assert session._phase is _SessionPhase.STOPPING
    finally:
        release.set()
        await asyncio.wait_for(stopping, 1)
    assert session._phase is _SessionPhase.STOPPED


@pytest.mark.parametrize("timeout", [True, float("nan"), float("inf"), -1, None, "5"])
async def test_invalid_adapter_stop_keeps_health_observation(config, timeout):
    adapter = XMPPAdapter(config)
    adapter._health_active = True
    adapter._reported_health = "healthy"
    before = adapter._health_epoch
    with pytest.raises(ValueError, match="finite and nonnegative"):
        await adapter.stop(timeout=timeout)
    assert adapter._health_active
    assert adapter._reported_health == "healthy"
    assert adapter._health_epoch == before


async def test_failed_native_close_retains_owner_until_retry(config):
    connections = []

    class FailsOnce(_Connection):
        async def close(self):
            self.close_count += 1
            if self.close_count == 1:
                raise RuntimeError("native cleanup failed")
            self.closed.set()

    def factory(*args):
        connection = FailsOnce(*args)
        connections.append(connection)
        return connection

    session = XMPPSession(
        config, lambda _: asyncio.sleep(0), connection_factory=factory
    )
    await session.start()
    with pytest.raises(RuntimeError, match="cleanup"):
        await session.stop()
    assert session._phase is _SessionPhase.ABANDONED
    assert session.state == "failed"
    assert session._connection is connections[0]
    with pytest.raises(RuntimeError, match="not fully retired"):
        await session.start()
    assert len(connections) == 1
    await session.stop()
    assert connections[0].close_count == 2
    assert session._connection is None
    assert session._phase is _SessionPhase.STOPPED


async def test_cancelled_start_keeps_cancellation_when_cleanup_is_unfinished(
    config, monkeypatch
):
    entered, release = asyncio.Event(), asyncio.Event()

    class StubbornOpen(_Connection):
        async def open(self):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()

    session = XMPPSession(
        config, lambda _: asyncio.sleep(0), connection_factory=StubbornOpen
    )
    stop = session.stop

    async def immediate_stop():
        await stop(timeout=0)

    monkeypatch.setattr(session, "stop", immediate_stop)
    starting = asyncio.create_task(session.start())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        starting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(starting, 1)
        assert session._phase is _SessionPhase.ABANDONED
        assert session._runner is not None
        release.set()
        await asyncio.wait_for(session._runner, 1)
    finally:
        release.set()
        await stop(timeout=1)


async def test_adapter_cancelled_start_retains_failed_cleanup_owner(config, context):
    class FailedStart:
        def __init__(self, *args, **kwargs):
            pass

        async def start(self):
            raise asyncio.CancelledError()

        async def stop(self):
            raise RuntimeError("native cleanup failed")

    adapter = XMPPAdapter(config, session_factory=FailedStart)
    with pytest.raises(asyncio.CancelledError):
        await adapter.start(context[0])
    assert adapter.session is not None


async def test_abandoned_typing_batch_never_publishes_remaining_items(config):
    entered, release = asyncio.Event(), asyncio.Event()
    published = []

    async def typing(activity):
        published.append(activity)
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()

    session = XMPPSession(
        config,
        lambda _: asyncio.sleep(0),
        connection_factory=_Connection,
        typing_receive=typing,
    )
    await session.start()
    activities = [TypingActivity("room", f"peer-{index}", True) for index in range(3)]
    session._typing_pending.update({(a.channel_id, a.sender_id): a for a in activities})
    session._typing_changed.set()
    try:
        await asyncio.wait_for(entered.wait(), 1)
        with pytest.raises(RuntimeError, match="cancellation-resistant"):
            await session.stop(timeout=0)
        assert session._phase is _SessionPhase.ABANDONED
        release.set()
        await asyncio.wait_for(session._typing_worker, 1)
        assert published == activities[:1]
        assert session.dropped_typing == 2
    finally:
        release.set()
        await session.stop(timeout=1)


async def test_adapter_stop_during_start_keeps_original_cancellation(config, context):
    entered = asyncio.Event()

    class SlowOpen(_Connection):
        async def open(self):
            entered.set()
            await asyncio.Event().wait()

    adapter = XMPPAdapter(
        config,
        session_factory=lambda cfg, receive: XMPPSession(
            cfg, receive, connection_factory=SlowOpen
        ),
    )
    starting = asyncio.create_task(adapter.start(context[0]))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await adapter.stop()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(starting, 1)
        assert adapter.session is None
        assert not adapter._health_active
    finally:
        if not starting.done():
            starting.cancel()
            await asyncio.gather(starting, return_exceptions=True)
        await adapter.stop()
