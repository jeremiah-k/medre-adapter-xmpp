"""Bounded ingress and lifetime reconnect ownership for one XMPP account."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Awaitable, Mapping
from typing import Protocol
from .codec import InboundStanza
from .typing import CHATSTATES, activity_from_stanza
from medre.core.contracts.typing import TypingActivity, TypingUpdate

from medre.core.contracts.adapter import AdapterPermanentError, AdapterSendError

from .config import XMPPConfig

_logger = logging.getLogger(__name__)


class _SessionConnection(Protocol):
    """Structural contract for every connection this session owns.

    Native startup binds :class:`SlixConnection`; tests bind lightweight
    fakes exposing the same lifecycle, message and activity ports.
    """

    async def open(self) -> None: ...

    async def wait_closed(self) -> None: ...

    async def close(self) -> None: ...

    async def send(self, payload: Mapping[str, str]) -> str | None: ...

    async def send_typing(self, update: TypingUpdate) -> None: ...


class XMPPSession:
    def __init__(
        self,
        config: XMPPConfig,
        receive: Callable[[InboundStanza], Awaitable[None]],
        *,
        connection_factory: Callable[..., _SessionConnection] | None = None,
        typing_receive: Callable[[TypingActivity], Awaitable[None]] | None = None,
    ) -> None:
        self.config = config
        self.receive = receive
        self._typing_receive = typing_receive
        self._typing_worker: asyncio.Task[None] | None = None
        self._typing_changed = asyncio.Event()
        self._typing_pending: dict[tuple[str, str], TypingActivity] = {}
        self.dropped_typing = 0
        self._connection_factory = connection_factory
        self._connection: _SessionConnection | None = None
        self._runner: asyncio.Task[None] | None = None
        self._worker: asyncio.Task[None] | None = None
        self._ready: asyncio.Future[None] | None = None
        self._queue: asyncio.Queue[InboundStanza] = asyncio.Queue(
            maxsize=config.inbound_queue_size
        )
        self._health_callback: Callable[[], None] | None = None
        self.state = "unknown"
        self._accepting = False
        self.dropped_inbound = 0
        self.failed_inbound = 0
        self.reconnects = 0

    def set_health_callback(self, callback: Callable[[], None] | None) -> None:
        """Attach a diagnostic observer without changing native connection ownership."""
        self._health_callback = callback

    def _set_state(self, state: str) -> None:
        """Publish settled native state after updating the explicit-probe observation."""
        self.state = state
        if (
            state in {"healthy", "degraded", "failed"}
            and self._health_callback is not None
        ):
            try:
                self._health_callback()
            except Exception as exc:
                _logger.warning("XMPP health observer failed (%s)", type(exc).__name__)

    async def start(self) -> None:
        if self._runner is not None:
            raise RuntimeError("XMPP session is started")
        self._set_state("starting")
        self._accepting = True
        self._ready = asyncio.get_running_loop().create_future()
        self._worker = asyncio.create_task(self._consume(), name="xmpp-ingress")
        if self._typing_receive is not None:
            self._typing_worker = asyncio.create_task(
                self._consume_typing(), name="xmpp-typing"
            )
        self._runner = asyncio.create_task(self._run(), name="xmpp-reconnect")
        try:
            await asyncio.wait_for(
                asyncio.shield(self._ready), self.config.connect_timeout_seconds
            )
        except TimeoutError:
            await self.stop()
            raise AdapterSendError(
                "Timed out waiting for XMPP authentication and room joins"
            ) from None
        except BaseException:
            await self.stop()
            raise

    def _enqueue(self, stanza: InboundStanza) -> None:
        if not self._accepting:
            return
        if self._typing_receive is not None:
            activity = activity_from_stanza(stanza.stanza, self.config)
            if activity is not None:
                key = activity.channel_id, activity.sender_id
                if key in self._typing_pending or len(self._typing_pending) < 1024:
                    self._typing_pending[key] = activity
                    self._typing_changed.set()
                else:
                    self.dropped_typing += 1
        # Bodyless chat state never consumes ordinary message queue capacity.
        if any(
            child.tag.startswith("{" + CHATSTATES + "}") for child in stanza.stanza
        ) and not any(child.tag.endswith("}body") for child in stanza.stanza):
            return
        try:
            self._queue.put_nowait(stanza)
        except asyncio.QueueFull:
            self.dropped_inbound += 1
            # No fake durable acceptance or protocol receipt is emitted.
            _logger.warning("XMPP inbound queue is full; message was not admitted")

    async def _consume(self) -> None:
        while True:
            stanza = await self._queue.get()
            try:
                await self.receive(stanza)
            except Exception:
                self.failed_inbound += 1
                _logger.warning("XMPP inbound message was not admitted")
            finally:
                self._queue.task_done()

    async def _consume_typing(self) -> None:
        """Consume a bounded coalesced mailbox independently of ordinary ingress."""
        while True:
            await self._typing_changed.wait()
            self._typing_changed.clear()
            pending, self._typing_pending = self._typing_pending, {}
            for activity in pending.values():
                try:
                    if self._typing_receive is not None:
                        await self._typing_receive(activity)
                except Exception:
                    self.dropped_typing += 1
                    _logger.debug("XMPP transient typing admission failed")

    async def send_typing(self, update: TypingUpdate) -> None:
        """Refuse disconnected native writes and keep SDK ownership in the connection."""
        connection = self._connection
        if self.state != "healthy" or connection is None:
            raise AdapterSendError("XMPP typing session is not ready")
        await connection.send_typing(update)

    async def _run(self) -> None:
        """Observe cancellation across connection setup, cleanup and reconnect backoff."""
        try:
            await self._run_connections()
        except asyncio.CancelledError:
            if self._accepting:
                self._set_state("failed")
            raise

    async def _run_connections(self) -> None:
        """Own native connection replacement while retaining the outer cancellation fence."""
        from .connection import SlixConnection

        factory = self._connection_factory or SlixConnection
        while True:
            connection: _SessionConnection | None = None
            try:
                connection = factory(self.config, self._enqueue)
                self._connection = connection
                await connection.open()
                self._set_state("healthy")
                if self._ready is not None and not self._ready.done():
                    self._ready.set_result(None)
                await connection.wait_closed()
                self._set_state("degraded")
            except AdapterPermanentError:
                self._set_state("failed")
                if self._ready is not None and not self._ready.done():
                    self._ready.set_exception(
                        AdapterPermanentError(
                            "XMPP authentication or configuration requires attention"
                        )
                    )
                return
            except Exception:
                self._set_state("degraded")
                _logger.warning(
                    "XMPP session unavailable; reconnecting at a bounded rate"
                )
            finally:
                self._connection = None
                if connection is not None:
                    # Teardown errors must not end the reconnect loop; only
                    # cancellation is allowed to propagate out of cleanup.
                    try:
                        await connection.close()
                    except Exception:
                        _logger.warning("XMPP connection cleanup failed")
            self._set_state("degraded")
            self.reconnects += 1
            await asyncio.sleep(self.config.reconnect_delay_seconds)

    async def send(self, payload: Mapping[str, str]) -> str | None:
        connection = self._connection
        if self.state != "healthy" or connection is None:
            raise AdapterSendError("XMPP session is not ready")
        return await connection.send(payload)

    async def stop(self, timeout: float = 5.0) -> None:
        self._set_state("stopping")
        self._accepting = False
        tasks = [
            task
            for task in (self._runner, self._worker, self._typing_worker)
            if task is not None
        ]
        for task in tasks:
            task.cancel()
        try:
            async with asyncio.timeout(timeout):
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            while not self._queue.empty():
                self._queue.get_nowait()
                self._queue.task_done()
                self.dropped_inbound += 1
            self._runner = self._worker = self._typing_worker = None
            self._typing_pending.clear()
            self._connection = None
            if self._ready is not None:
                if not self._ready.done():
                    self._ready.cancel()
                elif not self._ready.cancelled():
                    self._ready.exception()
            self._set_state("unknown")
