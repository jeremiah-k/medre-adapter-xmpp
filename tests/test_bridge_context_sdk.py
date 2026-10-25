"""Pinned Slixmpp serialization carries the renderer's original-source claims."""

from unittest.mock import Mock
from xml.etree.ElementTree import fromstring

import pytest

from medre.adapters.xmpp.connection import SlixConnection
from medre.adapters.xmpp.codec import XMPPCodec
from medre.adapters.xmpp.renderer import XMPPRenderer
from medre.core.rendering.renderer import RenderingContext
from conftest import NOW, stanza

pytestmark = pytest.mark.xmpp_sdk


async def test_native_socket_write_contains_original_source_context(config):
    """Assert actual wire XML independently of the production context decoder."""
    original = XMPPCodec(config).decode(stanza(), timestamp=NOW)
    result = await XMPPRenderer({config.adapter_id: config}).render(
        original, RenderingContext("direct", config.adapter_id, config.peers[0], "xmpp")
    )
    connection = SlixConnection(config, lambda _: None)
    connection.client.transport = Mock()
    connection.ready.set_result(None)
    try:
        native_id = await connection.send(result.payload)
        written = connection.client.transport.write.call_args.args[0]
        wire = fromstring(b"<stream xmlns='jabber:client'>" + written + b"</stream>")[0]
        sources = wire.findall(
            "{urn:medre:bridge-context:1}context/{urn:medre:bridge-context:1}source"
        )
        assert len(sources) == 1
        assert sources[0].get("sender_name") == "alice"
        assert sources[0].get("canonical_event_id") == original.event_id
        assert sources[0].get("sender_id") == original.source_transport_id
        assert wire.get("id") == native_id
        assert wire.find("{jabber:client}body").text == result.payload["body"]
        assert connection.client.waiting_queue.empty()
    finally:
        connection.client.transport = None
        await connection.close()
