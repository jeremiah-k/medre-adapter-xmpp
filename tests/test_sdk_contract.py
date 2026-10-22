"""Pinned Slixmpp XML and lifecycle behavior; no network connections."""

import asyncio
from unittest.mock import AsyncMock, Mock
from xml.etree.ElementTree import fromstring

import pytest

from medre.core.contracts.adapter import AdapterSendError
from medre.adapters.xmpp.connection import SlixConnection

pytestmark = pytest.mark.xmpp_sdk


async def test_native_transport_write_serializes_text_without_local_queue(config):
    connection = SlixConnection(config, lambda _: None)
    client = connection.client
    transport = Mock()
    client.transport = transport
    connection.ready.set_result(None)
    native_id = await connection.send(
        {"to": config.rooms[0], "type": "groupchat", "body": "<tag> & Unicode 🦉"}
    )
    written = transport.write.call_args.args[0]
    root = fromstring(b"<stream xmlns='jabber:client'>" + written + b"</stream>")[0]
    assert root.find("{jabber:client}body").text == "<tag> & Unicode 🦉"
    assert native_id is None
    assert root.find("{urn:xmpp:sid:0}origin-id").get("id") == root.attrib["id"]
    assert root.attrib["type"] == "groupchat"
    assert client.waiting_queue.empty()
    client.transport = None
    await connection.close()


async def test_native_configuration_requires_verified_tls_and_no_plaintext(config):
    import ssl

    connection = SlixConnection(config, lambda _: None)
    assert connection.client.enable_plaintext is False
    assert connection.client.ssl_context.verify_mode == ssl.CERT_REQUIRED
    assert connection.client.ssl_context.check_hostname
    assert connection.client.enable_starttls
    assert connection.client.enable_direct_tls
    await connection.close()


async def test_room_readiness_waits_for_join_and_requests_zero_history(config):
    from slixmpp import JID

    connection = SlixConnection(config, lambda _: None)
    client = connection.client
    client.init_plugins()
    client.send_presence = Mock()
    client.plugin["xep_0030"].get_info = AsyncMock(
        return_value={"disco_info": {"features": []}}
    )
    presence = client.make_presence(pfrom=f"{config.rooms[0]}/{config.nickname}")
    client.plugin["xep_0045"].join_muc_wait = AsyncMock(
        return_value=(presence, None, [], [])
    )
    await connection._session_started(None)
    assert connection.ready.done() and connection.ready.exception() is None
    client.plugin["xep_0045"].join_muc_wait.assert_awaited_once_with(
        config.rooms[0],
        config.nickname,
        maxstanzas=0,
        timeout=config.connect_timeout_seconds,
    )
    join = client.plugin["xep_0045"].make_join_stanza(
        JID(config.rooms[0]), config.nickname, maxstanzas=0
    )
    history = join.xml.find(
        "{http://jabber.org/protocol/muc}x/{http://jabber.org/protocol/muc}history"
    )
    assert history.get("maxstanzas") == "0"
    await connection.close()


async def test_closed_session_and_write_failure_never_claim_handoff(config):
    connection = SlixConnection(config, lambda _: None)
    with pytest.raises(AdapterSendError):
        await connection.send(
            {"to": config.rooms[0], "type": "groupchat", "body": "before-ready"}
        )
    connection.ready.set_result(None)
    with pytest.raises(AdapterSendError, match="write failed"):
        await connection.send(
            {"to": config.rooms[0], "type": "groupchat", "body": "disconnected"}
        )
    await connection.close()
    with pytest.raises(AdapterSendError):
        await connection.send(
            {"to": config.rooms[0], "type": "groupchat", "body": "after-close"}
        )


async def test_native_close_cancels_sdk_filter_worker(config):
    connection = SlixConnection(config, lambda _: None)
    connection.client._run_out_filters = asyncio.create_task(
        connection.client.run_filters()
    )
    worker = connection.client._run_out_filters
    await connection.close()
    assert worker.done()
    assert connection.client._current_connection_attempt is None
    assert not connection.client.scheduled_events


async def test_inbound_sdk_callback_copies_stanzas_before_queueing(config):
    from slixmpp.stanza import Message

    received = []
    connection = SlixConnection(config, received.append)
    message = Message()
    message["body"] = "original"
    connection._message(message)
    message["body"] = "mutated"
    assert received[0].stanza.find("{jabber:client}body").text == "original"
    await connection.close()


async def test_close_reaps_pending_entity_capability_discovery(config):
    connection = SlixConnection(config, lambda _: None)
    client = connection.client
    client.init_plugins()
    entered = asyncio.Event()
    pending = []

    async def lookup(_presence, _version):
        pending.append(asyncio.current_task())
        entered.set()
        await asyncio.Future()

    client.plugin["xep_0115"]._process_caps_wrapped = lookup
    presence = client.make_presence(pfrom="peer@example.test/client")
    presence["caps"]["hash"] = "sha-1"
    presence["caps"]["ver"] = "test-capabilities"
    client.event("entity_caps", presence)
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await connection.close()
        assert pending[0].cancelled()
        assert not connection._handlers
        # A previously scheduled callback cannot begin discovery after close.
        await connection._entity_caps(presence)
        assert len(pending) == 1
    finally:
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


async def test_native_send_rejects_non_messaging_stanza_types(config):
    connection = SlixConnection(config, lambda _: None)
    connection.ready.set_result(None)
    connection.client.make_message = Mock()
    try:
        with pytest.raises(AdapterSendError, match="Unsupported XMPP message type"):
            await connection.send(
                {"to": config.rooms[0], "body": "text", "type": "error"}
            )
        connection.client.make_message.assert_not_called()
    finally:
        await connection.close()


async def test_reply_xml_escapes_ids_and_marks_unicode_quote_offsets(config):
    connection = SlixConnection(config, lambda _: None)
    connection.ready.set_result(None)
    transport = Mock()
    connection.client.transport = transport
    quote = "> café 🦉\n"
    try:
        native_id = await connection.send(
            {
                "to": config.peers[0],
                "type": "chat",
                "body": quote + "answer",
                "reply_id": 'parent<&"',
                "reply_fallback": quote,
            }
        )
        written = transport.write.call_args.args[0]
        root = fromstring(b"<stream xmlns='jabber:client'>" + written + b"</stream>")[0]
        assert root.find("{urn:xmpp:reply:0}reply").get("id") == 'parent<&"'
        region = root.find("{urn:xmpp:fallback:0}fallback/{urn:xmpp:fallback:0}body")
        assert int(region.get("end")) == len(quote)
        assert (
            root.find("{jabber:client}body").text[int(region.get("end")) :] == "answer"
        )
        assert root.get("id") == native_id
    finally:
        connection.client.transport = None
        await connection.close()
