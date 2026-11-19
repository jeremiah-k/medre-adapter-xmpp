"""Startup-info facts from state the XMPP adapter already holds.

Core's runtime duck-types an optional ``startup_info()`` callable on a
built adapter and renders the returned scalar facts at the adapter's
ready transition (see the adapter-startup-info specification in the
core distribution).  This module shapes the facts this adapter holds at
that transition — the configured bare account JID and
the optional configured connection endpoint — into the plain mapping
the contract expects.

Pure read: the provider performs no stanza, discovery, or DNS I/O,
acquires no resources, and mutates no session state, so a synchronous
callable is safe to run on any worker thread.  The only lookup beyond
held state is the installed slixmpp distribution version via
``importlib.metadata`` — local package metadata, never a source literal
(the repository's pin-hygiene guard keeps pin values in
``pyproject.toml`` only) — and that fact is omitted when the package is
absent.  Server software is knowable only through a disco network
probe, so it is never reported here.  Facts that are not held (an
empty configured JID, no host/port override) are omitted rather than
probed: startup info must never gate, block, or delay the session.
This module imports nothing from ``medre.core``: the returned mapping
is plain JSON-like data, validated and redacted by core at the render
boundary.
"""

from __future__ import annotations

import importlib.metadata
from typing import Any

from medre.adapters.xmpp.config import XMPPConfig

__all__ = ["build_startup_info"]


def _installed_slixmpp_version() -> str | None:
    """Return the installed slixmpp distribution version, or ``None``.

    Reads local package metadata via ``importlib.metadata``; the value
    is never a source literal.  ``None`` means slixmpp is not installed
    (fake-only environments) and the fact is omitted.
    """
    try:
        return importlib.metadata.version("slixmpp")
    except importlib.metadata.PackageNotFoundError:
        return None


def _account_facts(config: XMPPConfig) -> list[dict[str, Any]]:
    """Keep the configured bare JID copyable as one account fact."""
    bare = config.jid.split("/", 1)[0]
    return [{"name": "jid", "value": bare}] if bare else []


def _endpoint_facts(config: XMPPConfig) -> list[dict[str, Any]]:
    """Endpoint facts for the configured connection override, if any.

    ``host`` and ``port`` are held only when configured together; an
    omitted pair means DNS SRV discovery and produces no facts here.
    """
    facts: list[dict[str, Any]] = []
    if config.host is not None:
        facts.append({"name": "host", "value": config.host, "label": "host"})
    if config.port is not None:
        facts.append({"name": "port", "value": config.port, "label": "port"})
    return facts


def build_startup_info(config: XMPPConfig) -> dict[str, Any]:
    """Build the startup-info mapping from held state.

    The configured account identity forms the first row and the
    configured endpoint plus the installed slixmpp version the second,
    per the contract's row guidance.
    """
    rows: list[list[dict[str, Any]]] = []
    account = _account_facts(config)
    if account:
        rows.append(account)
    endpoint = _endpoint_facts(config)
    slixmpp_version = _installed_slixmpp_version()
    if slixmpp_version:
        endpoint.append(
            {"name": "slixmpp_version", "value": slixmpp_version, "label": "slixmpp"}
        )
    if endpoint:
        rows.append(endpoint)
    return {"rows": rows}
