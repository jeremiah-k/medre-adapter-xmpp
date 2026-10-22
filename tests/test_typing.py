"""Independent XEP-0085 XML and mailbox checks; no socket or server required."""

import asyncio
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import AsyncMock
from xml.etree.ElementTree import Element, SubElement, tostring, fromstring

import pytest

from medre.adapters.xmpp.codec import InboundStanza, CLIENT
from medre.adapters.xmpp.connection import SlixConnection
from medre.adapters.xmpp.session import XMPPSession
from medre.adapters.xmpp.typing import CHATSTATES, activity_from_stanza
from medre.core.contracts.typing import TypingTarget, TypingUpdate
from conftest import stanza


def state(name="composing", **kwargs):
    """Build native wire XML independently of the outgoing encoder."""
    value = stanza(body=None, **kwargs)
    SubElement(value, "{" + CHATSTATES + "}" + name)
    return value


@pytest.mark.parametrize(
    "name,active",
    [("composing", True), ("active", False), ("paused", False), ("inactive", False)],
)
def test_live_muc_states_project_only_configured_native_actor_activity(
    config, name, active
):
    """Group activity retains occupant scope without asserting a verified identity."""
    notice = activity_from_stanza(state(name), config)
    assert notice.typing is active
    assert notice.channel_id == config.rooms[0]
    assert notice.sender_id == config.rooms[0] + "/alice"


@pytest.mark.parametrize(
    "case",
    [
        "gone",
        "duplicate",
        "delay",
        "legacy_delay",
        "thread",
        "content",
        "foreign_content",
        "own",
        "unconfigured",
    ],
)
def test_optional_typing_rejects_ambiguous_historical_or_unscoped_notifications(
    config, case
):
    """Optional state cannot turn delayed history or arbitrary namespaces into activity."""
    xml = state("gone" if case == "gone" else "composing")
    if case == "duplicate":
        SubElement(xml, "{" + CHATSTATES + "}paused")
    elif case in {"delay", "legacy_delay"}:
        SubElement(
            xml, "{urn:xmpp:delay}delay" if case == "delay" else "{jabber:x:delay}x"
        )
    elif case == "thread":
        SubElement(xml, "{" + CLIENT + "}thread").text = "private-thread"
    elif case in {"content", "foreign_content"}:
        SubElement(
            xml, "{" + (CLIENT if case == "content" else "urn:unknown") + "}body"
        ).text = "content"
    elif case == "own":
        xml.set("from", config.rooms[0] + "/" + config.nickname)
    elif case == "unconfigured":
        xml.set("from", "other@conference.example.org/alice")
    assert activity_from_stanza(xml, config) is None


def test_direct_chat_uses_prepared_peer_scope_and_supports_gone(config):
    """Direct resources coalesce to configured bare peers without leaking to strangers."""
    notice = activity_from_stanza(
        state("gone", kind="chat", sender="alice@example.org/device"), config
    )
    assert (
        not notice.typing
        and notice.sender_id == notice.channel_id == "alice@example.org"
    )
    assert (
        activity_from_stanza(
            state(kind="chat", sender="stranger@example.org/device"), config
        )
        is None
    )


class Message:
    """Minimal wire serializer; state assertions inspect XML rather than SDK fields."""

    def __init__(self, **kwargs):
        """Create a stanza using only independently supplied native attributes."""
        self.xml = Element(
            "{" + CLIENT + "}message", {"to": kwargs["mto"], "type": kwargs["mtype"]}
        )
        if kwargs.get("mbody"):
            SubElement(self.xml, "{" + CLIENT + "}body").text = kwargs["mbody"]

    def __setitem__(self, key, value):
        """Support the SDK message-id setter used by content sends."""
        self.xml.set(key, value)

    def __str__(self):
        """Serialize XML with native namespaces preserved."""
        return tostring(self.xml, encoding="unicode")


def connection(config):
    """Bind a ready connection to an independent native write recorder."""
    conn = object.__new__(SlixConnection)
    conn.config = config
    conn.closed = asyncio.Event()
    conn.ready = asyncio.get_running_loop().create_future()
    conn.ready.set_result(None)
    conn.trusted_rooms = frozenset()
    conn._echoes = {}
    conn._typing_states = OrderedDict()
    conn._typing_peers = set()
    wire = []
    conn.client = SimpleNamespace(
        make_message=lambda **kwargs: Message(**kwargs), send_raw=wire.append
    )
    return conn, wire


