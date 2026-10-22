"""Room references come from matched server echoes, never local stanza IDs."""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock
from xml.etree.ElementTree import SubElement, fromstring

import pytest

from medre.adapters.xmpp import connection as binding
from medre.adapters.xmpp.codec import CLIENT
from medre.adapters.xmpp.connection import SlixConnection

pytestmark = pytest.mark.xmpp_sdk


def echo(written, config, native_id="room-id"):
    stanza = fromstring(b"<stream xmlns='jabber:client'>" + written + b"</stream>")[0]
    stanza.set("from", config.rooms[0] + "/" + config.nickname)
    SubElement(
        stanza, "{urn:xmpp:sid:0}stanza-id", {"by": config.rooms[0], "id": native_id}
    )
    return stanza


@pytest.fixture
async def ready(config):
    received = []
    instance = SlixConnection(config, received.append)
    instance.ready.set_result(None)
    instance.trusted_rooms = frozenset(config.rooms)
    instance.client.transport = Mock()
    try:
        yield instance, received
    finally:
        instance.client.transport = None
        await instance.close()


def payload(config):
    return {"to": config.rooms[0], "type": "groupchat", "body": "café 🦉"}


async def test_room_echo_is_registered_before_write_and_not_promoted_to_ingress(
    config, ready
):
    instance, received = ready
    instance.client.transport.write.side_effect = lambda data: instance._message(
        SimpleNamespace(xml=echo(data, config))
    )
    assert await instance.send(payload(config)) == "room-id"
    assert not instance._echoes
    assert len(received) == 1  # Codec retains its separate own-echo suppression.
    from medre.adapters.xmpp.codec import XMPPCodec
    from conftest import NOW

    assert XMPPCodec(config).decode(received[0].stanza, timestamp=NOW) is None


@pytest.mark.parametrize(
    "change", ["sender", "origin", "room", "conflict", "body", "delay", "oversized"]
)
async def test_spoofed_delayed_or_contradictory_echo_cannot_assign_a_reference(
    config, ready, monkeypatch, change
):
    instance, _ = ready
    monkeypatch.setattr(binding, "MUC_ECHO_TIMEOUT_SECONDS", 0.01)

    def observe(data):
        stanza = echo(data, config)
        if change == "sender":
            stanza.set("from", config.rooms[0] + "/attacker")
        elif change == "origin":
            stanza.find("{urn:xmpp:sid:0}origin-id").set("id", "other")
        elif change == "room":
            stanza.find("{urn:xmpp:sid:0}stanza-id").set("by", "elsewhere@example.org")
        elif change == "conflict":
            SubElement(
                stanza,
                "{urn:xmpp:sid:0}stanza-id",
                {"by": config.rooms[0], "id": "different"},
            )
        elif change == "body":
            stanza.find(f"{{{CLIENT}}}body").text = "modified"
        elif change == "delay":
            SubElement(stanza, "{urn:xmpp:delay}delay")
        else:
            stanza.find("{urn:xmpp:sid:0}stanza-id").set("id", "x" * 1025)
        instance._message(SimpleNamespace(xml=stanza))

    instance.client.transport.write.side_effect = observe
    assert await instance.send(payload(config)) is None
    instance.client.transport.write.assert_called_once()
    assert not instance._echoes


async def test_unadvertised_room_never_records_the_local_id(config, ready):
    instance, _ = ready
    instance.trusted_rooms = frozenset()
    assert await instance.send(payload(config)) is None
    instance.client.transport.write.assert_called_once()
    assert not instance._echoes


async def test_direct_handoff_keeps_its_own_origin_namespace(config, ready):
    instance, _ = ready
    native_id = await instance.send(
        {"to": config.peers[0], "type": "chat", "body": "direct"}
    )
    written = instance.client.transport.write.call_args.args[0]
    stanza = fromstring(b"<stream xmlns='jabber:client'>" + written + b"</stream>")[0]
    assert (
        stanza.get("id")
        == stanza.find("{urn:xmpp:sid:0}origin-id").get("id")
        == native_id
    )


@pytest.mark.parametrize("operation", ["cancel", "disconnect"])
async def test_pending_wait_releases_ownership_on_cancellation_or_disconnect(
    config, ready, operation
):
    instance, _ = ready
    written = asyncio.Event()
    instance.client.transport.write.side_effect = lambda _: written.set()
    task = asyncio.create_task(instance.send(payload(config)))
    await asyncio.wait_for(written.wait(), 1)
    if operation == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        instance._disconnected(None)
        assert await asyncio.wait_for(task, 1) is None
    assert not instance._echoes


async def test_capacity_exhaustion_does_not_block_or_invent_failed_handoff(
    config, ready, monkeypatch
):
    instance, _ = ready
    monkeypatch.setattr(binding, "MAX_PENDING_ECHOES", 1)
    written = asyncio.Event()
    data = []

    def capture(value):
        data.append(value)
        written.set()

    instance.client.transport.write.side_effect = capture
    first = asyncio.create_task(instance.send(payload(config)))
    await asyncio.wait_for(written.wait(), 1)
    assert await instance.send(payload(config)) is None
    instance._message(SimpleNamespace(xml=echo(data[0], config)))
    assert await first == "room-id"
    assert not instance._echoes


async def test_unstyled_control_is_serialized_separately_from_literal_body(
    config, ready
):
    instance, _ = ready
    request = {
        "to": config.peers[0],
        "type": "chat",
        "body": "*literal*",
        "unstyled": "true",
    }
    assert await instance.send(request)
    wire = instance.client.transport.write.call_args.args[0]
    message = fromstring(b"<stream xmlns='jabber:client'>" + wire + b"</stream>")[0]
    assert message.find(f"{{{CLIENT}}}body").text == "*literal*"
    assert message.find("{urn:xmpp:styling:0}unstyled") is not None
