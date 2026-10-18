"""Provider-owned test data; no imports from MEDRE's private test suite."""

import asyncio
import logging
from datetime import datetime, timezone
from xml.etree.ElementTree import Element, SubElement

import pytest

from medre.core.contracts.adapter import AdapterContext
from medre.core.rendering.renderer import RenderingResult
from medre.adapters.xmpp.codec import CLIENT
from medre.adapters.xmpp.config import XMPPConfig

NOW = datetime(2026, 10, 17, tzinfo=timezone.utc)


@pytest.fixture
def config():
    return XMPPConfig(
        "xmpp-test",
        jid="bot@example.org/medre",
        password="test-only-secret",
        rooms=("room@conference.example.org",),
        peers=("alice@example.org",),
        relay_prefix="",
    )


def stanza(
    *,
    sender="room@conference.example.org/alice",
    body="Hello <XMPP> & friends",
    kind="groupchat",
    id="native-1",
):
    attrs = {"from": sender, "type": kind}
    if id is not None:
        attrs["id"] = id
    root = Element(f"{{{CLIENT}}}message", attrs)
    if body is not None:
        SubElement(root, f"{{{CLIENT}}}body").text = body
    return root


@pytest.fixture
def context():
    events = []

    async def publish(event):
        events.append(event)

    return (
        AdapterContext(
            "xmpp-test",
            publish,
            logging.getLogger("xmpp-test"),
            lambda: NOW,
            asyncio.Event(),
        ),
        events,
    )


def rendered(*, to="room@conference.example.org", kind="groupchat", body="Hello"):
    return RenderingResult(
        "event-1", "xmpp-test", to, {"to": to, "type": kind, "body": body}, {}
    )
