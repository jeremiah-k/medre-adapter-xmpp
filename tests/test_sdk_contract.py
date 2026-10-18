"""Pinned Slixmpp XML and lifecycle behavior; no network connections."""

import asyncio
from unittest.mock import AsyncMock, Mock
from xml.etree.ElementTree import fromstring

import pytest

from medre.core.contracts.adapter import AdapterSendError
from medre_adapter_xmpp.connection import SlixConnection

pytestmark = pytest.mark.xmpp_sdk


async def test_native_transport_write_serializes_text_without_local_queue(config):
    connection = SlixConnection(config, lambda _: None)
    client = connection.client
    transport = Mock()
    client.transport = transport
    connection.ready.set_result(None)
    native_id = connection.send(
        {"to": config.rooms[0], "type": "groupchat", "body": "<tag> & Unicode 🦉"}
    )
    written = transport.write.call_args.args[0]
    root = fromstring(b"<stream xmlns='jabber:client'>" + written + b"</stream>")[0]
    assert root.find("{jabber:client}body").text == "<tag> & Unicode 🦉"
    assert root.attrib["id"] == native_id
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
        connection.send(
            {"to": config.rooms[0], "type": "groupchat", "body": "before-ready"}
        )
    connection.ready.set_result(None)
    with pytest.raises(AdapterSendError, match="write failed"):
        connection.send(
            {"to": config.rooms[0], "type": "groupchat", "body": "disconnected"}
        )
    await connection.close()
    with pytest.raises(AdapterSendError):
        connection.send(
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
    assert received[0].find("{jabber:client}body").text == "original"
    await connection.close()
