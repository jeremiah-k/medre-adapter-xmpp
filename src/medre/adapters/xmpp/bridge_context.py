"""Bounded MEDRE relay claims in an XML stanza extension.

The durable payload stores JSON text because XMPP send payloads contain strings.
Only normalized core context becomes XML; raw XML and arbitrary SDK data never
cross that interface. Inbound claims do not confer native identity or relation
binding authority. Ordinary clients continue using the human-readable body.
"""

from __future__ import annotations

import json
from typing import Any
from xml.etree.ElementTree import Element, SubElement, tostring

from medre.core.rendering.bridge_context import normalize_bridge_context
from .config import plain_text

NAMESPACE = "urn:medre:bridge-context:1"
MAX_CONTEXT_BYTES = 16384


def _element(name: str) -> str:
    """Qualify each extension element independently of the stanza namespace."""
    return f"{{{NAMESPACE}}}{name}"


def _attributes(value: dict[str, Any]) -> dict[str, str]:
    """Omit absent scalars while preserving explicit empty claims and Unicode."""
    return {key: item for key, item in value.items() if isinstance(item, str)}


def _xml_context(context: dict[str, Any]) -> Element:
    """Map the validated shared shape to XML without copying unknown native data."""
    root = Element(_element("context"), {"schema_version": "1"})
    source = SubElement(root, _element("source"), _attributes(context["source"]))
    ref = context["source"]["native_ref"]
    if ref is not None:
        SubElement(source, _element("native_ref"), _attributes(ref))
    relations = SubElement(root, _element("relations"))
    for hint in context["relations"]:
        relation = SubElement(relations, _element("relation"), _attributes(hint))
        if hint["target_native_ref"] is not None:
            SubElement(
                relation,
                _element("target_native_ref"),
                _attributes(hint["target_native_ref"]),
            )
    return root


def _xml_safe(value: Any) -> bool:
    """Check normalized values before ElementTree can serialize illegal XML text."""
    if isinstance(value, str):
        return plain_text(value)
    if isinstance(value, dict):
        return all(_xml_safe(item) for item in value.values())
    if isinstance(value, list):
        return all(_xml_safe(item) for item in value)
    return value is None or type(value) is int


def encode_context(value: object) -> str | None:
    """Prepare bounded durable context, or omit unrepresentable optional claims.

    Both JSON payload and emitted XML count toward the 16 KiB context limit.
    XML escaping can expand otherwise short claims, so the wire form is measured
    before the renderer accepts the optional metadata.
    """
    context = normalize_bridge_context(value)
    if context is None or not _xml_safe(context):
        return None
    text = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
    if (
        len(text.encode("utf-8")) > MAX_CONTEXT_BYTES
        or len(tostring(_xml_context(context), encoding="utf-8")) > MAX_CONTEXT_BYTES
    ):
        return None
    return text


def decode_context(value: object) -> dict[str, Any] | None:
    """Validate saved payload context before it can reach the native socket."""
    if not isinstance(value, str) or len(value) > MAX_CONTEXT_BYTES:
        return None
    try:
        if len(value.encode("utf-8")) > MAX_CONTEXT_BYTES:
            return None
        context = normalize_bridge_context(json.loads(value))
        return context if encode_context(context) is not None else None
    except (ValueError, UnicodeError, RecursionError):
        return None


def append_context(stanza: Element, value: object) -> None:
    """Append validated metadata while leaving native addressing and body untouched."""
    context = decode_context(value)
    if context is None:
        raise ValueError("Invalid rendered XMPP relay context")
    stanza.append(_xml_context(context))


def _reference(parent: Element, name: str) -> dict[str, str] | None:
    """Reject duplicate references rather than guessing which scope a peer intended."""
    refs = parent.findall(_element(name))
    if len(refs) > 1:
        raise ValueError("Ambiguous relay reference")
    return dict(refs[0].attrib) if refs else None


def extract_context(stanza: Element) -> dict[str, Any] | None:
    """Read one bounded extension as remote claims; malformed metadata is optional."""
    roots = stanza.findall(_element("context"))
    if len(roots) != 1:
        return None
    root = roots[0]
    if (
        root.get("schema_version") != "1"
        or len(tostring(root, encoding="utf-8")) > MAX_CONTEXT_BYTES
    ):
        return None
    sources = root.findall(_element("source"))
    groups = root.findall(_element("relations"))
    if len(sources) != 1 or len(groups) != 1:
        return None
    source: dict[str, Any] = dict(sources[0].attrib)
    try:
        source["native_ref"] = _reference(sources[0], "native_ref")
        relations = []
        for node in groups[0].findall(_element("relation")):
            hint: dict[str, Any] = dict(node.attrib)
            hint["target_native_ref"] = _reference(node, "target_native_ref")
            relations.append(hint)
    except ValueError:
        return None
    return normalize_bridge_context(
        {"schema_version": 1, "source": source, "relations": relations}
    )
