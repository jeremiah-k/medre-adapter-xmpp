"""Session lifecycle transitions remain truthful under cancellation-resistant SDKs."""

import asyncio
from dataclasses import replace

import pytest

from medre.adapters.xmpp.session import XMPPSession, _SessionPhase
from medre.adapters.xmpp.adapter import XMPPAdapter
from medre.core.contracts.adapter import AdapterSendError


class _Connection:
    def __init__(self, config, receive):
        self.closed = asyncio.Event()
        self.opened = asyncio.Event()
        self.close_count = 0

    async def open(self):
        self.opened.set()

    async def wait_closed(self):
        await self.closed.wait()

    async def close(self):
        self.close_count += 1
        self.closed.set()

    async def send(self, payload):
        return "native"

    async def send_typing(self, update):
        return None


async def test_uncooperative_reconnect_cannot_survive_deadline_as_healthy(config):
    entered, release = asyncio.Event(), asyncio.Event()
    connections = []

    class _Stubborn(_Connection):
        async def open(self):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            self.opened.set()

    def factory(*args):
        connection = _Connection(*args) if not connections else _Stubborn(*args)
        connections.append(connection)
        return connection

    async def receive(_):
        pass

    session = XMPPSession(
        replace(config, reconnect_delay_seconds=0.001),
        receive,
        connection_factory=factory,
    )
    await session.start()
    connections[0].closed.set()
    await asyncio.wait_for(entered.wait(), 2)
    try:
        with pytest.raises(RuntimeError, match="cancellation-resistant"):
            await asyncio.wait_for(session.stop(timeout=0.01), 1)
        assert session._phase is _SessionPhase.ABANDONED
        assert session.state == "failed"
        assert session._runner is not None and not session._runner.done()
        with pytest.raises(RuntimeError, match="not fully retired"):
            await session.start()
        with pytest.raises(AdapterSendError):
            await session.send({"body": "must not send"})
        release.set()
        await asyncio.wait_for(session._runner, 2)
        assert session.state == "failed"
        assert len(connections) == 2
        await session.stop()
        assert session._phase is _SessionPhase.STOPPED
        assert session.state == "unknown"
    finally:
        release.set()
        if session._runner is not None and not session._runner.done():
            await asyncio.wait_for(session.stop(timeout=1), 2)


async def test_clean_stopped_session_can_start_fresh_without_old_queue(config):
    from conftest import stanza
    from medre.adapters.xmpp.codec import InboundStanza

    connections, received = [], []
    admitted = asyncio.Event()

    def factory(*args):
        connection = _Connection(*args)
        connections.append(connection)
        return connection

    async def receive(packet):
        received.append(packet)
        admitted.set()

    session = XMPPSession(config, receive, connection_factory=factory)
    await session.start()
    session._enqueue(InboundStanza(stanza(id="stale")))
    await session.stop()
    assert session._queue.empty()
    assert session.dropped_inbound == 1
    assert received == []
    await session.start()
    fresh = InboundStanza(stanza(id="fresh"))
    try:
        session._enqueue(fresh)
        await asyncio.wait_for(admitted.wait(), 1)
        assert received == [fresh]
    finally:
        await session.stop()
    assert session._phase is _SessionPhase.STOPPED
    assert session._runner is None
    assert len(connections) == 2


async def test_concurrent_start_cannot_spawn_second_connection(config):
    entered, release = asyncio.Event(), asyncio.Event()
    connections = []

    class _Slow(_Connection):
        async def open(self):
            entered.set()
            await release.wait()
            self.opened.set()

    def factory(*args):
        connection = _Slow(*args)
        connections.append(connection)
        return connection

    async def receive(_):
        pass

    session = XMPPSession(config, receive, connection_factory=factory)
    first = asyncio.create_task(session.start())
    await entered.wait()
    try:
        with pytest.raises(RuntimeError, match="already active"):
            await session.start()
        assert len(connections) == 1
    finally:
        release.set()
        await first
        await session.stop()


@pytest.mark.parametrize("timeout", [True, float("nan"), float("inf"), -1])
async def test_invalid_stop_timeout_does_not_mutate_phase(config, timeout):
    session = XMPPSession(config, lambda _: asyncio.sleep(0))
    with pytest.raises(ValueError, match="finite and nonnegative"):
        await session.stop(timeout=timeout)
    assert session._phase is _SessionPhase.NEW


async def test_adapter_retains_session_ownership_when_stop_fails(config):
    class _StopFailure:
        calls = 0

        async def stop(self, timeout=5.0):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("unfinished native runner")

    adapter = XMPPAdapter(config)
    session = _StopFailure()
    adapter.session = session
    with pytest.raises(RuntimeError, match="unfinished native runner"):
        await adapter.stop(timeout=0)
    assert adapter.session is session
    with pytest.raises(RuntimeError, match="adapter is started"):
        await adapter.start(None)
    await adapter.stop()
    assert adapter.session is None
    assert session.calls == 2
