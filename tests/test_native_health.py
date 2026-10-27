"""XMPP native reconnect state reaches core without health probes or extra traffic."""

import asyncio
from dataclasses import replace

import pytest

from medre.adapters.xmpp.adapter import XMPPAdapter
from medre.adapters.xmpp.session import XMPPSession
from medre.core.contracts.adapter import AdapterPermanentError


class HealthReports(list):
    """Record native observations and wake tests from the actual producer callback."""

    def __init__(self):
        super().__init__()
        self.changed = asyncio.Event()

    def append(self, info):
        """Capture one native observation and release any health-transition waiter."""
        super().append(info)
        self.changed.set()

    async def wait_for(self, health):
        """Wait for reported state without sampling health or scheduling a polling loop."""
        async with asyncio.timeout(3):
            while not self or self[-1].health != health:
                self.changed.clear()
                await self.changed.wait()


class Connection:
    """Inert transport retaining the real session's connection and reconnect owner."""

    def __init__(self, gate=None, failure=None):
        self.gate = gate
        self.failure = failure
        self.closed = asyncio.Event()

    async def open(self):
        """A completed open represents authentication and configured room joins."""
        if self.gate is not None:
            await self.gate.wait()
        if self.failure is not None:
            raise self.failure

    async def wait_closed(self):
        """Retain ownership until the controlled native disconnect."""
        await self.closed.wait()

    async def close(self):
        """Release the owned transport without connecting to a server."""
        self.closed.set()


async def test_disconnect_and_rejoin_publish_without_probe(config, context):
    """Loss and a completed replacement open publish degraded then healthy."""
    ctx, _ = context
    reports = HealthReports()
    ctx.report_health = reports.append
    gate = asyncio.Event()
    connections = []

    def connection(*args):
        """Hold the second connection before its authentication/join boundary."""
        native = Connection(gate if connections else None)
        connections.append(native)
        return native

    def session(cfg, receive, **kwargs):
        """Use the actual XMPP reconnect loop with controlled native connections."""
        return XMPPSession(cfg, receive, connection_factory=connection, **kwargs)

    adapter = XMPPAdapter(
        replace(config, reconnect_delay_seconds=0.01), session_factory=session
    )
    await adapter.start(ctx)
    try:
        assert reports[-1].health == "healthy"
        connections[0].closed.set()
        await reports.wait_for("degraded")
        gate.set()
        await reports.wait_for("healthy")
        count = len(reports)
        adapter.session._set_state("healthy")
        assert len(reports) == count
    finally:
        gate.set()
        await adapter.stop()


async def test_permanent_rejoin_failure_is_reported(config, context):
    """Authentication refusal after disconnect becomes failed native health."""
    ctx, _ = context
    reports = HealthReports()
    ctx.report_health = reports.append
    connections = []

    def connection(*args):
        """Refuse only the replacement native owner."""
        native = Connection(
            failure=(
                AdapterPermanentError("authentication refused") if connections else None
            )
        )
        connections.append(native)
        return native

    adapter = XMPPAdapter(
        replace(config, reconnect_delay_seconds=0.01),
        session_factory=lambda cfg, receive: XMPPSession(
            cfg, receive, connection_factory=connection
        ),
    )
    await adapter.start(ctx)
    try:
        connections[0].closed.set()
        await reports.wait_for("failed")
    finally:
        await adapter.stop()


async def test_retired_callback_and_observer_errors_are_contained(config, context):
    """Optional diagnostics cannot change native ownership or publish a replacement's health."""
    ctx, _ = context
    reports = HealthReports()

    def refused(info):
        """Model an unavailable diagnostic observer."""
        raise RuntimeError("observer unavailable")

    ctx.report_health = refused
    adapter = XMPPAdapter(
        config,
        session_factory=lambda cfg, receive: XMPPSession(
            cfg, receive, connection_factory=lambda *args: Connection()
        ),
    )
    await adapter.start(ctx)
    old = adapter.session._health_callback
    ctx.report_health = reports.append
    adapter.session._set_state("healthy")
    assert reports[-1].health == "healthy"
    await adapter.stop()
    await adapter.start(ctx)
    try:
        count = len(reports)
        old()
        assert len(reports) == count
    finally:
        await adapter.stop()


async def test_disconnect_is_reported_before_native_cleanup_finishes(config, context):
    """Slow native cleanup cannot leave an already closed connection apparently healthy."""
    ctx, _ = context
    reports = HealthReports()
    ctx.report_health = reports.append
    entered, release = asyncio.Event(), asyncio.Event()

    class SlowClose(Connection):
        """Retain native teardown after the stream has already closed."""

        async def close(self):
            """Expose the cleanup wait independently from the disconnect boundary."""
            entered.set()
            await release.wait()
            await super().close()

    native = SlowClose()
    adapter = XMPPAdapter(
        config,
        session_factory=lambda cfg, receive: XMPPSession(
            cfg, receive, connection_factory=lambda *args: native
        ),
    )
    await adapter.start(ctx)
    try:
        native.closed.set()
        await asyncio.wait_for(entered.wait(), 3)
        assert reports[-1].health == "degraded"
    finally:
        release.set()
        await adapter.stop()


@pytest.mark.parametrize("phase", ["backoff", "cleanup"])
@pytest.mark.parametrize("shutdown", [False, True])
async def test_runner_cancellation_covers_cleanup_and_backoff(
    config, context, phase, shutdown
):
    """Only shutdown can retire the owned runner silently, at every awaited phase."""
    ctx, _ = context
    reports = HealthReports()
    ctx.report_health = reports.append
    entered, release = asyncio.Event(), asyncio.Event()

    class GatedClose(Connection):
        """Separate the closed transport from pending native cleanup or backoff."""

        async def close(self):
            """Retain cleanup only for the requested cancellation phase."""
            entered.set()
            if phase == "cleanup":
                await release.wait()
            await super().close()

    connection = GatedClose()
    adapter = XMPPAdapter(
        replace(config, reconnect_delay_seconds=60),
        session_factory=lambda cfg, receive: XMPPSession(
            cfg, receive, connection_factory=lambda *args: connection
        ),
    )
    await adapter.start(ctx)
    try:
        connection.closed.set()
        await asyncio.wait_for(entered.wait(), 3)
        if shutdown:
            await adapter.stop()
            assert all(info.health != "failed" for info in reports)
        else:
            adapter.session._runner.cancel()
            await asyncio.gather(adapter.session._runner, return_exceptions=True)
            assert reports[-1].health == "failed"
    finally:
        release.set()
        await adapter.stop()
