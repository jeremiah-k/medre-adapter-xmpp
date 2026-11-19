"""Startup-info provider: duck-typed emission of held XMPP facts.

Covers:
- ``startup_info`` is discovered duck-typed on the adapter object (real
  and fake).
- Facts come only from already-held state: the configured account JID's
  localpart and domain, the optional configured host/port override, and
  the installed slixmpp distribution version read via
  ``importlib.metadata`` (never a source literal — the pin-hygiene
  guard keeps pins in pyproject only).
- Facts not held at ready (an empty configured JID, no host/port
  override, a slixmpp version when the package is absent) are omitted,
  never probed.  Server software is knowable only through a disco
  network probe and is never queried.
- Pure read: calling the hook mutates no adapter or session state and
  is safe on a worker thread, matching core's off-loop invocation.
- Configured JID and endpoint facts render in full through core's
  redaction boundary.

Core-side collection/rendering symbols are imported in tests only; the
adapter source must not import them.  Configuration-only fakes — no
stanzas, no disco, no DNS.  See core
``docs/spec/adapter-startup-info.md``.
"""

from __future__ import annotations

import ast
import importlib.metadata
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from medre.core.startup_info import (
    collect_startup_info,
    render_startup_info_block,
    startup_info_callable,
)

from medre.adapters.xmpp import startup_info as startup_info_module
from medre.adapters.xmpp.adapter import XMPPAdapter
from medre.adapters.xmpp.config import XMPPConfig
from medre.adapters.xmpp.fake import FakeXMPPAdapter

_EMITTED_AT = "2026-11-19T00:00:00+00:00"

# Synthetic slixmpp version for deterministically exercising the held
# version fact.  Not a pin value; the repo pins live in pyproject only.
_SYNTHETIC_SLIXMPP_VERSION = "1.2.3"


# -- Helpers ---------------------------------------------------------------


def _make_config(**overrides: Any) -> XMPPConfig:
    defaults: dict[str, Any] = {
        "jid": "bot@example.org/medre",
        "rooms": ("room@conference.example.org",),
        "relay_prefix": "",
    }
    defaults.update(overrides)
    return XMPPConfig("xmpp-test", **defaults)


def _account_row() -> list[dict[str, Any]]:
    return [
        {"name": "localpart", "value": "bot", "label": "local"},
        {"name": "domain", "value": "example.org", "label": "domain"},
    ]


def _endpoint_row(
    host: str | None = None, port: int | None = None
) -> list[dict[str, Any]]:
    row: list[dict[str, Any]] = []
    if host is not None:
        row.append({"name": "host", "value": host, "label": "host"})
    if port is not None:
        row.append({"name": "port", "value": port, "label": "port"})
    row.append({"name": "slixmpp_version", "value": _version(), "label": "slixmpp"})
    return row


def _version() -> str | None:
    """The real environment's answer, computed dynamically (no literals)."""
    try:
        return importlib.metadata.version("slixmpp")
    except importlib.metadata.PackageNotFoundError:
        return None


# -- Duck-typed discovery ---------------------------------------------------


def test_startup_info_symbol_is_discoverable() -> None:
    """Core's getattr discovery finds a callable on the adapter object."""
    adapter = XMPPAdapter(_make_config())
    discovered = startup_info_callable(adapter)
    assert discovered is not None
    assert callable(discovered)
    assert discovered.__name__ == "startup_info"


def test_fake_adapter_exposes_discoverable_startup_info() -> None:
    """The fake mirrors the real adapter's duck-typed seam."""
    fake = FakeXMPPAdapter("xmpp-test", _make_config())
    discovered = startup_info_callable(fake)
    assert discovered is not None
    assert callable(discovered)
    assert discovered.__name__ == "startup_info"


# -- Held-fact formatting ---------------------------------------------------


def test_account_facts_come_from_configured_jid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bare JID's localpart and domain are reported; the resource is not."""
    monkeypatch.setattr(startup_info_module, "_installed_slixmpp_version", lambda: None)
    adapter = XMPPAdapter(_make_config())
    assert adapter.startup_info() == {"rows": [_account_row()]}


