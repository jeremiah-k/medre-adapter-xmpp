"""Bounded XEP-0393 blocks/spans; directives remain authoritative text."""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass

from medre.core.events.text import (
    MAX_STYLED_TEXT_LENGTH,
    MAX_TEXT_SPANS,
    MAX_TEXT_STYLE_DEPTH,
    StyledText,
    TextSpan,
    TextStyle,
)

NAMESPACE = "urn:xmpp:styling:0"
_MARKERS: dict[str, TextStyle] = {
    "*": "strong",
    "_": "emphasis",
    "~": "strike",
    "`": "code",
}
_DIRECTIVES = {value: key for key, value in _MARKERS.items()}


@dataclass(frozen=True)
class _Line:
    text: str
    start: int
    content: int
    end: int


class _Parser:
    def __init__(self, body: str) -> None:
        self.body = body
        self.spans: list[TextSpan] = []
        self.closers: dict[str, list[int]] = {marker: [] for marker in _MARKERS}
        for index, char in enumerate(body):
            if char in self.closers and index and not body[index - 1].isspace():
                self.closers[char].append(index)

    def add(self, start: int, end: int, style: TextStyle) -> None:
        self.spans.append(TextSpan(start, end, style))
        if len(self.spans) > MAX_TEXT_SPANS:
            raise ValueError("XMPP styling span limit exceeded")

    def inline(self, left: int, right: int, depth: int) -> None:
        if depth >= MAX_TEXT_STYLE_DEPTH:
            raise ValueError("XMPP styling nesting limit exceeded")
        index = left
        while index < right:
            char = self.body[index]
            if (
                char not in _MARKERS
                or index + 1 >= right
                or self.body[index + 1].isspace()
                or self.body[index + 1] == char
                or (index != left and not self.body[index - 1].isspace())
            ):
                index += 1
                continue
            closers = self.closers[char]
            position = bisect_right(closers, index + 1)
            if position == len(closers) or closers[position] >= right:
                index += 1
                continue
            end = closers[position]
            self.add(index, end + 1, _MARKERS[char])
            if char != "`":
                self.inline(index + 1, end, depth + 1)
            index = end + 1

    def blocks(self, lines: list[_Line], depth: int = 0) -> None:
        if depth >= MAX_TEXT_STYLE_DEPTH:
            raise ValueError("XMPP block nesting limit exceeded")
        index = 0
        while index < len(lines):
            line = lines[index]
            if line.text.startswith("```"):
                end = index + 1
                while end < len(lines) and lines[end].text != "```":
                    end += 1
                if end < len(lines):
                    end += 1
                self.add(line.start, lines[end - 1].end, "pre")
                index = end
            elif line.text.startswith(">"):
                children = []
                end = index
                while end < len(lines) and lines[end].text.startswith(">"):
                    child = lines[end]
                    trim = 2 if len(child.text) > 1 and child.text[1].isspace() else 1
                    children.append(
                        _Line(
                            child.text[trim:],
                            child.start,
                            child.content + trim,
                            child.end,
                        )
                    )
                    end += 1
                self.add(line.start, lines[end - 1].end, "quote")
                self.blocks(children, depth + 1)
                index = end
            else:
                self.inline(line.content, line.content + len(line.text), depth)
                index += 1


def from_text(body: str, *, unstyled: bool = False) -> StyledText:
    """Apply XEP-0393 without removing directives or interpreting Markdown."""
    if unstyled or len(body) > MAX_STYLED_TEXT_LENGTH:
        return StyledText(body)
    lines = []
    offset = 0
    # Only LF separates XMPP styling blocks; other Unicode separators remain
    # content. Core offsets always address the original Unicode body.
    for part in body.split("\n"):
        lines.append(_Line(part, offset, offset, offset + len(part)))
        offset += len(part) + 1
    parser = _Parser(body)
    try:
        parser.blocks(lines)
        # Nested quote levels on the same lines share one semantic quote span.
        return StyledText(body, tuple(dict.fromkeys(parser.spans)))
    except ValueError:
        return StyledText(body)


def to_text(value: StyledText) -> tuple[str, bool]:
    """Return wire text and whether clients must suppress implicit styling.

    Native directives are preserved. Semantic inline spans get directives only
    when a parse-back proves exact ranges and styles with no unintended spans.
    Unsupported blocks/links and delimiter collisions retain the plain body.
    """
    if not value.spans:
        return value.body, True
    if from_text(value.body).spans == value.spans:
        return value.body, False
    if any(span.style not in _DIRECTIVES for span in value.spans):
        return value.body, True
    pieces: list[str] = []
    expected: list[TextSpan] = []
    stack: list[tuple[TextSpan, int]] = []
    position = 0
    length = 0
    for span in value.spans:
        while stack and stack[-1][0].end <= span.start:
            parent, start = stack.pop()
            text = value.body[position : parent.end] + _DIRECTIVES[parent.style]
            pieces.append(text)
            length += len(text)
            expected.append(TextSpan(start, length, parent.style))
            position = parent.end
        text = value.body[position : span.start] + _DIRECTIVES[span.style]
        pieces.append(text)
        length += len(text)
        stack.append((span, length - 1))
        position = span.start
    while stack:
        parent, start = stack.pop()
        text = value.body[position : parent.end] + _DIRECTIVES[parent.style]
        pieces.append(text)
        length += len(text)
        expected.append(TextSpan(start, length, parent.style))
        position = parent.end
    pieces.append(value.body[position:])
    rendered = "".join(pieces)
    if len(rendered) > MAX_STYLED_TEXT_LENGTH:
        return value.body, True
    try:
        desired = StyledText(rendered, tuple(expected))
    except ValueError:
        return value.body, True
    if from_text(rendered) != desired:
        return value.body, True
    return rendered, False
