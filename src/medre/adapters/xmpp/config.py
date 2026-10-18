"""SDK-free, closed configuration for explicitly selected XMPP conversations."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from collections.abc import Iterable
from typing import Any
from medre.config.paths import MedrePaths


def bare_jid(value: str) -> str:
    """Check the supported bare account/room shape without normalizing identities.

    Slixmpp performs RFC 7622 preparation during native startup. Configuration
    keeps exact identifiers so a route cannot silently change its recipient.
    """
    if (
        not isinstance(value, str)
        or value != value.strip()
        or len(value.encode("utf-8")) > 3071
        or value.count("@") != 1
        or "/" in value
        or any(ord(c) <= 32 or ord(c) == 127 for c in value)
        or not all(value.split("@"))
        # RFC 7622 preparation case-folds ASCII uppercase, so a server-supplied
        # address can never match a mixed-case configured conversation.
        or any("A" <= c <= "Z" for c in value)
    ):
        raise ValueError("XMPP conversations require a bare user@domain JID")
    return value


def plain_text(value: object) -> bool:
    """Whether text can be serialized in an XML 1.0 stanza."""
    return isinstance(value, str) and all(
        c in "\t\n\r"
        or 0x20 <= ord(c) <= 0xD7FF
        or 0xE000 <= ord(c) <= 0xFFFD
        or 0x10000 <= ord(c) <= 0x10FFFF
        for c in value
    )


@dataclass(frozen=True)
class XMPPConfig:
    adapter_id: str
    jid: str = ""
    password: str = field(default="", repr=False)
    rooms: tuple[str, ...] = ()
    peers: tuple[str, ...] = ()
    nickname: str = "medre"
    host: str | None = None
    port: int | None = None
    connect_timeout_seconds: float = 30.0
    reconnect_delay_seconds: float = 5.0
    max_text_bytes: int = 16384
    inbound_queue_size: int = 100
    origin_label: str = ""
    relay_prefix: str = "[{origin_label}] {sender}: "

    def validate(self) -> XMPPConfig:
        if not isinstance(self.jid, str):
            raise ValueError("jid must be a string")
        if self.jid:
            bare_jid(self.jid.split("/", 1)[0])
            if not plain_text(self.jid) or self.jid != self.jid.strip():
                raise ValueError(
                    "jid must contain valid XML text without surrounding whitespace"
                )
            if "/" in self.jid and not self.jid.split("/", 1)[1]:
                raise ValueError("jid resource must not be empty")
        if not isinstance(self.password, str):
            raise ValueError("password must be a string")
        for name in ("rooms", "peers"):
            items = getattr(self, name)
            if not isinstance(items, tuple) or any(
                not isinstance(item, str) for item in items
            ):
                raise ValueError(f"{name} must be a tuple of bare JIDs")
            for item in items:
                bare_jid(item)
            if len(set(items)) != len(items):
                raise ValueError(f"{name} contains repeated JIDs")
        if set(self.rooms) & set(self.peers):
            raise ValueError("A JID cannot be both a room and a direct peer")
        if not plain_text(self.nickname) or not self.nickname.strip():
            raise ValueError("nickname must be non-empty XML text")
        if (self.host is None) != (self.port is None):
            raise ValueError(
                "host and port must be supplied together; omit both for DNS SRV"
            )
        if self.host is not None and (
            not isinstance(self.host, str)
            or not self.host.strip()
            or self.host != self.host.strip()
        ):
            raise ValueError("host must be a non-empty hostname")
        if self.port is not None and (
            type(self.port) is not int or not 1 <= self.port <= 65535
        ):
            raise ValueError("port must be an integer in 1..65535")
        for name in ("connect_timeout_seconds", "reconnect_delay_seconds"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{name} must be a positive finite number")
        for name in ("max_text_bytes", "inbound_queue_size"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("origin_label", "relay_prefix"):
            if not plain_text(getattr(self, name)):
                raise ValueError(f"{name} must be XML text")
        return self

    def message_type(self, conversation: str | None) -> str:
        """Select a destination only from the explicitly configured conversations."""
        bare_jid(conversation)
        if conversation in self.rooms:
            return "groupchat"
        if conversation in self.peers:
            return "chat"
        raise ValueError("Destination is not in the configured XMPP rooms or peers")


def prepare_config(
    config: XMPPConfig, *, adapter_id: str, paths: MedrePaths, routes: Iterable[Any]
) -> XMPPConfig:
    """Refuse routes that cannot match an admitted XMPP conversation."""
    del paths
    for route in routes:
        if not route.enabled:
            continue
        if route.source.adapter == adapter_id and route.source.channel is not None:
            config.message_type(route.source.channel)
        for target in route.targets:
            if target.adapter == adapter_id:
                if (
                    target.destination is not None
                    and target.destination.kind != "channel"
                ):
                    raise ValueError(
                        "XMPP destinations use configured bare JID channels"
                    )
                config.message_type(target.channel)
    return config
