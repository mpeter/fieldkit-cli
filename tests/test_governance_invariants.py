"""Mechanized governance invariants — prose MUSTs converted to executable checks.

Governing artifact: docs/audit/first-principles-review-2026-07.md, Finding 2
("prose MUSTs that could be hooks decay; mechanized rules do not"). Each test
here pins an architectural claim that AGENTS.md states in prose and that has
no other enforcement:

1. ``fieldkit/errors.py`` imports nothing from fieldkit or third-party code
   (AGENTS.md: "errors.py has zero imports from fieldkit — any module may
   safely import from it").
2. ``fieldkit/cli_exit.py`` imports from fieldkit only ``fieldkit.config`` and
   ``fieldkit.errors`` (AGENTS.md: "No imports from fieldkit.llm, fieldkit.sf,
   or fieldkit.pursuit in cli_exit.py — do not add them").
3. The domain layer contains no executable ``sys.exit()`` call and no
   ``raise SystemExit`` — the historic regression ratchet. The audit
   (docs/audit/system-audit-2026-07.md §2) verified the domain layer clean;
   this test keeps it clean while the ~174 leaf-command sites are migrated
   per docs/briefs/historic regression-exit-code-leaves.md.
4. The hardcoded LLM fallback model uses the ``vertex_ai/`` LiteLLM prefix
   (docs/reference/environment-vars.md resolution chain, step 7).

All checks are AST-based, so docstring mentions of ``sys.exit`` (e.g.
``gmail/decay_domain.py`` line 6) do not trip them.
"""

import ast
import re
import sys
import warnings
from pathlib import Path

import pytest

from fieldkit.llm.core import _HARDCODED_DEFAULT_MODEL
from hooks.shim_guard import _is_shim

_SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "fieldkit"

# The exit boundary itself and the CLI adapter layer are the only places
# allowed to terminate the process (two-layer model, AGENTS.md Architecture).
_EXIT_ALLOWED = {
    _SRC_ROOT / "__main__.py",
    _SRC_ROOT / "cli_exit.py",
}
_EXIT_ALLOWED_DIRS = {_SRC_ROOT / "commands"}

# Guard against path drift silently emptying the scan: the domain layer had
# well over this many modules when the invariant was pinned (2026-07).
_MIN_DOMAIN_FILES_SCANNED = 50

pytestmark = pytest.mark.unit


def _parse(path: Path) -> ast.Module:
    """Parse a source file into an AST module.

    Args:
        path: Absolute path to a Python source file.

    Returns:
        The parsed ``ast.Module``.
    """
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imported_top_level_modules(tree: ast.Module) -> set[str]:
    """Collect fully qualified module names imported anywhere in a tree.

    Args:
        tree: Parsed module AST.

    Returns:
        Set of dotted module paths from both ``import X`` and
        ``from X import Y`` statements. Relative imports are returned with a
        leading ``.`` so callers can flag them explicitly.
    """
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            prefix = "." * node.level
            modules.add(prefix + (node.module or ""))
    return modules


def _domain_files() -> list[Path]:
    """Enumerate domain-layer source files subject to the process-exit ban.

    Returns:
        Sorted list of ``.py`` files under ``src/fieldkit/`` excluding the
        CLI adapter layer and the two allowed exit-boundary files.
    """
    files = []
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        if path in _EXIT_ALLOWED:
            continue
        if any(allowed in path.parents for allowed in _EXIT_ALLOWED_DIRS):
            continue
        files.append(path)
    return files


def _process_exit_sites(tree: ast.Module) -> list[int]:
    """Find line numbers of executable process-exit constructs in a tree.

    Detects ``sys.exit(...)`` calls and ``raise SystemExit(...)`` statements.

    Args:
        tree: Parsed module AST.

    Returns:
        Line numbers of each offending node, empty when the module is clean.
    """
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "exit"
                and isinstance(func.value, ast.Name)
                and func.value.id == "sys"
            ):
                lines.append(node.lineno)
        elif isinstance(node, ast.Raise) and node.exc is not None:
            exc = node.exc
            name = exc.func if isinstance(exc, ast.Call) else exc
            if isinstance(name, ast.Name) and name.id == "SystemExit":
                lines.append(node.lineno)
    return lines


