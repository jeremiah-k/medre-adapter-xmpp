from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from medre.core.contracts.adapter import AdapterPermanentError, AdapterSendError
from medre.core.planning.capabilities import resolve_adapter_capabilities
from medre.core.rendering.renderer import RenderingContext
from medre.core.routing.models import RouteTarget
from medre.adapters.xmpp.adapter import XMPPAdapter, CAPABILITIES
from medre.adapters.xmpp.codec import InboundStanza, XMPPCodec
from medre.adapters.xmpp.fake import FakeXMPPAdapter
from medre.adapters.xmpp.renderer import XMPPRenderer
from conftest import NOW, rendered, stanza


def render_context(**updates):
    return replace(
        RenderingContext("direct", "xmpp-test", "room@conference.example.org", "xmpp"),
        **updates,
    )


async def test_renderer_retains_plain_text_and_exact_destination(config):
    event = XMPPCodec(config).decode(stanza(), timestamp=NOW)
    result = await XMPPRenderer({config.adapter_id: config}).render(
        event, render_context()
    )
    assert result.payload == {
        "body": "Hello <XMPP> & friends",
        "to": config.rooms[0],
        "type": "groupchat",
    }
    direct = await XMPPRenderer({config.adapter_id: config}).render(
        event, render_context(target_channel=config.peers[0])
    )
    assert direct.payload["type"] == "chat"
    assert event.payload["body"] == "Hello <XMPP> & friends"


async def test_attribution_uses_shared_projection_and_explicit_empty_labels(config):

    event = XMPPCodec(config).decode(stanza(), timestamp=NOW)
    config = replace(config, relay_prefix="[{origin_label}] {sender}: ")
    renderer = XMPPRenderer(
        {config.adapter_id: config},
        {config.adapter_id: SimpleNamespace(platform="xmpp", origin_label="Origin")},
    )
    a = await renderer.render(event, render_context())
    b = await renderer.render(event, render_context(source_origin_label=""))
    assert a.payload["body"].startswith("[Origin] alice: ")
    assert b.payload["body"].startswith("[] alice: ")


@pytest.mark.parametrize(
    "updates",
    [
        {"max_text_bytes": 3},
        {"max_text_chars": 2},
        {"target_channel": "stranger@example.org"},
        {"delivery_strategy": "skip"},
    ],
)
async def test_renderer_refuses_unsafe_or_unrepresentable_output(config, updates):
    event = XMPPCodec(config).decode(stanza(), timestamp=NOW)
    with pytest.raises(ValueError):
        await XMPPRenderer({config.adapter_id: config}).render(
            event, render_context(**updates)
        )


async def test_adapter_only_transports_rendered_payload(config, context):
    ctx, events = context
    session = SimpleNamespace(
        start=AsyncMock(),
        stop=AsyncMock(),
        send=AsyncMock(return_value="wire-id"),
        state="healthy",
    )
    adapter = XMPPAdapter(config, session_factory=lambda *_: session)
    await adapter.start(ctx)
    result = rendered()
    handoff = await adapter.deliver(result)
    session.send.assert_awaited_once_with(result.payload)
    assert handoff.confirmation_level == "local_transport"
    assert handoff.native_message_id == "wire-id"
    await adapter._receive(InboundStanza(stanza()))
    assert len(events) == 1
    assert (await adapter.health_check()).health == "healthy"
    await adapter.stop()
    session.stop.assert_awaited_once()
    assert (await adapter.health_check()).health == "unknown"


async def test_stopped_adapter_and_wrong_addresses_fail_before_send(config):
    adapter = XMPPAdapter(config)
    with pytest.raises(AdapterSendError):
        await adapter.deliver(rendered())
    with pytest.raises(AdapterPermanentError):
        await adapter.deliver(rendered(to="stranger@example.org"))
    wrong = rendered()
    wrong.payload["to"] = "someone-else@example.org"
    with pytest.raises(AdapterPermanentError):
        await adapter.deliver(wrong)


async def test_startup_failure_cleans_up_the_session(config, context):
    session = SimpleNamespace(
        start=AsyncMock(side_effect=AdapterSendError("offline")), stop=AsyncMock()
    )
    adapter = XMPPAdapter(config, session_factory=lambda *_: session)
    with pytest.raises(AdapterSendError):
        await adapter.start(context[0])
    session.stop.assert_awaited_once()
    assert adapter.session is None


def test_capabilities_do_not_claim_unimplemented_protocol_features():
    assert CAPABILITIES.replies == "fallback"
    assert (
        CAPABILITIES.edits
        == CAPABILITIES.deletes
        == CAPABILITIES.reactions
        == "unsupported"
    )
    assert not CAPABILITIES.attachments
    assert not CAPABILITIES.identity_encryption
    assert not CAPABILITIES.presence


async def test_declared_capabilities_feed_planning_resolution(config):
    real = XMPPAdapter(config)
    fake = FakeXMPPAdapter(config.adapter_id, config)
    assert real._capabilities == replace(
        CAPABILITIES, max_text_bytes=config.max_text_bytes
    )
    assert fake._capabilities == real._capabilities
    health = await fake.health_check()
    resolved = resolve_adapter_capabilities(
        {config.adapter_id: fake}, RouteTarget(adapter=config.adapter_id)
    )
    assert resolved == health.capabilities


async def test_configured_budget_is_shared_by_planning_and_health(config):
    limited = replace(config, max_text_bytes=257)
    for adapter in (XMPPAdapter(limited), FakeXMPPAdapter(limited.adapter_id, limited)):
        resolved = resolve_adapter_capabilities(
            {limited.adapter_id: adapter}, RouteTarget(adapter=limited.adapter_id)
        )
        assert resolved.max_text_bytes == 257
        assert resolved == (await adapter.health_check()).capabilities
