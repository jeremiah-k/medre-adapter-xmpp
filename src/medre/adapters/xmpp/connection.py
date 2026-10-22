"""Pinned Slixmpp binding; protocol negotiation and XML encoding stay in the SDK."""

from __future__ import annotations

import asyncio
import ssl
import uuid
from collections.abc import Callable, Mapping
from typing import Any, Literal, cast
from xml.etree.ElementTree import SubElement
from .config import XMPPConfig
from .codec import InboundStanza, CLIENT, room_stanza_id

from medre.core.contracts.adapter import AdapterPermanentError, AdapterSendError

MUC_ECHO_TIMEOUT_SECONDS = 5.0
MAX_PENDING_ECHOES = 128


class SlixConnection:
    def __init__(
        self, config: XMPPConfig, receive: Callable[[InboundStanza], None]
    ) -> None:
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
        self.client.plugin["xep_0030"].add_feature("urn:xmpp:reply:0")
        self.ready = asyncio.get_running_loop().create_future()
        self.closed = asyncio.Event()
        self._handlers: set[asyncio.Task[None]] = set()
        self._closing = False
        # Slixmpp schedules XEP-0115 discovery without retaining its tasks.
        # Own that callback just like session setup so disconnect can reap it.
        caps = self.client.plugin["xep_0115"]
        self.client.del_event_handler("entity_caps", caps._process_caps)
        self.client.add_event_handler("entity_caps", self._entity_caps)
        self._connect_task: Any | None = None
        self.trusted_rooms: frozenset[str] = frozenset()
        self._echoes: dict[tuple[str, str], tuple[asyncio.Future[str | None], str]] = {}
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

    def _connect_done(self, future: asyncio.Future[Any]) -> None:
        if (
            not future.cancelled()
            and future.exception() is not None
            and not self.ready.done()
        ):
            self.ready.set_exception(AdapterSendError("XMPP connection failed"))

    async def _entity_caps(self, presence: Any) -> None:
        if self._closing:
            return
        task = asyncio.current_task()
        if task is not None:
            self._handlers.add(task)
        try:
            await self.client.plugin["xep_0115"]._process_caps(presence)
        finally:
            if task is not None:
                self._handlers.discard(task)

    async def _session_started(self, _event: Any) -> None:
        if self._closing:
            return
        from slixmpp import JID
        from slixmpp.exceptions import IqError, IqTimeout

        task = asyncio.current_task()
        if task is not None:
            self._handlers.add(task)
        try:
            self.client.send_presence()
            for room in self.config.rooms:
                result = await self.client.plugin["xep_0045"].join_muc_wait(
                    JID(room),
                    self.config.nickname,
                    maxstanzas=0,
                    timeout=self.config.connect_timeout_seconds,
                )
                # A nick conflict or server rewrite changes echo ownership.
                if str(result[0]["from"]) != f"{room}/{self.config.nickname}":
                    raise AdapterPermanentError(
                        "XMPP room joined under an unexpected occupant identity"
                    )
                # Only the assigning room can attest to its stanza-ID rules.
                # Servers without discovery support still carry ordinary text.
                try:
                    info = await self.client.plugin["xep_0030"].get_info(
                        jid=JID(room),
                        timeout=self.config.connect_timeout_seconds,
                    )
                except (TimeoutError, IqError, IqTimeout):
                    continue
                if "urn:xmpp:sid:0" in info["disco_info"]["features"]:
                    self.trusted_rooms = self.trusted_rooms | {room}
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
            if task is not None:
                self._handlers.discard(task)

    def _failed_auth(self, _event: Any) -> None:
        if not self.ready.done():
            self.ready.set_exception(
                AdapterPermanentError("XMPP authentication failed")
            )

    def _release_echoes(self) -> None:
        for future, _body in self._echoes.values():
            if not future.done():
                future.set_result(None)

    def _disconnected(self, _event: Any) -> None:
        self._release_echoes()
        self.closed.set()
        if not self.ready.done():
            self.ready.set_exception(
                AdapterSendError("XMPP disconnected before readiness")
            )

    def _own_presence(self, presence: Any) -> None:
        if presence["type"] in {"unavailable", "error"}:
            self._release_echoes()
            self.closed.set()

    def _message(self, message: Any) -> None:
        # The SDK owns this Element for callback lifetime. Copy into the bounded
        # ingress queue so subsequent SDK processing cannot mutate its contents.
        from copy import deepcopy

        stanza = deepcopy(message.xml)
        sender = stanza.get("from", "")
        room, _, nick = sender.partition("/")
        if (
            stanza.tag == f"{{{CLIENT}}}message"
            and stanza.get("type") == "groupchat"
            and room in self.trusted_rooms
            and nick == self.config.nickname
            and stanza.find("{urn:xmpp:delay}delay") is None
            and stanza.find("{jabber:x:delay}x") is None
        ):
            origins = stanza.findall("{urn:xmpp:sid:0}origin-id")
            if len(origins) == 1:
                pending = self._echoes.get((room, origins[0].get("id", "")))
                if pending is not None:
                    future, body = pending
                    bodies = stanza.findall(f"{{{CLIENT}}}body")
                    if (
                        not future.done()
                        and len(bodies) == 1
                        and bodies[0].text == body
                    ):
                        future.set_result(room_stanza_id(stanza, room))
        self.receive(InboundStanza(stanza, self.trusted_rooms))

    async def wait_closed(self) -> None:
        await self.closed.wait()

    async def send(self, payload: Mapping[str, str]) -> str | None:
        if (
            self.closed.is_set()
            or not self.ready.done()
            or self.ready.cancelled()
            or self.ready.exception() is not None
        ):
            raise AdapterSendError("XMPP session is not ready")
        message_type = payload["type"]
        if message_type not in {"chat", "groupchat"}:
            raise AdapterSendError("Unsupported XMPP message type")
        message = self.client.make_message(
            mto=payload["to"],
            mbody=payload["body"],
            mtype=cast(Literal["chat", "groupchat"], message_type),
        )
        native_id = uuid.uuid4().hex
        message["id"] = native_id
        SubElement(message.xml, "{urn:xmpp:sid:0}origin-id", {"id": native_id})
        if "reply_id" in payload:
            SubElement(
                message.xml, "{urn:xmpp:reply:0}reply", {"id": payload["reply_id"]}
            )
            quote = payload.get("reply_fallback")
            if quote:
                fallback = SubElement(
                    message.xml,
                    "{urn:xmpp:fallback:0}fallback",
                    {"for": "urn:xmpp:reply:0"},
                )
                SubElement(
                    fallback,
                    "{urn:xmpp:fallback:0}body",
                    {"start": "0", "end": str(len(quote))},
                )
        room = payload["to"]
        future: asyncio.Future[str | None] | None = None
        if (
            message_type == "groupchat"
            and room in self.trusted_rooms
            and len(self._echoes) < MAX_PENDING_ECHOES
        ):
            future = asyncio.get_running_loop().create_future()
            self._echoes[(room, native_id)] = (future, payload["body"])
        # send_message()/send() enqueue locally and cannot prove handoff. This
        # provider registers no stream-management, encryption or outgoing stanza
        # filters: serialize with Slixmpp and cross its public send_raw boundary
        # synchronously. No queue acceptance is promoted to remote confirmation.
        try:
            try:
                self.client.send_raw(str(message))
            except Exception:
                raise AdapterSendError("XMPP transport write failed") from None
            if message_type != "groupchat":
                return native_id
            if future is None:
                return None
            try:
                async with asyncio.timeout(MUC_ECHO_TIMEOUT_SECONDS):
                    return await future
            except TimeoutError:
                # The transport write happened. Missing correlation alone must
                # not invent a failed send and trigger a duplicate retry.
                return None
        finally:
            self._echoes.pop((room, native_id), None)
            if future is not None and not future.done():
                future.cancel()

    async def close(self) -> None:
        self._closing = True
        self._release_echoes()
        self.client.del_event_handler("entity_caps", self._entity_caps)
        self.client.del_event_handler("session_start", self._session_started)
        self.client.cancel_connection_attempt()
        # The pinned SDK omits the return annotation on this public method.
        disable_keepalive = cast(
            Callable[[], None], self.client.plugin["xep_0199"].disable_keepalive
        )
        disable_keepalive()
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
