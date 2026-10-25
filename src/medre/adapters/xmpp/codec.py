"""Live text stanza decoding with explicit conversation and identity scopes."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from dataclasses import dataclass
from xml.etree.ElementTree import Element

from medre.core.events.canonical import CanonicalEvent, EventRelation, NativeRef
from medre.core.events.metadata import EventMetadata, NativeMetadata, TransportMetadata

from .styling import NAMESPACE, from_text
from .config import XMPPConfig, bare_jid, plain_text
from .bridge_context import extract_context

CLIENT = "jabber:client"
# Mutations require ownership and target binding. Never relay their bodies as
# fresh text messages merely because a peer includes a compatibility body.
MUTATIONS = frozenset(
    {
        "{urn:xmpp:message-correct:0}replace",
        "{urn:xmpp:message-retract:0}retract",
        "{urn:xmpp:message-retract:1}retract",
        "{urn:xmpp:fasten:0}apply-to",
        "{urn:xmpp:reactions:0}reactions",
    }
)


def room_stanza_id(stanza: Element, room: str) -> str | None:
    """Accept only an unambiguous ID assigned by the discovered room itself."""
    found = None
    for entry in stanza.findall("{urn:xmpp:sid:0}stanza-id"):
        if entry.get("by") != room:
            continue
        value = entry.get("id", "")
        if not value.strip() or len(value.encode("utf-8")) > 1024:
            return None
        if found is not None and found != value:
            return None
        found = value
    return found


def reply_fallback(stanza: Element, body: Element, text: str) -> tuple[str, str | None]:
    """Remove one valid reply fallback range using Unicode code-point offsets."""
    indicators = [
        entry
        for entry in stanza.findall("{urn:xmpp:fallback:0}fallback")
        if entry.get("for") == "urn:xmpp:reply:0"
    ]
    if len(indicators) != 1:
        return text, None
    ranges = indicators[0].findall("{urn:xmpp:fallback:0}body")
    if not list(indicators[0]):
        return "", text
    if len(ranges) != 1:
        return text, None
    entry = ranges[0]
    language = "{http://www.w3.org/XML/1998/namespace}lang"
    if entry.get(language) != body.get(language):
        return text, None
    if "start" not in entry.attrib and "end" not in entry.attrib:
        return "", text
    start, end = entry.get("start", ""), entry.get("end", "")
    if (
        not start.isascii()
        or not start.isdecimal()
        or not end.isascii()
        or not end.isdecimal()
    ):
        return text, None
    if len(start) > 10 or len(end) > 10:
        return text, None
    first, last = int(start), int(end)
    if not 0 <= first < last <= len(text):
        return text, None
    quote = "\n".join(line.removeprefix("> ") for line in text[first:last].splitlines())
    return text[:first] + text[last:], quote or None


@dataclass(frozen=True, slots=True)
class InboundStanza:
    """Native XML and the discovery evidence of its receiving connection."""

    stanza: Element
    trusted_rooms: frozenset[str] = frozenset()


class XMPPCodec:
    def __init__(self, config: XMPPConfig) -> None:
        self.config = config

    def decode(
        self,
        stanza: Element,
        *,
        timestamp: datetime,
        trusted_rooms: frozenset[str] = frozenset(),
    ) -> CanonicalEvent | None:
        """Map direct and MUC messages; controls, history and echoes yield None."""
        if stanza.tag != f"{{{CLIENT}}}message":
            return None
        kind = stanza.get("type", "normal")
        if kind not in {"chat", "normal", "groupchat"}:
            return None
        if any(child.tag in MUTATIONS for child in stanza):
            return None
        if (
            stanza.find("{urn:xmpp:delay}delay") is not None
            or stanza.find("{jabber:x:delay}x") is not None
        ):
            return None
        sender = stanza.get("from", "")
        sender_scope = sender
        sender_bare, _, nickname = sender.partition("/")
        try:
            bare_jid(sender_bare)
        except ValueError:
            return None
        if sender_bare == self.config.jid.split("/", 1)[0]:
            return None
        if kind == "groupchat":
            if (
                sender_bare not in self.config.rooms
                or not nickname
                or nickname == self.config.nickname
            ):
                return None
        else:
            if sender_bare not in self.config.peers:
                return None
            # Bare JIDs retain a single conversation across changing resources.
            sender = sender_bare
            nickname = ""
        bodies = [child for child in stanza if child.tag == f"{{{CLIENT}}}body"]
        # Prefer an unqualified body over an arbitrary translated alternative.
        body = next(
            (
                child
                for child in bodies
                if "{http://www.w3.org/XML/1998/namespace}lang" not in child.attrib
            ),
            bodies[0] if bodies else None,
        )
        if body is None or list(body):
            return None
        text = body.text or ""
        if (
            not text.strip()
            or not plain_text(text)
            or len(text.encode("utf-8")) > self.config.max_text_bytes
        ):
            return None
        native_id = stanza.get("id") or None
        if native_id and (
            len(native_id.encode("utf-8")) > 1024 or not native_id.strip()
        ):
            return None
        conversation = sender_bare
        # MUC reply IDs (XEP-0461) refer to room-issued stanza IDs
        # (XEP-0359), not the sender's message id attribute. Preserve the
        # latter in metadata for diagnostics and older servers without SIDs.
        room_id = None
        if kind == "groupchat" and conversation in trusted_rooms:
            room_id = room_stanza_id(stanza, conversation)
            if room_id is None and any(
                entry.get("by") == conversation
                for entry in stanza.findall("{urn:xmpp:sid:0}stanza-id")
            ):
                return None
        # A sender-generated groupchat id is not in the MUC reply namespace;
        # never register it as though a room service had assigned it.
        origins = stanza.findall("{urn:xmpp:sid:0}origin-id")
        origin_id = origins[0].get("id", "") if len(origins) == 1 else None
        if origins and (
            len(origins) != 1
            or not origin_id
            or not origin_id.strip()
            or len(origin_id.encode("utf-8")) > 1024
        ):
            return None
        ref_id = room_id if kind == "groupchat" else (origin_id or native_id)
        scope = json.dumps(
            [
                self.config.adapter_id,
                conversation,
                conversation if room_id else sender_scope,
                ref_id or native_id,
            ],
            ensure_ascii=False,
        )
        event_id = (
            "xmpp:" + hashlib.sha256(scope.encode()).hexdigest()
            if ref_id or native_id
            else "xmpp:" + uuid.uuid4().hex
        )
        relations: tuple[EventRelation, ...] = ()
        replies = stanza.findall("{urn:xmpp:reply:0}reply")
        reply_id = replies[0].get("id") if len(replies) == 1 else None
        if (
            reply_id is not None
            and reply_id.strip()
            and len(reply_id.encode("utf-8")) <= 1024
            and (kind != "groupchat" or conversation in trusted_rooms)
        ):
            text, quoted = reply_fallback(stanza, body, text)
            if not text.strip():
                return None
            relations = (
                EventRelation(
                    relation_type="reply",
                    target_event_id=None,
                    target_native_ref=NativeRef(
                        self.config.adapter_id, conversation, reply_id
                    ),
                    key=None,
                    fallback_text=quoted,
                ),
            )
        relay_context = extract_context(stanza)
        return CanonicalEvent(
            event_id=event_id,
            event_kind="message.created",
            schema_version=1,
            timestamp=timestamp,
            source_adapter=self.config.adapter_id,
            source_transport_id=sender,
            source_channel_id=conversation,
            parent_event_id=None,
            lineage=(),
            relations=relations,
            payload=from_text(
                text, unstyled=stanza.find(f"{{{NAMESPACE}}}unstyled") is not None
            ).to_payload(),
            source_native_ref=(
                NativeRef(self.config.adapter_id, conversation, ref_id)
                if ref_id
                else None
            ),
            metadata=EventMetadata(
                transport=TransportMetadata(protocol="xmpp", substrate="ip"),
                native=NativeMetadata(
                    data={
                        "xmpp": {
                            "schema_version": 1,
                            "sender_jid": sender,
                            "nickname": nickname or None,
                            "message_type": kind,
                            "stanza_id": native_id,
                            "origin_id": origin_id,
                            "room_stanza_id": room_id,
                            **(
                                {"relay": {"bridge_context": relay_context}}
                                if relay_context is not None
                                else {}
                            ),
                        }
                    }
                ),
            ),
        )
