"""Reply targets and quote ranges retain native scope and human-readable fallback."""

from dataclasses import replace
from xml.etree.ElementTree import SubElement

import msgspec
import pytest

from medre.core.contracts.adapter import AdapterPermanentError
from medre.core.events.canonical import EventRelation, NativeRef
from medre.core.rendering.renderer import RenderingContext
from medre.adapters.xmpp.adapter import validate_payload
from medre.adapters.xmpp.codec import XMPPCodec
from medre.adapters.xmpp.renderer import XMPPRenderer
from conftest import NOW, rendered, stanza


def reply_event(config, *, channel=None, adapter=None, quote="parent café 🦉"):
    event = XMPPCodec(config).decode(stanza(body="answer 🦉"), timestamp=NOW)
    relation = EventRelation(
        relation_type="reply",
        target_event_id="parent",
        key=None,
        fallback_text=quote,
        target_native_ref=NativeRef(
            adapter or config.adapter_id, channel or config.rooms[0], "room-parent"
        ),
    )
    return msgspec.structs.replace(event, relations=(relation,))


def ctx(config, **updates):
    return replace(
        RenderingContext("direct", config.adapter_id, config.rooms[0], "xmpp"),
        **updates,
    )


async def test_native_reply_has_scoped_target_and_unicode_safe_compatibility_quote(
    config,
):
    event = reply_event(config)
    result = await XMPPRenderer({config.adapter_id: config}).render(event, ctx(config))
    assert result.payload["reply_id"] == "room-parent"
    assert result.payload["reply_fallback"] == "> parent café 🦉\n"
    assert result.payload["body"] == "> parent café 🦉\nanswer 🦉"
    assert result.fallback_applied is None
    assert validate_payload(result, config) == result.payload
    assert event.payload["body"] == "answer 🦉"


@pytest.mark.parametrize(
    "scope", ["channel", "adapter", "missing", "multiple", "strategy"]
)
async def test_unbound_or_incompatible_reply_uses_readable_text_without_native_target(
    config, scope
):
    event = reply_event(
        config,
        channel="other@conference.example.org" if scope == "channel" else None,
        adapter="other" if scope == "adapter" else None,
    )
    if scope == "missing":
        event = msgspec.structs.replace(
            event,
            relations=(
                msgspec.structs.replace(event.relations[0], target_native_ref=None),
            ),
        )
    elif scope == "multiple":
        event = msgspec.structs.replace(event, relations=event.relations * 2)
    context = ctx(
        config, delivery_strategy="fallback_text" if scope == "strategy" else "direct"
    )
    result = await XMPPRenderer({config.adapter_id: config}).render(event, context)
    assert "reply_id" not in result.payload
    assert "parent café 🦉" in result.payload["body"]
    assert result.fallback_applied in {"relation_reply", "strategy_fallback_text"}


async def test_quote_cannot_exhaust_body_budget_or_clip_user_text(config):
    config = replace(config, max_text_bytes=len("answer 🦉".encode()))
    result = await XMPPRenderer({config.adapter_id: config}).render(
        reply_event(config, quote="parent" * 1000), ctx(config)
    )
    assert result.payload["body"] == "answer 🦉"
    assert result.payload["reply_id"] == "room-parent"
    assert "reply_fallback" not in result.payload


@pytest.mark.parametrize(
    "fields",
    [
        {"reply_id": None},
        {"reply_id": ""},
        {"reply_id": 1},
        {"reply_id": "x" * 1025},
        {"reply_id": "bad\x00id"},
        {"reply_fallback": "Hello"},
        {"reply_id": "parent", "reply_fallback": "other"},
        {"reply_id": "parent", "reply_fallback": "Hello"},
        {"extra": "unowned"},
    ],
)
def test_adapter_rejects_invalid_reply_payload_before_socket_use(config, fields):
    result = rendered()
    result.payload.update(fields)
    with pytest.raises(AdapterPermanentError):
        validate_payload(result, config)


def incoming(config, *, quoted="> café 🦉\n", answer="answer", range_values=None):
    message = stanza(
        kind="chat", sender=config.peers[0] + "/phone", body=quoted + answer
    )
    SubElement(message, "{urn:xmpp:reply:0}reply", {"id": "parent"})
    fallback = SubElement(
        message, "{urn:xmpp:fallback:0}fallback", {"for": "urn:xmpp:reply:0"}
    )
    SubElement(
        fallback,
        "{urn:xmpp:fallback:0}body",
        (
            range_values
            if range_values is not None
            else {"start": "0", "end": str(len(quoted))}
        ),
    )
    return message


def test_inbound_reply_separates_quote_from_body_using_codepoints(config):
    event = XMPPCodec(config).decode(incoming(config), timestamp=NOW)
    assert event.payload["body"] == "answer"
    assert event.relations[0].fallback_text == "café 🦉"
    assert event.relations[0].target_native_ref.native_message_id == "parent"


@pytest.mark.parametrize(
    "values",
    [
        {"start": "-1", "end": "3"},
        {"start": "1", "end": "9999"},
        {"start": "3", "end": "1"},
        {"start": "x", "end": "3"},
        {"start": "0", "end": "999999999999999999999999"},
        {"start": "٠", "end": "3"},
    ],
)
def test_invalid_quote_offsets_preserve_the_original_body(config, values):
    message = incoming(config, range_values=values)
    event = XMPPCodec(config).decode(message, timestamp=NOW)
    assert event.payload["body"] == "> café 🦉\nanswer"
    assert event.relations[0].fallback_text is None


@pytest.mark.parametrize("whole", ["range", "body", "all"])
def test_fallback_only_stanza_does_not_become_a_fresh_message(config, whole):
    message = incoming(config, answer="")
    fallback = message.find("{urn:xmpp:fallback:0}fallback")
    if whole == "body":
        fallback[0].attrib.clear()
    elif whole == "all":
        fallback.remove(fallback[0])
    assert XMPPCodec(config).decode(message, timestamp=NOW) is None


def test_direct_origin_id_survives_server_rewriting_and_rejects_ambiguity(config):
    message = stanza(
        kind="chat", sender=config.peers[0] + "/phone", id="server-rewrite"
    )
    SubElement(message, "{urn:xmpp:sid:0}origin-id", {"id": "origin"})
    event = XMPPCodec(config).decode(message, timestamp=NOW)
    assert event.source_native_ref.native_message_id == "origin"
    assert event.metadata.native.data["xmpp"]["stanza_id"] == "server-rewrite"
    SubElement(message, "{urn:xmpp:sid:0}origin-id", {"id": "conflict"})
    assert XMPPCodec(config).decode(message, timestamp=NOW) is None
