"""Pinned Slixmpp binding; protocol negotiation and XML encoding stay in the SDK."""

from __future__ import annotations

import asyncio
import ssl
import uuid
from collections.abc import Callable, Mapping
from typing import Any
from xml.etree.ElementTree import Element
from .config import XMPPConfig

from medre.core.contracts.adapter import AdapterPermanentError, AdapterSendError


class SlixConnection:
    def __init__(self, config: XMPPConfig, receive: Callable[[Element], None]) -> None:
        # Native imports occur only when the real session starts.
        from slixmpp import ClientXMPP, JID

        if not config.jid or not config.password:
            raise AdapterPermanentError(
                "XMPP jid and password are required for native startup"
            )
        for value in (config.jid, *config.rooms, *config.peers):
            try:
                prepared = str(JID(value))
            except ValueError:
                raise AdapterPermanentError("Invalid RFC 7622 XMPP identity") from None
            if prepared != value:
                raise AdapterPermanentError(
                    "Use RFC 7622 prepared JIDs consistently in XMPP configuration and routes"
                )
        self.config = config
        self.receive = receive
        self.client = ClientXMPP(config.jid, config.password)
        self.client.ssl_context = ssl.create_default_context()
        self.client.enable_plaintext = False
        self.client.enable_starttls = True
        self.client.enable_direct_tls = True
        self.client.register_plugin("xep_0030")
        self.client.register_plugin("xep_0045")
        self.client.register_plugin("xep_0199")
        self.ready = asyncio.get_running_loop().create_future()
        self.closed = asyncio.Event()
        self._handlers: set[asyncio.Task] = set()
        self._connect_task = None
        self.client.add_event_handler("session_start", self._session_started)
        # A failed SASL mechanism is retryable by Slixmpp; exhaustion is terminal.
        self.client.add_event_handler("failed_all_auth", self._failed_auth)
        self.client.add_event_handler("disconnected", self._disconnected)
        # One message hook handles direct chat and MUC once; never register
        # both message and groupchat_message callbacks for the same stanza.
        self.client.add_event_handler("message", self._message)
        for room in config.rooms:
            self.client.add_event_handler(
                f"muc::{room}::self-presence", self._own_presence
            )

    async def open(self) -> None:
        self._connect_task = self.client.connect(self.config.host, self.config.port)
        self._connect_task.add_done_callback(self._connect_done)
        await asyncio.wait_for(
            asyncio.shield(self.ready), self.config.connect_timeout_seconds
        )

    def _connect_done(self, future: asyncio.Future) -> None:
        if (
            not future.cancelled()
            and future.exception() is not None
            and not self.ready.done()
        ):
            self.ready.set_exception(AdapterSendError("XMPP connection failed"))

    async def _session_started(self, _event: Any) -> None:
        task = asyncio.current_task()
        self._handlers.add(task)
        try:
            self.client.send_presence()
            for room in self.config.rooms:
                result = await self.client.plugin["xep_0045"].join_muc_wait(
                    room,
                    self.config.nickname,
                    maxstanzas=0,
                    timeout=self.config.connect_timeout_seconds,
                )
                # A nick conflict or server rewrite changes echo ownership.
                if str(result[0]["from"]) != f"{room}/{self.config.nickname}":
                    raise AdapterPermanentError(
                        "XMPP room joined under an unexpected occupant identity"
                    )
            self.client.plugin["xep_0199"].enable_keepalive(interval=60, timeout=15)
            if not self.ready.done():
                self.ready.set_result(None)
        except AdapterPermanentError as error:
            if not self.ready.done():
                self.ready.set_exception(error)
        except Exception:
            if not self.ready.done():
                self.ready.set_exception(
                    AdapterSendError("XMPP room or session setup failed")
                )
        finally:
            self._handlers.discard(task)

    def _failed_auth(self, _event: Any) -> None:
        if not self.ready.done():
            self.ready.set_exception(
                AdapterPermanentError("XMPP authentication failed")
            )

    def _disconnected(self, _event: Any) -> None:
        self.closed.set()
        if not self.ready.done():
            self.ready.set_exception(
                AdapterSendError("XMPP disconnected before readiness")
            )

    def _own_presence(self, presence: Any) -> None:
        if presence["type"] in {"unavailable", "error"}:
            self.closed.set()

    def _message(self, message: Any) -> None:
        # The SDK owns this Element for callback lifetime. Copy into the bounded
        # ingress queue so subsequent SDK processing cannot mutate its contents.
        from copy import deepcopy

        self.receive(deepcopy(message.xml))

    async def wait_closed(self) -> None:
        await self.closed.wait()

    def send(self, payload: Mapping[str, str]) -> str:
        if (
            self.closed.is_set()
            or not self.ready.done()
            or self.ready.cancelled()
            or self.ready.exception() is not None
        ):
            raise AdapterSendError("XMPP session is not ready")
        message = self.client.make_message(
            mto=payload["to"], mbody=payload["body"], mtype=payload["type"]
        )
        native_id = uuid.uuid4().hex
        message["id"] = native_id
        # send_message()/send() enqueue locally and cannot prove handoff. This
        # provider registers no stream-management, encryption or outgoing stanza
        # filters: serialize with Slixmpp and cross its public send_raw boundary
        # synchronously. No queue acceptance is promoted to remote confirmation.
        try:
            self.client.send_raw(str(message))
        except Exception:
            raise AdapterSendError("XMPP transport write failed") from None
        return native_id

    async def close(self) -> None:
        self.client.cancel_connection_attempt()
        self.client.plugin["xep_0199"].disable_keepalive()
        for name in tuple(self.client.scheduled_events):
            self.client.cancel_schedule(name)
        for task in tuple(self._handlers):
            task.cancel()
        await asyncio.gather(*self._handlers, return_exceptions=True)
        await self.client.disconnect(wait=0)
        # Slixmpp 1.17 leaves its outgoing filter worker waiting on a queue after
        # disconnect. Its pinned contract is isolated here and covered by SDK
        # shutdown tests so a stopped provider retains no background task.
        worker = self.client._run_out_filters
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        if self._connect_task is not None:
            await asyncio.gather(self._connect_task, return_exceptions=True)
        if not self.ready.done():
            self.ready.cancel()
        elif not self.ready.cancelled():
            self.ready.exception()
        self.closed.set()
