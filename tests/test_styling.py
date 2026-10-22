"""XEP-0393 examples, bounded parsing, explicit plain-text fallback."""

from dataclasses import replace
from msgspec.structs import replace as event_replace
from xml.etree.ElementTree import SubElement

import pytest

from medre.adapters.xmpp.adapter import validate_payload
from medre.adapters.xmpp.codec import XMPPCodec
from medre.adapters.xmpp.renderer import XMPPRenderer
from medre.adapters.xmpp.styling import NAMESPACE, from_text, to_text
from medre.core.contracts.adapter import AdapterPermanentError
from medre.core.events.text import StyledText, TextSpan, MAX_STYLED_TEXT_LENGTH
from medre.core.rendering.renderer import RenderingContext
from conftest import NOW, stanza


@pytest.mark.parametrize(
    "body,spans",
    [
        ("*strong span*", (TextSpan(0, 13, "strong"),)),
        ("plain _emphasis_ plain", (TextSpan(6, 16, "emphasis"),)),
        ("*strong*plain*", (TextSpan(0, 8, "strong"),)),
        ("* plain *strong*", (TextSpan(8, 16, "strong"),)),
        ("Everyone ~dis~likes cake.", (TextSpan(9, 14, "strike"),)),
        ("`*monospace*`", (TextSpan(0, 13, "code"),)),
        ("*_both_*", (TextSpan(0, 8, "strong"), TextSpan(1, 7, "emphasis"))),
        ("café *🦉*", (TextSpan(5, 8, "strong"),)),
    ],
)
def test_standard_inline_examples_keep_literal_directives(body, spans):
    assert from_text(body) == StyledText(body, spans)
    assert to_text(from_text(body)) == (body, False)


@pytest.mark.parametrize(
    "body",
    [
        "not strong*",
        "*not strong",
        "*not \n strong*",
        "*not *strong",
        "**",
        "***",
        "****",
        "foo_bar_baz",
        "* spaced *",
        "<b>plain</b>",
    ],
)
def test_invalid_directives_do_not_become_markdown(body):
    assert from_text(body) == StyledText(body)


def test_blocks_parse_before_spans_and_preserve_original_offsets():
    body = "> *quote*\n> ```ignored\n> _opaque_\n> ```\nplain"
    value = from_text(body)
    assert value.body == body
    assert any(
        span.style == "quote" and span.start == 0 and span.end == body.index("\nplain")
        for span in value.spans
    )
    assert any(
        span.style == "strong" and body[span.start : span.end] == "*quote*"
        for span in value.spans
    )
    assert any(
        span.style == "pre"
        and body[span.start : span.end] == "> ```ignored\n> _opaque_\n> ```"
        for span in value.spans
    )
    assert not any(span.style == "emphasis" for span in value.spans)
    assert from_text(">> nested").spans == (TextSpan(0, 9, "quote"),)


def test_limits_and_unstyled_control_leave_the_body_unchanged(config):
    body = "*literal*"
    message = stanza(body=body)
    SubElement(message, f"{{{NAMESPACE}}}unstyled")
    event = XMPPCodec(config).decode(message, timestamp=NOW)
    assert event.payload == {"body": body}
    for text in (
        "x" * (MAX_STYLED_TEXT_LENGTH + 1),
        " ".join("*x*" for _ in range(257)),
        ">" * 20 + " x",
    ):
        assert from_text(text) == StyledText(text)


@pytest.mark.parametrize(
    "value,wire,unstyled",
    [
        (StyledText("hello", (TextSpan(0, 5, "strong"),)), "*hello*", False),
        (
            StyledText("hello", (TextSpan(0, 5, "strong"), TextSpan(0, 5, "emphasis"))),
            "*_hello_*",
            False,
        ),
        (StyledText("*literal*"), "*literal*", True),
        (StyledText("word", (TextSpan(1, 3, "strong"),)), "word", True),
        (
            StyledText("link", (TextSpan(0, 4, "link", "https://example.org"),)),
            "link",
            True,
        ),
        (StyledText("one\ntwo", (TextSpan(0, 7, "strong"),)), "one\ntwo", True),
        (StyledText("a * b", (TextSpan(0, 5, "strong"),)), "*a * b*", False),
    ],
)
def test_semantic_output_is_parse_checked_or_explicitly_plain(value, wire, unstyled):
    assert to_text(value) == (wire, unstyled)


@pytest.mark.parametrize(
    "prefix,expected,plain",
    [("[relay] ", "[relay] *hello*", False), ("*relay ", "*relay hello", True)],
)
async def test_prefix_does_not_acquire_source_styles(config, prefix, expected, plain):
    config = replace(config, relay_prefix=prefix)
    event = XMPPCodec(config).decode(stanza(body="hello"), timestamp=NOW)
    event = event_replace(
        event, payload=StyledText("hello", (TextSpan(0, 5, "strong"),)).to_payload()
    )
    result = await XMPPRenderer({config.adapter_id: config}).render(
        event, RenderingContext("direct", config.adapter_id, config.rooms[0], "xmpp")
    )
    assert result.payload["body"] == expected
    assert (result.payload.get("unstyled") == "true") == plain
    validate_payload(result, config)


async def test_style_overhead_yields_to_final_budget_and_unstyled_is_closed(config):
    event = XMPPCodec(config).decode(stanza(body="hello"), timestamp=NOW)
    event = event_replace(
        event, payload=StyledText("hello", (TextSpan(0, 5, "strong"),)).to_payload()
    )
    result = await XMPPRenderer({config.adapter_id: config}).render(
        event,
        RenderingContext(
            "direct", config.adapter_id, config.rooms[0], "xmpp", max_text_bytes=5
        ),
    )
    assert result.payload["body"] == "hello" and result.payload["unstyled"] == "true"
    for value in (False, "false", None, "<script>"):
        result.payload["unstyled"] = value
        with pytest.raises(AdapterPermanentError):
            validate_payload(result, config)
