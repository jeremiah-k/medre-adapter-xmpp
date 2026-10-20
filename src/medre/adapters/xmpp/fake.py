"""SDK-free pipeline fake with the real codec and native payload validation."""

from datetime import datetime, timezone
from uuid import uuid4
from xml.etree.ElementTree import Element, SubElement

from medre.core.contracts.adapter import (
    AdapterContext,
    AdapterHandoffResult,
    AdapterInfo,
    AdapterSendError,
)
from medre.core.events.canonical import CanonicalEvent
from medre.core.rendering.renderer import RenderingResult

from .adapter import XMPPAdapter, validate_payload
from .codec import CLIENT
from .config import XMPPConfig


class FakeXMPPAdapter(XMPPAdapter):
    def __init__(self, adapter_id: str, config: XMPPConfig | None = None) -> None:
        super().__init__(
            config
            or XMPPConfig(
                adapter_id,
                jid="medre@example.org/bridge",
                rooms=("bridge@conference.example.org",),
            )
        )
        self.delivered_payloads: list[RenderingResult] = []
        self._started = False

    async def start(self, ctx: AdapterContext) -> None:
        self.ctx = ctx
        self._mark_started(ctx)
        self._started = True

    async def stop(self, timeout: float = 5.0) -> None:
        self._started = False

    async def deliver(self, result: RenderingResult) -> AdapterHandoffResult:
        # Core constructs fake providers by instance ID; explicit route channels
        # can differ from the sample address. Preserve validation of body/type
        # while permitting configured conversations in this pipeline-only fake.
        from dataclasses import replace

        config = self.config
        target_channel = result.target_channel
        if target_channel is not None and target_channel not in (
            config.rooms + config.peers
        ):
            if (
                isinstance(result.payload, dict)
                and result.payload.get("type") == "chat"
            ):
                config = replace(config, peers=(*config.peers, target_channel))
            else:
                config = replace(config, rooms=(*config.rooms, target_channel))
        validate_payload(result, config)
        if not self._started:
            raise AdapterSendError("XMPP fake is not started")
        self.delivered_payloads.append(result)
        if len(self.delivered_payloads) > 1000:
            del self.delivered_payloads[:-1000]
        return AdapterHandoffResult(
            native_message_id=f"fake-xmpp-{uuid4().hex}",
            native_channel_id=result.target_channel,
            note="SDK-free fake; no native delivery",
        )

    def diagnostics(self) -> dict[str, object]:
        return {
            "fake": True,
            "delivered": len(self.delivered_payloads),
            "started": self._started,
        }

    async def health_check(self) -> AdapterInfo:
        return AdapterInfo(
            self.adapter_id,
            self.platform,
            self.role,
            "0.1.0",
            self._capabilities,
            health="healthy" if self._started else "unknown",
        )

    def make_event(
        self,
        *,
        text: str,
        event_kind: str = "message.created",
        channel: str | None = None,
    ) -> CanonicalEvent:
        from dataclasses import replace
        import msgspec
        from .codec import XMPPCodec

        if event_kind not in {"message.created", "message.text"}:
            raise ValueError("XMPP fake source supports text messages")
        room = channel or self.config.rooms[0]
        config = replace(self.config, rooms=(*self.config.rooms, room))
        stanza = Element(
            f"{{{CLIENT}}}message",
            {"type": "groupchat", "from": f"{room}/alice", "id": uuid4().hex},
        )
        SubElement(stanza, f"{{{CLIENT}}}body").text = text
        event = XMPPCodec(config).decode(
            stanza,
            timestamp=self.ctx.clock() if self.ctx else datetime.now(timezone.utc),
        )
        if event is None:
            raise ValueError("Invalid fake XMPP message")
        return msgspec.structs.replace(event, event_kind=event_kind)
