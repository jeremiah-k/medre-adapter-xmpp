from dataclasses import replace
from xml.etree.ElementTree import SubElement

import pytest

from medre_adapter_xmpp.codec import CLIENT, MUTATIONS, XMPPCodec
from conftest import NOW, stanza


def test_muc_identity_and_text_are_preserved(config):
    event = XMPPCodec(config).decode(stanza(), timestamp=NOW)
    assert event.source_transport_id == "room@conference.example.org/alice"
    assert event.source_channel_id == "room@conference.example.org"
    assert event.payload["body"] == "Hello <XMPP> & friends"
    assert (
        event.source_native_ref is None
    )  # MUC sender ID is not a room-issued reference.
    assert event.timestamp == NOW
    assert event.metadata.transport.substrate == "ip"
    assert event.metadata.native.data["xmpp"]["nickname"] == "alice"


def test_sender_namespaces_prevent_colliding_event_ids(config):
    codec = XMPPCodec(config)
    first = codec.decode(stanza(), timestamp=NOW)
    assert codec.decode(stanza(), timestamp=NOW).event_id == first.event_id
    assert (
        codec.decode(
            stanza(sender="room@conference.example.org/bob"), timestamp=NOW
        ).event_id
        != first.event_id
    )
    assert (
        XMPPCodec(replace(config, adapter_id="another"))
        .decode(stanza(), timestamp=NOW)
        .event_id
        != first.event_id
    )


def test_direct_conversation_survives_resource_changes(config):
    codec = XMPPCodec(config)
    a = codec.decode(
        stanza(sender="alice@example.org/phone", kind="chat"), timestamp=NOW
    )
    b = codec.decode(
        stanza(sender="alice@example.org/desktop", kind="chat"), timestamp=NOW
    )
    assert a.event_id == b.event_id
    assert a.source_channel_id == a.source_transport_id == "alice@example.org"


@pytest.mark.parametrize(
    "updates",
    [
        {"sender": "room@conference.example.org/medre"},
        {"sender": "bot@example.org/elsewhere", "kind": "chat"},
        {"sender": "unlisted@conference.example.org/alice"},
        {"sender": "room@conference.example.org"},
        {"sender": "stranger@example.org", "kind": "chat"},
        {"sender": "invalid"},
        {"kind": "error"},
        {"kind": "headline"},
        {"body": None},
        {"body": ""},
        {"body": "   "},
        {"body": "\x00"},
        {"body": "x" * 16385},
        {"id": " "},
        {"id": "x" * 1025},
    ],
)
def test_unadmitted_messages_are_not_canonicalized(config, updates):
    assert XMPPCodec(config).decode(stanza(**updates), timestamp=NOW) is None


@pytest.mark.parametrize(
    "tag", [*MUTATIONS, "{urn:xmpp:delay}delay", "{jabber:x:delay}x"]
)
def test_history_and_mutation_compatibility_bodies_are_not_fresh_text(config, tag):
    message = stanza()
    SubElement(message, tag)
    assert XMPPCodec(config).decode(message, timestamp=NOW) is None


def test_typing_and_receipts_have_no_text_history_side_effect(config):
    for tag in (
        "{http://jabber.org/protocol/chatstates}composing",
        "{urn:xmpp:receipts}received",
    ):
        message = stanza(body=None)
        SubElement(message, tag)
        assert XMPPCodec(config).decode(message, timestamp=NOW) is None


def test_reply_retains_native_target_without_inventing_ownership(config):
    message = stanza()
    SubElement(
        message,
        "{urn:xmpp:reply:0}reply",
        {"id": "parent", "to": "room@conference.example.org/bob"},
    )
    event = XMPPCodec(config).decode(message, timestamp=NOW)
    assert event.relations[0].target_event_id is None
    assert event.relations[0].target_fact is None
    assert event.relations[0].target_native_ref.native_message_id == "parent"


def test_body_language_prefers_unqualified_text(config):
    message = stanza(body="français")
    message[0].set("{http://www.w3.org/XML/1998/namespace}lang", "fr")
    SubElement(message, f"{{{CLIENT}}}body").text = "Default"
    assert XMPPCodec(config).decode(message, timestamp=NOW).payload["body"] == "Default"


def test_absent_native_id_remains_absent(config):
    codec = XMPPCodec(config)
    a = codec.decode(stanza(id=None), timestamp=NOW)
    b = codec.decode(stanza(id=None), timestamp=NOW)
    assert a.source_native_ref is None
    assert a.event_id != b.event_id


def test_muc_reply_uses_room_assigned_native_reference(config):
    """Sender IDs and room-assigned SIDs are different namespaces."""
    earlier = stanza(id="client-msg-1")
    SubElement(
        earlier,
        "{urn:xmpp:sid:0}stanza-id",
        {"by": config.rooms[0], "id": "muc-service-id"},
    )
    original = XMPPCodec(config).decode(earlier, timestamp=NOW)
    assert original.source_native_ref.native_message_id == "muc-service-id"
    assert original.metadata.native.data["xmpp"]["stanza_id"] == "client-msg-1"
    assert original.metadata.native.data["xmpp"]["room_stanza_id"] == "muc-service-id"

    reply = stanza(sender=f"{config.rooms[0]}/bob", id="client-msg-2")
    SubElement(
        reply,
        "{urn:xmpp:reply:0}reply",
        {"id": "muc-service-id", "to": f"{config.rooms[0]}/alice"},
    )
    result = XMPPCodec(config).decode(reply, timestamp=NOW)
    assert result.relations[0].target_native_ref == original.source_native_ref


def test_muc_ignores_foreign_room_stanza_ids(config):
    original = stanza()
    SubElement(
        original,
        "{urn:xmpp:sid:0}stanza-id",
        {"by": "untrusted@conference.example.org", "id": "foreign"},
    )
    result = XMPPCodec(config).decode(original, timestamp=NOW)
    assert result.source_native_ref is None
    assert result.metadata.native.data["xmpp"]["room_stanza_id"] is None


def test_muc_rejects_conflicting_room_stanza_ids(config):
    original = stanza()
    for value in ("first", "second"):
        SubElement(
            original,
            "{urn:xmpp:sid:0}stanza-id",
            {"by": config.rooms[0], "id": value},
        )
    assert XMPPCodec(config).decode(original, timestamp=NOW) is None


def test_direct_chat_keeps_client_native_id(config):
    result = XMPPCodec(config).decode(
        stanza(sender="alice@example.org/mobile", kind="chat"), timestamp=NOW
    )
    assert result.source_native_ref.native_message_id == "native-1"
