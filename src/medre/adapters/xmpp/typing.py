"""Bounded XEP-0085 projection; native chat state is never a stored message."""

from __future__ import annotations

from xml.etree.ElementTree import Element

from medre.core.contracts.typing import TypingActivity
from .codec import CLIENT
from .config import XMPPConfig

CHATSTATES = "http://jabber.org/protocol/chatstates"
STATES = frozenset({"active", "composing", "paused", "inactive", "gone"})


def activity_from_stanza(stanza: Element, config: XMPPConfig) -> TypingActivity | None:
    """Read one live configured actor, honoring direct/MUC and delayed-state rules.

    Ignore our own relay identity, ambiguous states, groupchat gone, threaded
    activity, and composing attached to message content. Unsupported optional
    state never changes the ordinary body codec's interpretation.
    """
    if (
        stanza.tag != f"{{{CLIENT}}}message"
        or stanza.get("type") not in {"chat", "groupchat"}
        or stanza.find("{urn:xmpp:delay}delay") is not None
        or stanza.find("{jabber:x:delay}x") is not None
        or stanza.find(f"{{{CLIENT}}}thread") is not None
    ):
        return None
    states = [
        child.tag.split("}")[-1]
        for child in stanza
        if child.tag.startswith("{" + CHATSTATES + "}")
    ]
    if len(states) != 1 or states[0] not in STATES:
        return None
    state = states[0]
    sender = stanza.get("from", "")
    bare, _, nick = sender.partition("/")
    if bare == config.jid.partition("/")[0]:
        return None
    if stanza.get("type") == "groupchat":
        if (
            bare not in config.rooms
            or not nick
            or nick == config.nickname
            or state == "gone"
        ):
            return None
        channel = bare
    else:
        if bare not in config.peers:
            return None
        channel = sender = bare
    bodies = [
        child
        for child in stanza
        if child.tag in {f"{{{CLIENT}}}body", f"{{{CLIENT}}}subject"}
    ]
    if bodies and state != "active":
        return None
    if state == "composing" and any(
        child.tag
        not in {
            f"{{{CHATSTATES}}}composing",
            "{urn:xmpp:sid:0}stanza-id",
            "{urn:xmpp:sid:0}origin-id",
            "{urn:xmpp:occupant-id:0}occupant-id",
            "{http://jabber.org/protocol/muc#user}x",
        }
        for child in stanza
    ):
        return None
    try:
        return TypingActivity(channel, sender, state == "composing")
    except ValueError:
        return None