# ── TestExceptionModuleIsolation (flattened) ────────────────────────────────


def test_exception_module_isolation_errors_module_imports_stdlib_only():
    """errors.py must import only from the standard library.

    The zero-dependency guarantee is what lets every domain module import
    the exception hierarchy without risking an import cycle.
    """
    imports = _imported_top_level_modules(_parse(_SRC_ROOT / "errors.py"))
    non_stdlib = {mod for mod in imports if mod.startswith(".") or mod.split(".")[0] not in sys.stdlib_module_names}
    assert non_stdlib == set(), f"errors.py gained non-stdlib imports: {sorted(non_stdlib)}"


def test_exception_module_isolation_cli_exit_fieldkit_imports_restricted():
    """cli_exit.py may import from fieldkit only config and errors.

    Importing fieldkit.llm / fieldkit.sf / fieldkit.pursuit here would
    make the exit boundary transitively depend on every heavy domain
    module — the exact coupling the errors-module extraction removed.
    """
    imports = _imported_top_level_modules(_parse(_SRC_ROOT / "cli_exit.py"))
    fieldkit_imports = {mod for mod in imports if mod == "fieldkit" or mod.startswith(("fieldkit.", "."))}
    allowed = {"fieldkit.config", "fieldkit.errors"}
    assert fieldkit_imports <= allowed, (
        f"cli_exit.py imports beyond the allowed set {sorted(allowed)}: {sorted(fieldkit_imports - allowed)}"
    )


# ── TestDomainLayerExitBan (flattened) ──────────────────────────────────────


def test_domain_layer_exit_ban_domain_layer_has_no_process_exit():
    """No sys.exit() call or raise SystemExit outside commands/ and the boundary.

    Domain modules signal failure by raising typed exceptions from
    fieldkit.errors; only cli_main()/the dispatcher backstop convert them
    to exit codes. A violation here would bypass the taxonomy for every
    caller that imports the domain function (the implementation note failure mode).
    """
    files = _domain_files()
    assert len(files) >= _MIN_DOMAIN_FILES_SCANNED, (
        f"Only {len(files)} domain files scanned — the src/fieldkit layout moved; update _SRC_ROOT."
    )
    violations = {
        str(path.relative_to(_SRC_ROOT)): lines for path in files if (lines := _process_exit_sites(_parse(path)))
    }
    assert violations == {}, f"Process-exit constructs in domain modules (file: line numbers): {violations}"


# ── TestModelFallbackContract (flattened) ───────────────────────────────────


def test_model_fallback_contract_hardcoded_fallback_model_uses_vertex_prefix():
    """The fallback must be LiteLLM-routable to Vertex without env help.

    A bare model name here would fail ADC routing at the one moment the
    fallback exists to cover: no env vars, no config file.
    """
    assert _HARDCODED_DEFAULT_MODEL.startswith("vertex_ai/"), (
        f"Fallback model {_HARDCODED_DEFAULT_MODEL!r} lost its vertex_ai/ routing prefix"
    )


_TESTS_ROOT = Path(__file__).resolve().parent

# Private-coupling counts — advisory only (implementation change, operator-ratified 2026-07-14).
# These were hard gates from 2026-07-06 to 2026-07-14. The ceiling only ever went
# up (354 → 395 in 5 days, never decreased) and blocked the driver loop on 3 issues.
# Demoted to warnings: the count is still measured and reported, but does not fail
# the test suite. The CODEOWNERS gate on this file prevents silent weakening.

_PRIVATE_PATCH_RE = re.compile(r"""patch\(\s*["']fieldkit\.[\w.]*\._[a-z]""")
_PRIVATE_IMPORT_RE = re.compile(r"from fieldkit\.[a-z_.]+ import _[a-z]")
_PRIVATE_MONKEYPATCH_RE = re.compile(r"""monkeypatch\.setattr\(\s*["']fieldkit\.[^"']*\._[a-z]""")


