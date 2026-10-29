"""Lifecycle and delivery through one session, without transport presentation."""

from __future__ import annotations

from collections.abc import Mapping, Callable
from typing import TYPE_CHECKING, cast
from dataclasses import replace

from medre.core.supervision.diagnostic_contract import COMMON_DIAGNOSTIC_KEYS

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
from medre.core.contracts.typing import TypingActivity, TypingUpdate
from medre.core.rendering.renderer import RenderingResult

from .codec import InboundStanza, XMPPCodec
from .bridge_context import decode_context
from .config import XMPPConfig, plain_text

if TYPE_CHECKING:
    from .session import XMPPSession

CAPABILITIES = AdapterCapabilities(
    text=True,
    title=False,
    replies="native",
    threads="fallback",
    reactions="unsupported",
    edits="unsupported",
    deletes="unsupported",
    attachments=False,
    metadata_fields=True,
    store_and_forward=False,
    direct_messages=True,
    channels=True,
    identity_encryption=False,
    presence=False,
    typing=True,
    topic_rooms=True,
    mesh_routing=False,
    priority_delivery=False,
)


def validate_payload(result: RenderingResult, config: XMPPConfig) -> Mapping[str, str]:
    """Refuse misaddressed or malformed output before touching a socket."""
    payload = result.payload
    if result.target_adapter != config.adapter_id or not isinstance(payload, Mapping):
        raise AdapterPermanentError("Invalid XMPP delivery target or payload")
    try:
        message_type = config.message_type(result.target_channel)
    except ValueError:
        raise AdapterPermanentError("Unconfigured XMPP destination") from None
    if set(payload) - {
        "to",
        "type",
        "body",
        "reply_id",
        "reply_fallback",
        "unstyled",
        "bridge_context",
    }:
        raise AdapterPermanentError("Unknown rendered XMPP payload fields")
    if (
        "bridge_context" in payload
        and decode_context(payload["bridge_context"]) is None
    ):
        raise AdapterPermanentError("Invalid rendered XMPP relay context")
    if "unstyled" in payload and payload["unstyled"] != "true":
        raise AdapterPermanentError("Invalid XMPP styling control")
    reply = payload.get("reply_id")
    quote = payload.get("reply_fallback")
    if reply is not None and (
        not isinstance(reply, str)
        or not reply.strip()
        or not plain_text(reply)
        or len(reply.encode("utf-8")) > 1024
    ):
        raise AdapterPermanentError("Invalid rendered XMPP reply reference")
    if "reply_id" in payload and reply is None:
        raise AdapterPermanentError("Invalid rendered XMPP reply reference")
    body = payload.get("body")
    if "reply_fallback" in payload and (
        reply is None
        or not isinstance(quote, str)
        or not quote
        or not isinstance(body, str)
        or not body.startswith(quote)
        or len(quote) >= len(body)
    ):
        raise AdapterPermanentError("Invalid rendered XMPP reply fallback")
    if (
        payload.get("to") != result.target_channel
        or payload.get("type") != message_type
        or not isinstance(body, str)
        or not plain_text(body)
        or not body.strip()
        or len(body.encode("utf-8")) > config.max_text_bytes
    ):
        raise AdapterPermanentError("Invalid rendered XMPP addressing or body")
    # Validation boundary: to/type equality plus the str body check above
    # pin the three fields the transport consumes to text.
    return cast(Mapping[str, str], payload)


class XMPPAdapter(AdapterContract):
    platform = "xmpp"
    role = AdapterRole.HYBRID

    def __init__(
        self,
        config: XMPPConfig,
        *,
        session_factory: Callable[..., XMPPSession] | None = None,
    ) -> None:
        super().__init__()
        self.config = config.validate()
        self.adapter_id = config.adapter_id
        self._capabilities = replace(CAPABILITIES, max_text_bytes=config.max_text_bytes)
        self.codec = XMPPCodec(config)
        self.ctx: AdapterContext | None = None
        self.session: XMPPSession | None = None
        self._session_factory = session_factory
        self._health_epoch = 0
        self._health_active = False
        self._reported_health: str | None = None

    async def start(self, ctx: AdapterContext) -> None:
        if self.session is not None:
            raise RuntimeError("XMPP adapter is started")
        from .session import XMPPSession

        self._health_epoch += 1
        self._reported_health = None
        epoch = self._health_epoch
        self.ctx = ctx
        self._mark_started(ctx)
        options = (
            {"typing_receive": self._receive_typing}
            if ctx.publish_typing is not None
            else {}
        )
        factory: Callable[..., XMPPSession] = self._session_factory or XMPPSession
        self.session = factory(self.config, self._receive, **options)
        if isinstance(self.session, XMPPSession):
            self.session.set_health_callback(lambda: self._report_native_health(epoch))
        try:
            await self.session.start()
            self._health_active = True
            self._report_native_health(epoch)
        except BaseException:
            await self.session.stop()
            self.session = None
            raise

    async def _receive(self, packet: InboundStanza) -> None:
        ctx = self.ctx
        if ctx is None:
            return
        event = self.codec.decode(
            packet.stanza,
            timestamp=ctx.clock(),
            trusted_rooms=packet.trusted_rooms,
        )
        if event is not None:
            await self.publish_inbound(event)

    async def _receive_typing(self, activity: TypingActivity) -> None:
        """Publish live optional state through the source-bound transient sink."""
        if self.ctx is not None and self.ctx.publish_typing is not None:
            await self.ctx.publish_typing(activity)

    async def send_typing(self, update: TypingUpdate) -> None:
        """Use the session-owned native port without message rendering or receipts."""
        if self.session is None or update.target.adapter_id != self.adapter_id:
            raise AdapterSendError("XMPP typing session or adapter is not available")
        self.config.message_type(update.target.channel_id)
        await self.session.send_typing(update)

    async def stop(self, timeout: float = 5.0) -> None:
        self._health_active = False
        self._health_epoch += 1
        self._reported_health = None
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
            **dict.fromkeys(sorted(COMMON_DIAGNOSTIC_KEYS)),
            "health": session.state if session else "unknown",
            "mode": "live",
            "state": session.state if session else "unknown",
            "dropped_inbound": session.dropped_inbound if session else 0,
            "failed_inbound": session.failed_inbound if session else 0,
            "reconnects": session.reconnects if session else 0,
        }

    def _report_native_health(self, epoch: int) -> None:
        """Report this lifetime's native state; diagnostics cannot control reconnection."""
        if not self._health_active or epoch != self._health_epoch or self.ctx is None:
            return
        info = self._health_info()
        report = getattr(self.ctx, "report_health", None)
        if (
            info.health not in {"healthy", "degraded", "failed"}
            or report is None
            or self._reported_health == info.health
        ):
            return
        try:
            report(info)
            self._reported_health = info.health
        except Exception as exc:
            self.ctx.logger.warning(
                "XMPP health notification failed (%s)", type(exc).__name__
            )

    async def health_check(self) -> AdapterInfo:
        """Read authenticated connection and room readiness without native requests."""
        return self._health_info()

    def _health_info(self) -> AdapterInfo:
        """Share the native session observation between probes and notifications."""
        state = self.session.state if self.session else "unknown"
        return AdapterInfo(
            self.adapter_id,
            self.platform,
            self.role,
            "0.1.0",
            self._capabilities,
            health=state,
        )
