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


@pytest.mark.parametrize("portnum", [1, 1.0])
def test_xml_roundtrips_approved_source_native_facts(config, portnum):
    """Public source-owned facts share the core envelope without leaking secrets."""
    value = claim(config, source_sender_label="Alice")
    value["source"]["native_facts"] = {
        "namespace": "meshtastic",
        "fields": {"portnum": portnum, "meshnet_label": "East <&>"},
    }
    wire = stanza()
    append_context(wire, encode_context(value))
    received = extract_context(fromstring(tostring(wire)))
    assert received == value
    assert type(received["source"]["native_facts"]["fields"]["portnum"]) is int
    assert b"native_facts" in tostring(wire)


@pytest.mark.parametrize(
    "invalid",
    [
        {"namespace": "meshtastic", "fields": {"credential": {"secret": "x"}}},
        {"namespace": "meshtastic", "fields": {"portnum": 2**50}},
        {"namespace": "meshtastic", "fields": {"portnum": "x" * 257}},
        {"namespace": "../bad", "fields": {"portnum": 1}},
    ],
)
def test_xml_rejects_invalid_public_source_facts(config, invalid):
    """Untrusted XML does not promote unbounded or structured native packet data."""
    value = claim(config)
    value["source"]["native_facts"] = invalid
    assert encode_context(value) is None


def test_xml_rejects_duplicate_native_facts_nodes(config):
    """Conflicting peer claims must not be resolved by last-writer-wins."""
    value = claim(config)
    value["source"]["native_facts"] = {
        "namespace": "meshtastic",
        "fields": {"portnum": 1},
    }
    wire = stanza()
    append_context(wire, encode_context(value))
    source = wire.find(f"{{{NAMESPACE}}}context").find(f"{{{NAMESPACE}}}source")
    node = source.find(f"{{{NAMESPACE}}}native_facts")
    source.append(deepcopy(node))
    assert extract_context(wire) is None


def test_xml_rejects_duplicate_fact_keys_inside_json(config):
    value = claim(config)
    value["source"]["native_facts"] = {
        "namespace": "meshtastic",
        "fields": {"portnum": 1},
    }
    wire = stanza()
    append_context(wire, encode_context(value))
    node = (
        wire.find(f"{{{NAMESPACE}}}context")
        .find(f"{{{NAMESPACE}}}source")
        .find(f"{{{NAMESPACE}}}native_facts")
    )
    node.text = '{"portnum":1,"portnum":10}'
    assert extract_context(wire) is None


@pytest.mark.parametrize(
    "origin,expected", [(None, "Remote"), ("", ""), ("Route", "Route")]
)
async def test_embedding_attribution_reaches_xmpp_stanza(config, origin, expected):
    original = XMPPCodec(config).decode(stanza(), timestamp=NOW)
    result = await XMPPRenderer({config.adapter_id: config}).render(
        original,
        context(
            config,
            source_origin_label=origin,
            source_attribution=RelayAttribution(
                source_platform="meshtastic",
                source_sender_label="Alice",
                source_sender_short_label="AL",
                source_origin_label="Remote",
            ),
        ),
    )
    wire = stanza()
    append_context(wire, result.payload["bridge_context"])
    source = extract_context(wire)["source"]
    assert source["sender_name"] == "Alice"
    assert source["sender_short_name"] == "AL"
    assert source["origin_label"] == expected


@pytest.mark.parametrize(
    "text", ["East\nWest", "tabs\tand\rreturns", "A\x00B", "café 🦉"]
)
def test_json_native_facts_preserve_strings_that_are_not_xml_attributes(config, text):
    value = claim(config)
    value["source"]["native_facts"] = {"namespace": "public", "fields": {"label": text}}
    wire = stanza()
    append_context(wire, encode_context(value))
    assert extract_context(fromstring(tostring(wire))) == value


@pytest.mark.parametrize("text", ["\ud800", "\udfff"])
def test_unencodable_public_fact_is_omitted_without_losing_native_message(config, text):
    value = claim(config)
    value["source"]["native_facts"] = {"namespace": "public", "fields": {"label": text}}
    assert encode_context(value) is None

    # The XML itself is valid: the surrogate arrives escaped inside JSON text.
    safe = claim(config)
    safe["source"]["native_facts"] = {
        "namespace": "public",
        "fields": {"label": "safe"},
    }
    wire = stanza()
    append_context(wire, encode_context(safe))
    wire.find(
        f"{{{NAMESPACE}}}context/{{{NAMESPACE}}}source/{{{NAMESPACE}}}native_facts"
    ).text = json.dumps({"label": text})
    received = XMPPCodec(config).decode(fromstring(tostring(wire)), timestamp=NOW)
    assert received is not None
    assert received.payload["body"] == wire.find("{jabber:client}body").text
    assert "bridge_context" not in received.metadata.native.data["xmpp"].get(
        "relay", {}
    )
    msgspec.json.encode(received)


def test_context_sizing_never_imports_a_transport_sdk(config, monkeypatch):
    import builtins

    original_import = builtins.__import__

    def guard(name, *args, **kwargs):
        if name == "slixmpp" or name.startswith("slixmpp."):
            pytest.fail("Pure context sizing must not import Slixmpp")
        return original_import(name, *args, **kwargs)

    value = claim(config)
    monkeypatch.setattr(builtins, "__import__", guard)
    assert encode_context(value) is not None


@pytest.mark.parametrize("context_label", [None, "", "Route"])
@pytest.mark.parametrize("snapshot_label", [None, "", "Snapshot"])
async def test_supplied_attribution_preserves_registry_origin(
    config, context_label, snapshot_label
):
    """Preserve registry fallback and explicit empty labels in XML context."""
    original = XMPPCodec(config).decode(stanza(), timestamp=NOW)
    sender = XMPPRenderer(
        {config.adapter_id: config},
        {config.adapter_id: SimpleNamespace(platform="xmpp", origin_label="Registry")},
    )
    rendered = await sender.render(
        original,
        context(
            config,
            source_origin_label=context_label,
            source_attribution=RelayAttribution(
                source_sender_label="Alice", source_origin_label=snapshot_label
            ),
        ),
    )
    source = decode_context(rendered.payload["bridge_context"])["source"]
    expected = (
        context_label
        if context_label is not None
        else snapshot_label if snapshot_label is not None else "Registry"
    )
    assert source["origin_label"] == expected
    assert source["sender_name"] == "Alice"