def test_dns_srv_mode_omits_endpoint_facts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a host/port override only the account identity is held."""
    monkeypatch.setattr(startup_info_module, "_installed_slixmpp_version", lambda: None)
    adapter = XMPPAdapter(_make_config(host=None, port=None))
    assert adapter.startup_info() == {"rows": [_account_row()]}


def test_endpoint_override_and_version_follow_held_state() -> None:
    """Configured host/port and the installed slixmpp version are reported.

    The version comparison is against a computed value, never a literal
    pin.
    """
    adapter = XMPPAdapter(_make_config(host="c2s.example.net", port=5222))
    rows = adapter.startup_info()["rows"]
    names = [fact["name"] for row in rows for fact in row]
    assert {"localpart", "domain", "host", "port"} <= set(names)
    values = [
        fact["value"]
        for row in rows
        for fact in row
        if fact["name"] == "slixmpp_version"
    ]
    installed = _version()
    if installed is None:
        assert values == []
    else:
        assert values == [installed]


def test_server_software_is_never_probed() -> None:
    """Disco is a network probe; no server-software fact may ever appear."""
    adapter = XMPPAdapter(_make_config())
    rows = adapter.startup_info()["rows"]
    names = [fact["name"] for row in rows for fact in row]
    assert not any(
        "server" in name or "software" in name or "disco" in name for name in names
    )


def test_empty_configured_jid_omits_account_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No configured JID means no account identity — never probed."""
    monkeypatch.setattr(startup_info_module, "_installed_slixmpp_version", lambda: None)
    adapter = XMPPAdapter(_make_config(jid=""))
    assert adapter.startup_info() == {"rows": []}


def test_fake_adapter_reports_configured_facts_only() -> None:
    """The fake shapes the same configured facts through the same seam."""
    fake = FakeXMPPAdapter("xmpp-test", _make_config())
    assert fake.startup_info() == XMPPAdapter(_make_config()).startup_info()


# -- Purity and thread safety -----------------------------------------------


def test_startup_info_is_pure_read() -> None:
    """Reading facts neither mutates held state nor drifts between calls."""
    adapter = XMPPAdapter(_make_config())
    before = adapter.diagnostics()
    first = adapter.startup_info()
    second = adapter.startup_info()
    assert first == second
    assert adapter.diagnostics() == before
    assert adapter.session is None


def test_startup_info_snapshot_is_safe_from_worker_threads() -> None:
    """Core runs synchronous providers off-loop; the read must be stable."""
    adapter = XMPPAdapter(_make_config(host="c2s.example.net", port=5222))
    with ThreadPoolExecutor(max_workers=4) as pool:
        payloads = list(pool.map(lambda _: adapter.startup_info(), range(32)))
    assert all(payload == payloads[0] for payload in payloads)
    assert payloads[0] == adapter.startup_info()


def test_provider_module_imports_nothing_from_core() -> None:
    """The provider stays duck-typed: plain data, no medre.core coupling."""
    source = Path(startup_info_module.__file__ or "").read_text(encoding="utf-8")
    offenders: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            offenders.extend(
                alias.name
                for alias in node.names
                if alias.name == "medre.core" or alias.name.startswith("medre.core.")
            )
        elif isinstance(node, ast.ImportFrom) and not node.level:
            module = node.module or ""
            if module == "medre.core" or module.startswith("medre.core."):
                offenders.append(module)
    assert offenders == []


# ---------------------------------------------------------------------------
# Core boundary: collection, rendering
# ---------------------------------------------------------------------------


async def test_adapter_emits_held_facts_through_core(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Core's supervised collection preserves rows and renders the
    configured JID facts in full."""
    monkeypatch.setattr(startup_info_module, "_installed_slixmpp_version", lambda: None)
    adapter = XMPPAdapter(_make_config())
    info = await collect_startup_info("xmpp-1", "xmpp", adapter, emitted_at=_EMITTED_AT)
    assert info is not None
    assert [(f.name, f.row, f.label) for f in info.fields] == [
        ("localpart", 0, "local"),
        ("domain", 0, "domain"),
    ]
    assert info.fields[0].value == "bot"
    assert info.fields[1].value == "example.org"
    assert render_startup_info_block("xmpp.xmpp-1  ready", info) == (
        "xmpp.xmpp-1  ready\n" "  local bot · domain example.org"
    )


async def test_core_collection_keeps_version_fact_when_held(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A held slixmpp version renders on the endpoint row through core."""
    monkeypatch.setattr(
        startup_info_module,
        "_installed_slixmpp_version",
        lambda: _SYNTHETIC_SLIXMPP_VERSION,
    )
    adapter = XMPPAdapter(_make_config(host="c2s.example.net", port=5222))
    info = await collect_startup_info("xmpp-1", "xmpp", adapter, emitted_at=_EMITTED_AT)
    assert info is not None
    assert [(f.name, f.row, f.label) for f in info.fields] == [
        ("localpart", 0, "local"),
        ("domain", 0, "domain"),
        ("host", 1, "host"),
        ("port", 1, "port"),
        ("slixmpp_version", 1, "slixmpp"),
    ]
    assert info.fields[4].value == _SYNTHETIC_SLIXMPP_VERSION
    assert render_startup_info_block("xmpp.xmpp-1  ready", info) == (
        "xmpp.xmpp-1  ready\n"
        "  local bot · domain example.org\n"
        "  host c2s.example.net · port 5222 · slixmpp 1.2.3"
    )
