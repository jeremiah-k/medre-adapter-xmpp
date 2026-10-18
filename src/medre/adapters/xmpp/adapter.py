"""Lifecycle and delivery through one session, without transport presentation."""

from __future__ import annotations

from collections.abc import Mapping, Callable
from typing import Any
from xml.etree.ElementTree import Element
from dataclasses import replace

from medre.core.contracts.adapter import (
    AdapterCapabilities,
    AdapterContext,
    AdapterContract,
    AdapterHandoffResult,
    AdapterInfo,
    AdapterPermanentError,
    AdapterRole,
    AdapterSendError,
)
from medre.core.rendering.renderer import RenderingResult

from .codec import XMPPCodec
from .config import XMPPConfig, plain_text

CAPABILITIES = AdapterCapabilities(
    text=True,
    title=False,
    replies="fallback",
    threads="fallback",
    reactions="unsupported",
    edits="unsupported",
    deletes="unsupported",
    attachments=False,
    metadata_fields=False,
    store_and_forward=False,
    direct_messages=True,
    channels=True,
    identity_encryption=False,
    presence=False,
    topic_rooms=True,
    mesh_routing=False,
    priority_delivery=False,
)


def validate_payload(result: RenderingResult, config: XMPPConfig) -> Mapping:
    """Refuse misaddressed or malformed output before touching a socket."""
    payload = result.payload
    if result.target_adapter != config.adapter_id or not isinstance(payload, Mapping):
        raise AdapterPermanentError("Invalid XMPP delivery target or payload")
    try:
        message_type = config.message_type(result.target_channel)
    except ValueError:
        raise AdapterPermanentError("Unconfigured XMPP destination") from None
    body = payload.get("body")
    if (
        payload.get("to") != result.target_channel
        or payload.get("type") != message_type
        or not plain_text(body)
        or not body.strip()
        or len(body.encode("utf-8")) > config.max_text_bytes
    ):
        raise AdapterPermanentError("Invalid rendered XMPP addressing or body")
    return payload


class XMPPAdapter(AdapterContract):
    platform = "xmpp"
    role = AdapterRole.HYBRID

    def __init__(
        self, config: XMPPConfig, *, session_factory: Callable[..., Any] | None = None
    ) -> None:
        super().__init__()
        self.config = config.validate()
        self.adapter_id = config.adapter_id
        self.codec = XMPPCodec(config)
        self.ctx: AdapterContext | None = None
        self.session = None
        self._session_factory = session_factory

    async def start(self, ctx: AdapterContext) -> None:
        if self.session is not None:
            raise RuntimeError("XMPP adapter is started")
        from .session import XMPPSession

        self.ctx = ctx
        self._mark_started(ctx)
        self.session = (self._session_factory or XMPPSession)(
            self.config, self._receive
        )
        try:
            await self.session.start()
        except BaseException:
            await self.session.stop()
            self.session = None
            raise

    async def _receive(self, stanza: Element) -> None:
        event = self.codec.decode(stanza, timestamp=self.ctx.clock())
        if event is not None:
            await self.publish_inbound(event)

    async def stop(self, timeout: float = 5.0) -> None:
        if self.session is not None:
            session, self.session = self.session, None
            await session.stop(timeout=timeout)

    async def deliver(self, result: RenderingResult) -> AdapterHandoffResult:
        payload = validate_payload(result, self.config)
        if self.session is None:
            raise AdapterSendError("XMPP adapter is not started")
        native_id = await self.session.send(payload)
        return AdapterHandoffResult(
            disposition="transport_handoff",
            confirmation_level="local_transport",
            native_message_id=native_id,
            native_channel_id=result.target_channel,
            note="XML written to the local TLS transport; recipient delivery is unconfirmed",
        )

    def diagnostics(self) -> dict[str, object]:
        """Expose bounded-admission and recovery evidence without credentials."""
        session = self.session
        return {
            "state": session.state if session else "unknown",
            "dropped_inbound": session.dropped_inbound if session else 0,
            "failed_inbound": session.failed_inbound if session else 0,
            "reconnects": session.reconnects if session else 0,
        }

    async def health_check(self) -> AdapterInfo:
        state = self.session.state if self.session else "unknown"
        return AdapterInfo(
            self.adapter_id,
            self.platform,
            self.role,
            "0.1.0",
            replace(CAPABILITIES, max_text_bytes=self.config.max_text_bytes),
            health=state,
        )
