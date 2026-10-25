"""Native stanza attribution preserves source claims without replacing XMPP authority."""

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
from xml.etree.ElementTree import fromstring, tostring

import msgspec
import pytest
from jsonschema import Draft202012Validator

from medre.adapters.xmpp.adapter import validate_payload
from medre.adapters.xmpp.bridge_context import (
    NAMESPACE,
    append_context,
    decode_context,
    encode_context,
    extract_context,
)
from medre.adapters.xmpp.codec import XMPPCodec
from medre.adapters.xmpp.renderer import XMPPRenderer
from medre.core.contracts.adapter import AdapterPermanentError
from medre.core.rendering.attribution import RelayAttribution
from medre.core.rendering.bridge_context import build_bridge_context
from medre.core.rendering.renderer import RenderingContext
from conftest import NOW, rendered, stanza


def context(config, **updates):
    """Use a configured MUC destination with explicit delivery strategy overrides."""
    return replace(
        RenderingContext("direct", config.adapter_id, config.rooms[0], "xmpp"),
        **updates,
    )


def claim(config, **attrs):
    """Build typed source claims from actual codec ingress instead of rendered output."""
    source = XMPPCodec(config).decode(stanza(), timestamp=NOW)
    assert source is not None
    return build_bridge_context(source, RelayAttribution(**attrs))


async def test_renderer_to_xml_and_receiving_codec_retains_claims(config):
    """Preserve original names and references while keeping the receiving native identity."""
    original = XMPPCodec(config).decode(stanza(), timestamp=NOW)
    renderer = XMPPRenderer(
        {config.adapter_id: config},
        {
            config.adapter_id: SimpleNamespace(
                platform="xmpp", origin_label="East <&> 🦉"
            ),
        },
    )
    result = await renderer.render(original, context(config))
    validate_payload(result, config)
    wire = stanza(kind="chat", sender=config.peers[0] + "/phone", id="actual-native")
    append_context(wire, result.payload["bridge_context"])
    received = XMPPCodec(config).decode(fromstring(tostring(wire)), timestamp=NOW)
    source = received.metadata.native.data["xmpp"]["relay"]["bridge_context"]["source"]
    assert source["sender_name"] == "alice"
    assert source["origin_label"] == "East <&> 🦉"
    assert source["sender_verification"] == "unknown"
    assert source["canonical_event_id"] == original.event_id
    assert received.source_transport_id == config.peers[0]
    assert received.source_native_ref.native_message_id == "actual-native"
    assert received.event_id != source["canonical_event_id"]
    assert not received.relations
    schema = json.loads(
        (Path(__file__).parents[1] / "docs/schemas/native.schema.json").read_text()
    )
    Draft202012Validator(schema).validate(
        msgspec.json.decode(msgspec.json.encode(received.metadata.native.data["xmpp"]))
    )


def test_xml_preserves_explicit_empty_names_and_removes_private_fields(config):
    """Empty claims stay empty; unrecognized credentials never become native context."""
    value = claim(config, source_sender_label_raw="", source_origin_label="")
    value["source"]["password"] = "PRIVATE_SENTINEL"
    wire = stanza()
    append_context(wire, encode_context(value))
    actual = extract_context(fromstring(tostring(wire)))
    assert actual["source"]["sender_name"] == ""
    assert actual["source"]["origin_label"] == ""
    assert "PRIVATE_SENTINEL" not in tostring(wire).decode()


@pytest.mark.parametrize("mode", ["duplicate", "version", "source", "ref"])
def test_malformed_extension_does_not_discard_the_native_message(config, mode):
    """Reject ambiguous or malformed optional claims while retaining ordinary text."""
    wire = stanza()
    append_context(wire, encode_context(claim(config)))
    root = wire.find(f"{{{NAMESPACE}}}context")
    if mode == "duplicate":
        wire.append(deepcopy(root))
    elif mode == "version":
        root.set("schema_version", "2")
    elif mode == "source":
        root.find(f"{{{NAMESPACE}}}source").set("adapter", "")
    else:
        source = root.find(f"{{{NAMESPACE}}}source")
        source.append(
            fromstring(f'<native_ref xmlns="{NAMESPACE}" adapter="unscoped"/>')
        )
    received = XMPPCodec(config).decode(wire, timestamp=NOW)
    assert received.payload["body"] == "Hello <XMPP> & friends"
    assert "relay" not in received.metadata.native.data["xmpp"]


def test_remote_relation_context_does_not_bind_a_native_reply(config):
    """Only native reply extensions can create relations; relay hints remain metadata."""
    value = claim(config)
    value["relations"] = [
        {
            "relation_type": "reply",
            "target_event_id": "remote-target",
            "target_native_ref": None,
            "key": None,
        }
    ]
    wire = stanza()
    append_context(wire, encode_context(value))
    received = XMPPCodec(config).decode(wire, timestamp=NOW)
    assert received.metadata.native.data["xmpp"]["relay"]["bridge_context"]["relations"]
    assert not received.relations


@pytest.mark.parametrize(
    "name", ["bad\x00name", "&" * 1024], ids=["illegal-xml", "xml-byte-budget"]
)
def test_unrepresentable_context_is_omitted(config, name):
    """Reject illegal XML text and account for escaping in the context byte budget."""
    value = claim(
        config,
        source_sender_label=name,
        source_sender_short_label=name,
        source_origin_label=name,
        source_sender_verification=name,
    )
    assert encode_context(value) is None


async def test_metadata_switch_preserves_visible_message(config):
    """Disabling structured embedding leaves the prefix and body rendering usable."""
    source = XMPPCodec(config).decode(stanza(), timestamp=NOW)
    result = await XMPPRenderer(
        {config.adapter_id: replace(config, metadata_embedding=False)}
    ).render(source, context(config))
    assert "bridge_context" not in result.payload
    assert result.payload["body"] == source.payload["body"]


@pytest.mark.parametrize("value", [None, True, "not JSON", "{}", "[" * 20000])
def test_invalid_saved_context_is_rejected_before_send(config, value):
    """Malformed durable metadata cannot pass adapter validation to a socket."""
    result = rendered()
    result.payload["bridge_context"] = value
    assert decode_context(value) is None
    with pytest.raises(AdapterPermanentError, match="relay context"):
        validate_payload(result, config)
