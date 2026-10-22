"""Plain-text compatibility rendering using MEDRE attribution and fallbacks."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from medre.adapters.metadata import project_source_fields
from medre.core.events.canonical import CanonicalEvent
from medre.core.rendering.attribution import (
    build_relay_attribution,
    format_relay_prefix,
)
from medre.core.rendering.relations import degrade_relations_inline
from medre.core.rendering.renderer import RenderingContext, RenderingResult

from .config import XMPPConfig, plain_text


class XMPPRenderer:
    name = "xmpp"

    def __init__(
        self,
        configs: Mapping[str, XMPPConfig],
        source_attribution: Mapping[str, Any] | None = None,
    ) -> None:
        self.configs = dict(configs)
        self.source_attribution = dict(source_attribution or {})

    def can_render(self, event: CanonicalEvent, ctx: RenderingContext) -> bool:
        return (
            ctx.target_platform == "xmpp"
            and ctx.target_adapter in self.configs
            and ctx.delivery_strategy != "skip"
            and (
                event.event_kind in {"message.created", "message.text"}
                or (ctx.delivery_strategy == "fallback_text" and bool(event.relations))
            )
        )

    async def render(
        self, event: CanonicalEvent, ctx: RenderingContext
    ) -> RenderingResult:
        if not self.can_render(event, ctx):
            raise ValueError("Unsupported XMPP rendering target or strategy")
        config = self.configs[ctx.target_adapter]
        if (
            ctx.target_destination is not None
            and ctx.target_destination.kind != "channel"
        ):
            raise ValueError("XMPP rendering requires a configured bare JID channel")
        message_type = config.message_type(ctx.target_channel)
        text = event.payload.get("body", event.payload.get("text", ""))
        if not isinstance(text, str):
            raise ValueError("XMPP message body must be text")
        reply_relations = [
            rel for rel in event.relations if rel.relation_type == "reply"
        ]
        reply_id = None
        if ctx.delivery_strategy == "direct" and len(reply_relations) == 1:
            ref = reply_relations[0].target_native_ref
            if (
                ref is not None
                and ref.adapter == ctx.target_adapter
                and ref.native_channel_id == ctx.target_channel
                and isinstance(ref.native_message_id, str)
                and ref.native_message_id.strip()
                and plain_text(ref.native_message_id)
                and len(ref.native_message_id.encode("utf-8")) <= 1024
            ):
                reply_id = ref.native_message_id
        fallback = ctx.delivery_strategy == "fallback_text" or (
            bool(reply_relations) and reply_id is None
        )
        if fallback:
            text = degrade_relations_inline(event, text)
        if not text.strip():
            raise ValueError("XMPP message body must not be empty")
        attr_config = self.source_attribution.get(event.source_adapter)
        label = ctx.source_origin_label
        if label is None and attr_config is not None:
            label = attr_config.origin_label
        native = dict(event.metadata.native.data) if event.metadata.native else {}
        projected = project_source_fields(
            native,
            source_adapter=event.source_adapter,
            source_transport_id=event.source_transport_id,
            platform_hint=getattr(attr_config, "platform", None),
        )
        attr = build_relay_attribution(
            event, source_origin_label=label, projected_fields=projected
        )
        prefix = format_relay_prefix(config.relay_prefix, attr)
        if prefix.formatting_error:
            raise ValueError("Invalid XMPP relay prefix template")
        text = prefix.rendered_prefix + text
        if not text.strip() or not plain_text(text):
            raise ValueError("XMPP message requires non-empty XML text")
        # Fail rather than silently cut an identity prefix or message content.
        limits = [config.max_text_bytes]
        if ctx.max_text_bytes is not None:
            limits.append(ctx.max_text_bytes)
        if len(text.encode("utf-8")) > min(limits) or (
            ctx.max_text_chars is not None and len(text) > ctx.max_text_chars
        ):
            raise ValueError("Rendered XMPP text exceeds the destination budget")
        payload: dict[str, object] = {
            "body": text,
            "to": ctx.target_channel,
            "type": message_type,
        }
        if reply_id is not None:
            payload["reply_id"] = reply_id
            original = reply_relations[0].fallback_text
            quote = (
                "\n".join("> " + line for line in original.splitlines()) + "\n"
                if original
                else "> Reply\n"
            )
            quoted = quote + text
            if len(quoted.encode("utf-8")) <= min(limits) and (
                ctx.max_text_chars is None or len(quoted) <= ctx.max_text_chars
            ):
                payload["body"] = quoted
                payload["reply_fallback"] = quote
        return RenderingResult(
            event_id=event.event_id,
            target_adapter=ctx.target_adapter,
            target_channel=ctx.target_channel,
            payload=payload,
            metadata={"xmpp": {"format": "plain", "attribution": "relay"}},
            fallback_applied=(
                (
                    "strategy_fallback_text"
                    if ctx.delivery_strategy == "fallback_text"
                    else "relation_reply"
                )
                if fallback
                else None
            ),
        )


def build_renderer(
    *,
    runtime_configs: Mapping[str, Any],
    all_runtime_configs: Mapping[str, Any],
    source_attribution: Mapping[str, Any],
) -> XMPPRenderer | None:
    del all_runtime_configs
    configs = {
        rtc.adapter_id: rtc.config
        for rtc in runtime_configs.values()
        if rtc.enabled and rtc.config is not None
    }
    return XMPPRenderer(configs, source_attribution) if configs else None