def _count_test_pattern(pattern: re.Pattern[str]) -> int:
    """Count matches of a source pattern across all test files.

    Args:
        pattern: Compiled regex applied to each test file's full text.

    Returns:
        Total match count over ``tests/**/*.py``, excluding this file.
    """
    total = 0
    for path in sorted(_TESTS_ROOT.rglob("*.py")):
        if path == Path(__file__).resolve():
            continue
        total += len(pattern.findall(path.read_text(encoding="utf-8")))
    return total


# ── TestSuiteCouplingRatchet (flattened) ────────────────────────────────────


def test_suite_coupling_ratchet_private_symbol_patches_advisory():
    """Report private-symbol patch count (advisory — does not fail).

    Prefer patching the public boundary the module exposes; where a private
    seam is genuinely required, TC-013 demands a justifying comment.
    """
    count = _count_test_pattern(_PRIVATE_PATCH_RE)
    if count > 0:
        warnings.warn(
            f"Private-symbol patch count: {count}. Prefer patching the public boundary instead.",
            stacklevel=1,
        )


def test_suite_coupling_ratchet_private_imports_advisory():
    """Report private-import count (advisory — does not fail)."""
    count = _count_test_pattern(_PRIVATE_IMPORT_RE)
    if count > 0:
        warnings.warn(
            f"Private-import count: {count}. Exercise the public API instead.",
            stacklevel=1,
        )


def test_suite_coupling_ratchet_private_monkeypatches_advisory():
    """Report private monkeypatch.setattr count (advisory — does not fail).

    ``monkeypatch.setattr("fieldkit.*._x", ...)`` is semantically identical to
    ``patch("fieldkit.*._x", ...)`` and shares the same coupling risk (TC-008/TC-013).
    """
    count = _count_test_pattern(_PRIVATE_MONKEYPATCH_RE)
    if count > 0:
        warnings.warn(
            f"Private monkeypatch.setattr count: {count}. Patch the public boundary instead.",
            stacklevel=1,
        )


# ===========================================================================
# shim_guard._is_shim — AST-based shim detection (pure function)
# ===========================================================================

# _is_shim is imported at the top of this file (with the other imports).
# It takes a source string and returns bool — no subprocess or filesystem involvement.


# ── True cases (shim detected) ───────────────────────────────────────────────


def test_is_shim_star_import_is_shim() -> None:
    assert _is_shim("from foo import *\n") is True


def test_is_shim_explicit_reexport_is_shim() -> None:
    assert _is_shim("from foo import Bar as Bar\n") is True


def test_is_shim_docstring_plus_reexport_is_shim() -> None:
    src = '"""Re-exports from foo."""\nfrom foo import Bar as Bar\n'
    assert _is_shim(src) is True


def test_is_shim_all_plus_reexport_is_shim() -> None:
    src = "from foo import Bar as Bar\n__all__ = ['Bar']\n"
    assert _is_shim(src) is True


def test_is_shim_future_plus_reexport_is_shim() -> None:
    src = "from __future__ import annotations\nfrom foo import Bar as Bar\n"
    assert _is_shim(src) is True


# ── False cases (not a shim) ─────────────────────────────────────────────────


def test_is_shim_file_with_function_is_not_shim() -> None:
    src = "from foo import Bar as Bar\n\ndef helper():\n    pass\n"
    assert _is_shim(src) is False


def test_is_shim_non_all_assignment_is_not_shim() -> None:
    src = "from foo import Bar as Bar\nVERSION = '1.0'\n"
    assert _is_shim(src) is False


def test_is_shim_plain_import_without_alias_is_not_shim() -> None:
    """from foo import Bar (no 'as Bar') is real import logic, not a re-export."""
    assert _is_shim("from foo import Bar\n") is False


def test_is_shim_empty_file_is_not_shim() -> None:
    assert _is_shim("") is False


def test_is_shim_docstring_only_is_not_shim() -> None:
    assert _is_shim('"""Just a docstring."""\n') is False


def test_is_shim_syntax_error_is_not_shim() -> None:
    assert _is_shim("def (broken syntax:\n") is False
