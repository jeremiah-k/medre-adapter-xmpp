"""XMPP operational boundary enforcement tests.

These tests enforce structural invariants that keep the XMPP adapter
decoupled from core runtime internals without requiring the ``slixmpp``
SDK or network access.  They use **AST import inspection** so violations
are caught without importing adapter modules, including alternate import
forms that share no text with the banned dotted paths.

Operational boundaries covered:

1. **Core routing containment** — XMPP adapter modules do not import
   the core routing engine or its models.  Route preflight consumes
   routes as duck-typed objects through the same ``Iterable[Any]`` seam
   contract as ``medre.runtime.adapter_preparation``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_SRC_ROOT = (
    Path(__file__).resolve().parent.parent / "src" / "medre" / "adapters" / "xmpp"
)
"""Root directory of XMPP adapter source files."""

_ROUTING_PACKAGE = "medre.core.routing"
"""Core routing package banned from XMPP adapter module imports."""


def _read_source(path: Path) -> str:
    """Read a source file and return its text."""
    return path.read_text()


def _xmpp_py_files(root: Path = _SRC_ROOT) -> list[Path]:
    """Return all ``*.py`` files under the XMPP adapter package."""
    return sorted(root.rglob("*.py"))


def _routing_import_lines(source: str) -> list[str]:
    """Return source lines whose imports resolve into the routing package.

    Covers ``import medre.core.routing...``, ``from medre.core.routing
    import ...``, ``from medre.core import routing``, and aliased forms.
    Prose and comments cannot trigger a violation because only AST
    import statements are inspected.
    """
    violations: list[str] = []
    lines = source.splitlines()
    for node in ast.walk(ast.parse(source)):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.module is None or node.level:
                continue
            names = [node.module]
            names.extend(f"{node.module}.{alias.name}" for alias in node.names)
        for name in names:
            if name == _ROUTING_PACKAGE or name.startswith(_ROUTING_PACKAGE + "."):
                lineno = getattr(node, "lineno", 0)
                text = lines[lineno - 1].strip() if 0 < lineno <= len(lines) else name
                violations.append(f"L{lineno}: {text}")
                break
    return violations


class TestXMPPRoutingBoundary:
    """XMPP adapter modules must not import core routing internals.

    **WHY this matters**: Adapter modules implement transport-specific
    logic and receive already-planned routes from the framework, so the
    route objects only need to support attribute access.  Importing the
    routing engine or its models couples the adapter to core
    implementation internals that core's cross-transport containment
    guard rejects in installed adapter source.
    """

    @pytest.mark.parametrize(
        "filepath",
        _xmpp_py_files(),
        ids=lambda p: p.name,
    )
    def test_no_core_routing_imports(self, filepath: Path) -> None:
        """XMPP adapter modules must not import core routing internals."""
        violations = _routing_import_lines(_read_source(filepath))
        assert (
            violations == []
        ), f"{filepath.name} contains banned core routing imports:\n" + "\n".join(
            violations
        )

    def test_scanner_flags_alternate_import_forms(self) -> None:
        """Every import form that resolves into the routing package flags."""
        source = (
            "from medre.core.routing import models\n"
            "from medre.core.routing.models import Route as R\n"
            "from medre.core import routing\n"
            "import medre.core.routing.router\n"
        )
        assert len(_routing_import_lines(source)) == 4

    def test_scanner_ignores_prose_mentions(self) -> None:
        """Comments and strings naming the routing package are not imports."""
        source = (
            "# from medre.core.routing import models (prose example)\n"
            'BANNED = "medre.core.routing.router"\n'
            "from medre.config.paths import MedrePaths\n"
        )
        assert _routing_import_lines(source) == []

    def test_nested_package_modules_are_scanned(self, tmp_path: Path) -> None:
        """Modules in nested XMPP package directories are discovered."""
        nested = tmp_path / "subpackage"
        nested.mkdir()
        (tmp_path / "top.py").write_text("")
        (nested / "mod.py").write_text("from medre.core.routing import models\n")
        files = _xmpp_py_files(tmp_path)
        assert sorted(p.name for p in files) == ["mod.py", "top.py"]
        assert _routing_import_lines(files[0].read_text()) != []
