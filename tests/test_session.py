import asyncio
from dataclasses import replace

import pytest

from medre.core.contracts.adapter import AdapterPermanentError, AdapterSendError
from medre_adapter_xmpp.session import XMPPSession


class Connection:
    def __init__(self, config, receive):
        self.receive = receive
        self.opened = asyncio.Event()
        self.closed = asyncio.Event()
        self.close_calls = 0
        self.sent = []

    async def open(self):
        self.opened.set()

    async def wait_closed(self):
        await self.closed.wait()

    def send(self, payload):
        self.sent.append(payload)
        return "native-id"

    async def close(self):
        self.close_calls += 1
        self.closed.set()


async def test_session_ready_send_and_shutdown_release_owned_tasks(config):
    connections = []

    def factory(*args):
        connection = Connection(*args)
        connections.append(connection)
        return connection

    async def receive(_):
        pass

    session = XMPPSession(config, receive, connection_factory=factory)
    await session.start()
    runner, worker = session._runner, session._worker
    assert session.state == "healthy"
    assert await session.send({"body": "hello"}) == "native-id"
    await session.stop()
    assert runner.done() and worker.done()
    assert connections[0].close_calls == 1
    assert session.state == "unknown"
    with pytest.raises(AdapterSendError):
        await session.send({"body": "late"})


async def test_disconnect_reconnects_and_rejoins_through_a_fresh_connection(config):
    second_open = asyncio.Event()
    connections = []

    def factory(*args):
        connection = Connection(*args)
        connections.append(connection)
        if len(connections) == 2:
            original = connection.open

            async def open_second():
                await original()
                second_open.set()

            connection.open = open_second
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
    await asyncio.wait_for(second_open.wait(), 2)
    assert session.state == "healthy"
    assert session.reconnects == 1
    assert connections[0].close_calls == 1
    await session.stop()
    assert connections[1].close_calls == 1


async def test_auth_failure_is_permanent_and_cleanup_is_bounded(config):
    attempts = []

    class Rejected(Connection):
        async def open(self):
            attempts.append(self)
            raise AdapterPermanentError("rejected")

    async def receive(_):
        pass

    session = XMPPSession(config, receive, connection_factory=Rejected)
    with pytest.raises(AdapterPermanentError):
        await session.start()
    assert len(attempts) == 1
    assert attempts[0].close_calls == 1
    assert session._runner is session._worker is None


async def test_startup_timeout_cancels_connection_and_worker(config):
    connections = []

    class Hanging(Connection):
        async def open(self):
            connections.append(self)
            await asyncio.Event().wait()

    async def receive(_):
        pass

    session = XMPPSession(
        replace(config, connect_timeout_seconds=0.02),
        receive,
        connection_factory=Hanging,
    )
    with pytest.raises(AdapterSendError, match="Timed out"):
        await session.start()
    assert connections[0].close_calls == 1
    assert session._runner is session._worker is None


async def test_ingress_queue_bounds_pending_work_and_counts_rejected_admission(config):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def receive(_):
        entered.set()
        await release.wait()
        raise RuntimeError("admission rejected")

    session = XMPPSession(
        replace(config, inbound_queue_size=1), receive, connection_factory=Connection
    )
    await session.start()
    session._enqueue("first")
    await asyncio.wait_for(entered.wait(), 2)
    session._enqueue("second")
    session._enqueue("overflow")
    assert session.dropped_inbound == 1
    release.set()
    await asyncio.wait_for(session._queue.join(), 2)
    assert session.failed_inbound == 2
    await session.stop()
    session._enqueue("after-stop")
    assert session._queue.empty()


async def test_cancelling_start_does_not_leave_connection_work(config):
    connection_opening = asyncio.Event()
    connections = []

    class Hanging(Connection):
        async def open(self):
            connections.append(self)
            connection_opening.set()
            await asyncio.Event().wait()

    async def receive(_):
        pass

    session = XMPPSession(config, receive, connection_factory=Hanging)
    task = asyncio.create_task(session.start())
    await asyncio.wait_for(connection_opening.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert connections[0].close_calls == 1
    assert session._runner is session._worker is None
