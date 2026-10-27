"""XMPP stopped-state declaration truthfulness.

The adapter keeps no local state and the credential is server-side
configuration. The declaration must say so with zero files instead of
inventing capturable state. No server connection or SDK import is involved.
"""

from __future__ import annotations

from types import SimpleNamespace

from medre.adapters.xmpp.plugin import adapter_spec
from medre.adapters.xmpp.stopped_declaration import declaration
from medre.maintenance.stopped_declare import (
    DeclaredFile,
    declaration_from_spec,
    declared,
    plan_from_coverages,
)


def test_spec_references_the_declaration_module():
    reference = adapter_spec().stopped_state_declaration
    assert reference is not None
    assert reference.module == "medre.adapters.xmpp.stopped_declaration"
    assert reference.name == "declaration"
    assert declaration().provider == "xmpp"


def test_core_resolution_loads_the_declaration_through_the_spec():
    resolved = declaration_from_spec(adapter_spec())
    assert resolved.provider == "xmpp"


def test_plan_accepts_stateless_coverage_with_only_core_state(tmp_path):
    coverage = declaration().coverage(
        SimpleNamespace(jid="relay@example.org"), adapter_id="chat"
    )
    core = declared(
        provider="core",
        adapter_id="core",
        files=(DeclaredFile(tmp_path / "medre.db", "core_sqlite", "sqlite"),),
    )
    plan = plan_from_coverages(tmp_path, (core, coverage))
    assert [entry.path for entry in plan.entries] == ["medre.db"]
    summary = {item["provider"]: item for item in plan.summary()["providers"]}
    assert summary["xmpp"]["state_class"] == "stateless_local"
    assert summary["xmpp"]["required_files"] == 0


def test_xmpp_is_stateless_with_server_side_credential():
    coverage = declaration().coverage(
        SimpleNamespace(jid="relay@example.org"), adapter_id="chat"
    )
    assert coverage.state_class == "stateless_local"
    assert coverage.files == ()
    assert any("configuration" in note for note in coverage.notes)
    assert any("another host" in note for note in coverage.notes)


def test_session_conflicts_are_reported_unsupported():
    exclusion = declaration().local_exclusion(
        SimpleNamespace(jid="relay@example.org"), adapter_id="chat"
    )
    assert exclusion.scope == "unsupported"
    assert exclusion.method == "none"
    assert "server" in exclusion.note
