"""Opt-in two-account native proof; credentials remain outside the repository."""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

pytestmark = pytest.mark.live


async def test_native_room_ingress_and_direct_egress():
    path = os.environ.get("MEDRE_XMPP_TEST_CONFIG")
    if not path:
        pytest.skip("Set MEDRE_XMPP_TEST_CONFIG to a private two-account test config")
    from slixmpp.plugins.xep_0004.stanza import Form
    from medre.core.contracts.adapter import AdapterContext
    from medre.core.rendering.renderer import RenderingResult
    from medre.adapters.xmpp.adapter import XMPPAdapter
    from medre.adapters.xmpp.config import XMPPConfig
    from medre.adapters.xmpp.connection import SlixConnection

    settings = json.loads(await asyncio.to_thread(Path(path).read_text))
    room = f"medre-proof-{uuid4().hex}@{settings['muc_domain']}"
    nonce = uuid4().hex
    observed = asyncio.Queue()
    replies = asyncio.Queue()

    async def publish(event):
        await observed.put(event)

    connection_options = {
        key: settings[key] for key in ("host", "port") if key in settings
    }
    bot_jid = settings["bot_jid"]
    peer_jid = settings["peer_jid"]
    peer = SlixConnection(
        XMPPConfig(
            "peer",
            jid=peer_jid,
            password=settings["peer_password"],
            peers=(bot_jid.split("/", 1)[0],),
            **connection_options,
        ),
        replies.put_nowait,
    )
    adapter = XMPPAdapter(
        XMPPConfig(
            "bot",
            jid=bot_jid,
            password=settings["bot_password"],
            rooms=(room,),
            peers=(peer_jid.split("/", 1)[0],),
            **connection_options,
        )
    )
    joined = False
    try:
        await peer.open()
        await peer.client.plugin["xep_0045"].join_muc_wait(
            room, "peer", maxstanzas=0, timeout=10
        )
        joined = True
        await peer.client.plugin["xep_0045"].set_room_config(room, Form())
        await adapter.start(
            AdapterContext(
                "bot",
                publish,
                logging.getLogger("xmpp-live"),
                lambda: datetime.now(timezone.utc),
                asyncio.Event(),
            )
        )
        peer.send(
            {"to": room, "type": "groupchat", "body": f"Native <xml> & 🦉 {nonce}"}
        )
        event = await asyncio.wait_for(observed.get(), 10)
        assert event.source_transport_id == f"{room}/peer"
        assert event.payload["body"] == f"Native <xml> & 🦉 {nonce}"
        handoff = await adapter.deliver(
            RenderingResult(
                nonce,
                "bot",
                peer_jid.split("/", 1)[0],
                {"to": peer_jid.split("/", 1)[0], "type": "chat", "body": nonce},
                {},
            )
        )
        async with asyncio.timeout(10):
            while True:
                reply = await replies.get()
                body = reply.find("{jabber:client}body")
                if (
                    reply.get("type") == "chat"
                    and body is not None
                    and body.text == nonce
                ):
                    break
        assert handoff.confirmation_level == "local_transport"
        assert (await adapter.health_check()).health == "healthy"
    finally:
        try:
            await adapter.stop()
            if joined:
                await peer.client.plugin["xep_0045"].destroy(room)
        finally:
            await peer.close()
