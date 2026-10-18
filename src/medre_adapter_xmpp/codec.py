"""Live text stanza decoding with explicit conversation and identity scopes."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from xml.etree.ElementTree import Element

from medre.core.events.canonical import CanonicalEvent, EventRelation, NativeRef
from medre.core.events.metadata import EventMetadata, NativeMetadata, TransportMetadata

from .config import XMPPConfig, bare_jid, plain_text

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


class XMPPCodec:
    def __init__(self, config: XMPPConfig) -> None:
        self.config = config

    def decode(self, stanza: Element, *, timestamp: datetime) -> CanonicalEvent | None:
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
        if kind == "groupchat":
            for entry in stanza.findall("{urn:xmpp:sid:0}stanza-id"):
                candidate = entry.get("id", "")
                if entry.get("by") == conversation and candidate.strip():
                    if len(candidate.encode("utf-8")) > 1024:
                        return None
                    if room_id is not None and room_id != candidate:
                        return None  # Contradictory room-issued IDs.
                    room_id = candidate
        # A sender-generated groupchat id is not in the MUC reply namespace;
        # never register it as though a room service had assigned it.
        ref_id = room_id if kind == "groupchat" else native_id
        scope = json.dumps(
            [self.config.adapter_id, conversation, sender, ref_id or native_id],
            ensure_ascii=False,
        )
        event_id = (
            "xmpp:" + hashlib.sha256(scope.encode()).hexdigest()
            if ref_id or native_id
            else "xmpp:" + uuid.uuid4().hex
        )
        relations = ()
        reply = stanza.find("{urn:xmpp:reply:0}reply")
        if reply is not None and reply.get("id", "").strip():
            relations = (
                EventRelation(
                    relation_type="reply",
                    target_event_id=None,
                    target_native_ref=NativeRef(
                        self.config.adapter_id, conversation, reply.get("id")
                    ),
                    key=None,
                    fallback_text=None,
                ),
            )
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
            payload={"body": text},
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
                            "room_stanza_id": room_id,
                        }
                    }
                ),
            ),
        )
