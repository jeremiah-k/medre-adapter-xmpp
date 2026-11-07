"""Bounded ingress and lifetime reconnect ownership for one XMPP account."""

from __future__ import annotations

import asyncio
import logging
import math
from enum import Enum
from collections.abc import Callable, Awaitable, Mapping
from typing import Protocol
from .codec import InboundStanza
from .typing import CHATSTATES, activity_from_stanza
from medre.core.contracts.typing import TypingActivity, TypingUpdate

from medre.core.contracts.adapter import AdapterPermanentError, AdapterSendError

from .config import XMPPConfig

_logger = logging.getLogger(__name__)


def validate_stop_timeout(timeout: float) -> None:
    """Reject invalid budgets before changing adapter or session ownership."""
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout < 0
    ):
        raise ValueError("XMPP shutdown timeout must be finite and nonnegative")


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


class _SessionPhase(Enum):
    """Owner-loop lifecycle; health is a separate native observation."""

    NEW = "new"
    STARTING = "starting"
    ACTIVE = "active"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ABANDONED = "abandoned"


_PHASE_TRANSITIONS: dict[_SessionPhase, frozenset[_SessionPhase]] = {
    _SessionPhase.NEW: frozenset({_SessionPhase.STARTING, _SessionPhase.STOPPING}),
    _SessionPhase.STARTING: frozenset({_SessionPhase.ACTIVE, _SessionPhase.STOPPING}),
    _SessionPhase.ACTIVE: frozenset({_SessionPhase.STOPPING}),
    _SessionPhase.STOPPING: frozenset({_SessionPhase.STOPPED, _SessionPhase.ABANDONED}),
    _SessionPhase.STOPPED: frozenset({_SessionPhase.STARTING}),
    _SessionPhase.ABANDONED: frozenset({_SessionPhase.STOPPING}),
}


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
        self._phase = _SessionPhase.NEW
        self._stop_lock = asyncio.Lock()
        self.dropped_inbound = 0
        self.failed_inbound = 0
        self.reconnects = 0

    def _transition(self, phase: _SessionPhase) -> None:
        if phase is self._phase:
            return
        if phase not in _PHASE_TRANSITIONS[self._phase]:
            raise RuntimeError("Invalid XMPP session lifecycle transition")
        self._phase = phase

    @property
    def _accepting(self) -> bool:
        return self._phase in (_SessionPhase.STARTING, _SessionPhase.ACTIVE)

    def _starting(self) -> bool:
        """Re-read the phase after an await; another task may have stopped us."""
        return self._phase is _SessionPhase.STARTING

    def set_health_callback(self, callback: Callable[[], None] | None) -> None:
        """Attach a diagnostic observer without changing native connection ownership."""
        self._health_callback = callback

    def _set_state(self, state: str) -> None:
        """Publish settled native state after updating the explicit-probe observation."""
        if self._phase in (_SessionPhase.STOPPING, _SessionPhase.STOPPED):
            if state not in {"stopping", "unknown"}:
                return
        if self._phase is _SessionPhase.ABANDONED and state != "failed":
            return
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
        if self._phase not in (_SessionPhase.NEW, _SessionPhase.STOPPED):
            raise RuntimeError("XMPP session is already active or not fully retired")
        if any(
            task is not None and not task.done()
            for task in (self._runner, self._worker, self._typing_worker)
        ):
            raise RuntimeError("XMPP session still owns pending work")
        self._transition(_SessionPhase.STARTING)
        self._set_state("starting")
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
            if not self._starting():
                raise AdapterSendError("XMPP session stopped before readiness settled")
            self._transition(_SessionPhase.ACTIVE)
        except TimeoutError:
            await self.stop()
            raise AdapterSendError(
                "Timed out waiting for XMPP authentication and room joins"
            ) from None
        except BaseException:
            try:
                await self.stop()
            except Exception:
                _logger.warning("XMPP startup cleanup retains unfinished native work")
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
            if not self._accepting:
                return

    async def _consume_typing(self) -> None:
        """Consume a bounded coalesced mailbox independently of ordinary ingress."""
        while True:
            await self._typing_changed.wait()
            self._typing_changed.clear()
            pending, self._typing_pending = self._typing_pending, {}
            for index, activity in enumerate(pending.values()):
                if not self._accepting:
                    self.dropped_typing += len(pending) - index
                    return
                try:
                    if self._typing_receive is not None:
                        await self._typing_receive(activity)
                except Exception:
                    self.dropped_typing += 1
                    _logger.debug("XMPP transient typing admission failed")
            if not self._accepting:
                return

    async def send_typing(self, update: TypingUpdate) -> None:
        """Refuse disconnected native writes and keep SDK ownership in the connection."""
        connection = self._connection
        if not self._accepting or self.state != "healthy" or connection is None:
            raise AdapterSendError("XMPP typing session is not ready")
        await connection.send_typing(update)

    async def _run(self) -> None:
        """Observe cancellation across connection setup, cleanup and reconnect backoff."""
        try:
            await self._run_connections()
        except asyncio.CancelledError:
            if self._accepting:
                self._transition(_SessionPhase.STOPPING)
                self._transition(_SessionPhase.ABANDONED)
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
                if not self._accepting:
                    return
                self._set_state("healthy")
                if self._ready is not None and not self._ready.done():
                    self._ready.set_result(None)
                await connection.wait_closed()
                if not self._accepting:
                    return
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
                if connection is not None:
                    try:
                        await self._close_connection(connection)
                    except Exception:
                        # Failed cleanup cannot authorize a replacement owner.
                        if self._phase not in (
                            _SessionPhase.STOPPING,
                            _SessionPhase.ABANDONED,
                        ):
                            self._transition(_SessionPhase.STOPPING)
                        if self._phase is _SessionPhase.STOPPING:
                            self._transition(_SessionPhase.ABANDONED)
                        self._set_state("failed")
                        if self._ready is not None and not self._ready.done():
                            self._ready.set_exception(
                                AdapterSendError("XMPP connection cleanup failed")
                            )
                        raise
            if not self._accepting:
                return
            self._set_state("degraded")
            self.reconnects += 1
            await asyncio.sleep(self.config.reconnect_delay_seconds)

    async def _close_connection(self, connection: _SessionConnection) -> None:
        """Release the native owner only after its cleanup succeeds."""
        await connection.close()
        if self._connection is connection:
            self._connection = None

    async def send(self, payload: Mapping[str, str]) -> str | None:
        connection = self._connection
        if not self._accepting or self.state != "healthy" or connection is None:
            raise AdapterSendError("XMPP session is not ready")
        return await connection.send(payload)

    async def stop(self, timeout: float = 5.0) -> None:
        """Retire one session; never forget a cancellation-resistant task.

        A stop deadline is an upper bound on *waiting*, not proof that a
        native coroutine obeyed cancellation. Until all tasks settle, this
        session cannot restart and will not report healthy or accept ingress.
        """
        validate_stop_timeout(timeout)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        try:
            async with asyncio.timeout_at(deadline):
                await self._stop_lock.acquire()
        except TimeoutError:
            raise RuntimeError(
                "XMPP shutdown deadline expired waiting for cleanup"
            ) from None
        try:
            if self._phase is _SessionPhase.STOPPED:
                return
            self._transition(_SessionPhase.STOPPING)
            self._set_state("stopping")
            tasks = [
                task
                for task in (self._runner, self._worker, self._typing_worker)
                if task is not None
            ]
            for task in tasks:
                if not task.done():
                    task.cancel()
            if self._connection is not None and (
                self._runner is None or self._runner.done()
            ):
                if self._runner is not None and not self._runner.cancelled():
                    self._runner.exception()
                # Retry failed native cleanup under the same session owner and
                # deadline. Do not cancel this freshly created cleanup task.
                self._runner = asyncio.create_task(
                    self._close_connection(self._connection), name="xmpp-cleanup"
                )
                tasks.append(self._runner)
            pending: set[asyncio.Task[None]] = set()
            try:
                if tasks:
                    _, pending = await asyncio.wait(
                        tasks, timeout=max(0.0, deadline - loop.time())
                    )
                if (
                    not pending
                    and self._connection is not None
                    and self._runner is not None
                    and self._runner.cancelled()
                ):
                    # Cancellation can interrupt the runner inside close().
                    # Retire that retained connection within the same deadline.
                    self._runner = asyncio.create_task(
                        self._close_connection(self._connection), name="xmpp-cleanup"
                    )
                    tasks.append(self._runner)
                    _, pending = await asyncio.wait(
                        [self._runner], timeout=max(0.0, deadline - loop.time())
                    )
            finally:
                # Do not suppress external stop() cancellation or claim the
                # tasks were retired when asyncio.wait itself was interrupted.
                pending.update(task for task in tasks if not task.done())
                for task in tasks:
                    if task.done() and not task.cancelled():
                        task.exception()
                while not self._queue.empty():
                    self._queue.get_nowait()
                    self._queue.task_done()
                    self.dropped_inbound += 1
                self._typing_pending.clear()
                if self._ready is not None:
                    if not self._ready.done():
                        self._ready.cancel()
                    elif not self._ready.cancelled():
                        self._ready.exception()
                if pending or self._connection is not None:
                    # Retain each task through the existing references; a
                    # follow-up stop may safely retry after it settles.
                    self._transition(_SessionPhase.ABANDONED)
                    self._set_state("failed")
                else:
                    self._runner = self._worker = self._typing_worker = None
                    self._connection = None
                    self._transition(_SessionPhase.STOPPED)
                    self._set_state("unknown")
            if pending:
                raise RuntimeError(
                    "XMPP session still owns cancellation-resistant work"
                )
            if self._connection is not None:
                raise RuntimeError(
                    "XMPP session still owns failed native connection cleanup"
                )
        finally:
            self._stop_lock.release()