async def test_native_refreshes_never_repeat_standalone_composing_and_content_resets_state(
    config,
):
    """Content messages carry active so a following composing transition is valid."""
    conn, wire = connection(config)
    target = TypingTarget(config.adapter_id, config.rooms[0])
    await conn.send_typing(TypingUpdate(target, 30))
    await conn.send_typing(TypingUpdate(target, 10))
    assert len(wire) == 1
    assert fromstring(wire[0]).find("{" + CHATSTATES + "}composing") is not None
    assert fromstring(wire[0]).find("{" + CLIENT + "}body") is None
    await conn.send({"to": config.rooms[0], "type": "groupchat", "body": "hello"})
    assert fromstring(wire[-1]).find("{" + CHATSTATES + "}active") is not None
    await conn.send_typing(TypingUpdate(target, 10))
    assert fromstring(wire[-1]).find("{" + CHATSTATES + "}composing") is not None
    await conn.send_typing(TypingUpdate(target, 0))
    count = len(wire)
    await conn.send_typing(TypingUpdate(target, 0))
    assert (
        len(wire) == count
        and fromstring(wire[-1]).find("{" + CHATSTATES + "}paused") is not None
    )


async def test_direct_typing_requires_negotiated_native_support(config):
    """A configured peer alone does not authorize unsolicited chat-state traffic."""
    conn, wire = connection(config)
    target = TypingTarget(config.adapter_id, config.peers[0])
    await conn.send_typing(TypingUpdate(target, 30))
    assert not wire
    conn._typing_peers.add(config.peers[0])
    await conn.send_typing(TypingUpdate(target, 30))
    assert len(wire) == 1


async def test_typing_bursts_coalesce_without_consuming_message_queue_slots(config):
    """Optional traffic cannot replace canonical-message ingress under queue pressure."""
    session = XMPPSession(config, AsyncMock(), typing_receive=AsyncMock())
    session._accepting = True
    try:
        for number in range(1100):
            xml = state(sender=config.rooms[0] + "/actor-" + str(number))
            session._enqueue(InboundStanza(xml))
        assert len(session._typing_pending) == 1024 and session.dropped_typing == 76
        assert session._queue.empty()
        session._enqueue(
            InboundStanza(state("paused", sender=config.rooms[0] + "/actor-0"))
        )
        assert not session._typing_pending[
            (config.rooms[0], config.rooms[0] + "/actor-0")
        ].typing
        session._enqueue(InboundStanza(stanza(body="ordinary message")))
        assert session._queue.qsize() == 1
    finally:
        await session.stop()
    assert not session._typing_pending


@pytest.mark.parametrize(
    "tag",
    ["{urn:xmpp:occupant-id:0}occupant-id", "{http://jabber.org/protocol/muc#user}x"],
)
def test_muc_metadata_does_not_discard_standalone_chat_states(config, tag):
    """Room-added identity metadata is not message content or identity authority."""
    xml = state()
    SubElement(xml, tag)
    assert activity_from_stanza(xml, config).typing


async def test_fake_activity_uses_optional_sink_and_separate_bounded_history():
    """SDK-free fake contracts mirror native capability without message evidence."""
    import logging
    from datetime import datetime, timezone
    from medre.core.contracts.adapter import AdapterContext
    from medre.core.contracts.typing import TypingActivity
    from medre.adapters.xmpp.fake import FakeXMPPAdapter

    fake = FakeXMPPAdapter("fake")
    activity = TypingActivity("source", "actor", True)
    assert not await fake.simulate_typing(activity)
    sink = AsyncMock(return_value=True)
    ctx = AdapterContext(
        "fake",
        AsyncMock(),
        logging.getLogger("fake-typing"),
        lambda: datetime.now(timezone.utc),
        asyncio.Event(),
        publish_typing=sink,
    )
    await fake.start(ctx)
    assert (await fake.health_check()).capabilities.typing
    assert await fake.simulate_typing(activity)
    sink.assert_awaited_once_with(activity)
    update = TypingUpdate(TypingTarget("fake", "destination"), 5)
    for _ in range(1001):
        await fake.send_typing(update)
    assert len(fake.typing_updates) == 1000
    assert not fake.delivered_payloads
    assert not ctx.publish_inbound.called
    await fake.stop()
    assert not await fake.simulate_typing(activity)
