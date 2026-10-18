"""Bounded ingress and lifetime reconnect ownership for one XMPP account."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Awaitable, Mapping
from typing import Any
from xml.etree.ElementTree import Element

from medre.core.contracts.adapter import AdapterPermanentError, AdapterSendError

from .config import XMPPConfig

_logger = logging.getLogger(__name__)


class XMPPSession:
    def __init__(
        self,
        config: XMPPConfig,
        receive: Callable[[Element], Awaitable[None]],
        *,
        connection_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.config = config
        self.receive = receive
        self._connection_factory = connection_factory
        self._connection = None
        self._runner = None
        self._worker = None
        self._ready = None
        self._queue = asyncio.Queue(maxsize=config.inbound_queue_size)
        self.state = "unknown"
        self._accepting = False
        self.dropped_inbound = 0
        self.failed_inbound = 0
        self.reconnects = 0

    async def start(self) -> None:
        if self._runner is not None:
            raise RuntimeError("XMPP session is started")
        self.state = "starting"
        self._accepting = True
        self._ready = asyncio.get_running_loop().create_future()
        self._worker = asyncio.create_task(self._consume(), name="xmpp-ingress")
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

    def _enqueue(self, stanza: Element) -> None:
        if not self._accepting:
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

    async def _run(self) -> None:
        from .connection import SlixConnection

        factory = self._connection_factory or SlixConnection
        while True:
            connection = None
            try:
                connection = factory(self.config, self._enqueue)
                self._connection = connection
                await connection.open()
                self.state = "healthy"
                if not self._ready.done():
                    self._ready.set_result(None)
                await connection.wait_closed()
            except AdapterPermanentError:
                self.state = "failed"
                if not self._ready.done():
                    self._ready.set_exception(
                        AdapterPermanentError(
                            "XMPP authentication or configuration requires attention"
                        )
                    )
                return
            except Exception:
                self.state = "degraded"
                _logger.warning(
                    "XMPP session unavailable; reconnecting at a bounded rate"
                )
            finally:
                self._connection = None
                if connection is not None:
                    await connection.close()
            self.state = "degraded"
            self.reconnects += 1
            await asyncio.sleep(self.config.reconnect_delay_seconds)

    async def send(self, payload: Mapping[str, str]) -> str:
        connection = self._connection
        if self.state != "healthy" or connection is None:
            raise AdapterSendError("XMPP session is not ready")
        return connection.send(payload)

    async def stop(self, timeout: float = 5.0) -> None:
        self.state = "stopping"
        self._accepting = False
        tasks = [task for task in (self._runner, self._worker) if task is not None]
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
            self._runner = self._worker = None
            self._connection = None
            if self._ready is not None:
                if not self._ready.done():
                    self._ready.cancel()
                elif not self._ready.cancelled():
                    self._ready.exception()
            self.state = "unknown"
