"""Offline Slixmpp callback contracts; no SDK import or network required."""

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from medre.core.contracts.adapter import AdapterPermanentError, AdapterSendError
from medre_adapter_xmpp.connection import SlixConnection


class DummyClient:
    def __init__(self, *args):
        self.handlers = {}
        self.plugin = {
            "xep_0045": SimpleNamespace(join_muc_wait=AsyncMock()),
            "xep_0199": SimpleNamespace(enable_keepalive=lambda **kw: None),
        }

    def register_plugin(self, key):
        pass

    def add_event_handler(self, key, callback):
        self.handlers[key] = callback

    def send_presence(self):
        pass


@pytest.fixture
def client_stub(monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "slixmpp",
        SimpleNamespace(ClientXMPP=DummyClient, JID=lambda value: value),
    )


@pytest.mark.asyncio
async def test_failed_sasl_mechanism_does_not_preempt_fallback(config, client_stub):
    connection = SlixConnection(config, lambda _: None)
    assert "failed_auth" not in connection.client.handlers
    assert "failed_all_auth" in connection.client.handlers
    assert not connection.ready.done()
    connection.client.handlers["failed_all_auth"](None)
    with pytest.raises(AdapterPermanentError, match="authentication"):
        await connection.ready


@pytest.mark.asyncio
async def test_unexpected_occupant_identity_is_permanent(config, client_stub):
    connection = SlixConnection(config, lambda _: None)
    connection.client.plugin["xep_0045"].join_muc_wait.return_value = (
        {"from": config.rooms[0] + "/unexpected"},
    )
    await connection._session_started(None)
    with pytest.raises(AdapterPermanentError, match="unexpected occupant"):
        await connection.ready


@pytest.mark.asyncio
async def test_other_join_failure_is_retryable(config, client_stub):
    connection = SlixConnection(config, lambda _: None)
    connection.client.plugin["xep_0045"].join_muc_wait.side_effect = RuntimeError("sdk")
    await connection._session_started(None)
    with pytest.raises(AdapterSendError, match="session setup failed"):
        await connection.ready
