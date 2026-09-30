"""Mechanized governance invariants — prose MUSTs converted to executable checks.

Governing artifact: docs/audit/first-principles-review-2026-07.md, Finding 2
("prose MUSTs that could be hooks decay; mechanized rules do not"). Each test
here pins an architectural claim that AGENTS.md states in prose and that has
no other enforcement:

1. ``fieldkit/errors.py`` imports nothing from fieldkit or third-party code
   (AGENTS.md: "errors.py has zero imports from fieldkit — any module may
   safely import from it").
2. ``fieldkit/cli_exit.py`` imports only config, shared errors, and the pure
   ``fieldkit.sf.errors`` leaf. The SF package initializer remains docstring-only
   so typed Salesforce diagnostics cannot load optional integration clients.
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


def _assert_cli_exit_import_isolation(tree: ast.Module) -> None:
    """Allow exactly the pure Salesforce leaf through its canonical module alias."""
    imports = _imported_top_level_modules(tree)
    fieldkit_imports = {mod for mod in imports if mod == "fieldkit" or mod.startswith(("fieldkit.", "."))}
    allowed = {"fieldkit.config", "fieldkit.errors", "fieldkit.sf.errors"}
    assert fieldkit_imports <= allowed, (
        f"cli_exit.py imports beyond the allowed set {sorted(allowed)}: {sorted(fieldkit_imports - allowed)}"
    )
    assert not any(
        isinstance(node, ast.ImportFrom)
        and (node.level or (node.module or "").startswith("fieldkit"))
        and any(alias.name == "*" for alias in node.names)
        for node in ast.walk(tree)
    ), "cli_exit.py cannot use wildcard fieldkit imports"
    sf_imports = [
        node
        for node in ast.walk(tree)
        if (isinstance(node, ast.Import) and any(alias.name.startswith("fieldkit.sf") for alias in node.names))
        or (isinstance(node, ast.ImportFrom) and (node.module or "").startswith("fieldkit.sf"))
    ]
    assert len(sf_imports) == 1, "cli_exit.py requires one canonical SF module import"
    canonical = sf_imports[0]
    assert isinstance(canonical, ast.Import) and canonical in tree.body, "SF import must be a top-level module import"
    assert [(alias.name, alias.asname) for alias in canonical.names] == [("fieldkit.sf.errors", "sf_errors")], (
        "SF import must name precisely the canonical errors leaf"
    )


def test_exception_module_isolation_cli_exit_fieldkit_imports_restricted() -> None:
    """Typed SF diagnostics may load their pure leaf, never an optional client."""
    _assert_cli_exit_import_isolation(_parse(_SRC_ROOT / "cli_exit.py"))


_FORBIDDEN_CLI_EXIT_ADDITIONS = (
    "import fieldkit.sf",
    "from fieldkit.sf import errors as sf_errors",
    "from fieldkit import sf",
    "from .sf import errors",
    "import fieldkit.sf.client",
    "import fieldkit.sf.errors.extra",
    "def nested():\n    import fieldkit.sf.client",
    "if True:\n    import fieldkit.sf._transport",
)


@pytest.mark.parametrize("source", _FORBIDDEN_CLI_EXIT_ADDITIONS)
def test_cli_exit_import_allowance_rejects_broader_or_noncanonical_imports(source: str) -> None:
    """Forbidden additions fail the allowlist even with a valid canonical import."""
    with pytest.raises(AssertionError, match="imports beyond the allowed set"):
        _assert_cli_exit_import_isolation(ast.parse("import fieldkit.sf.errors as sf_errors\n" + source))


@pytest.mark.parametrize(
    ("source", "message"),
    (
        ("", "requires one canonical SF module import"),
        ("import fieldkit.sf.errors", "name precisely the canonical errors leaf"),
        ("import fieldkit.sf.errors as renamed", "name precisely the canonical errors leaf"),
        ("from fieldkit.sf.errors import SFAuthError", "top-level module import"),
        ("def nested():\n    import fieldkit.sf.errors as sf_errors", "top-level module import"),
        ("if True:\n    import fieldkit.sf.errors as sf_errors", "top-level module import"),
        ("from fieldkit.sf.errors import *", "cannot use wildcard fieldkit imports"),
        (
            "from fieldkit.config import *\nimport fieldkit.sf.errors as sf_errors",
            "cannot use wildcard fieldkit imports",
        ),
        (
            "from fieldkit.errors import *\nimport fieldkit.sf.errors as sf_errors",
            "cannot use wildcard fieldkit imports",
        ),
    ),
)
def test_cli_exit_import_allowance_rejects_canonical_substitutions(source: str, message: str) -> None:
    with pytest.raises(AssertionError, match=message):
        _assert_cli_exit_import_isolation(ast.parse(source))


@pytest.mark.parametrize("source", _FORBIDDEN_CLI_EXIT_ADDITIONS)
def test_cli_exit_negative_control_detects_an_empty_import_scanner(
    source: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A vacuous scanner must break the forbidden-package regression."""
    monkeypatch.setattr(sys.modules[__name__], "_imported_top_level_modules", lambda _tree: set())
    with pytest.raises((AssertionError, pytest.fail.Exception), match=r"Regex pattern did not match|DID NOT RAISE"):
        test_cli_exit_import_allowance_rejects_broader_or_noncanonical_imports(source)


def test_cli_exit_empty_scanner_control_accepts_only_the_canonical_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    """The scanner mutation alone does not fail the required-alias guard."""
    monkeypatch.setattr(sys.modules[__name__], "_imported_top_level_modules", lambda _tree: set())
    result = _assert_cli_exit_import_isolation(ast.parse("import fieldkit.sf.errors as sf_errors"))
    assert result is None


def _is_docstring(node: ast.stmt) -> bool:
    """Recognize a literal docstring without accepting executable expressions."""
    return isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)


def _assert_sf_errors_leaf_shape(tree: ast.Module) -> None:
    """Accept only the existing local exception declarations and literal guidance."""
    assert len(tree.body) == 9 and _is_docstring(tree.body[0]), "SF leaf module shape changed"
    bases = tree.body[1]
    assert isinstance(bases, ast.ImportFrom), "SF leaf shared bases import changed"
    assert bases.module == "fieldkit.errors" and bases.level == 0, "SF leaf cannot import an optional dependency"
    assert [(alias.name, alias.asname) for alias in bases.names] == [("AuthError", None), ("FieldkitError", None)], (
        "SF leaf shared bases import changed"
    )
    for declaration, (name, base) in zip(
        tree.body[2:8],
        (
            ("SFAuthError", "AuthError"),
            ("SFNotFoundError", "FieldkitError"),
            ("SFDataAccessError", "FieldkitError"),
            ("SFAPIError", "FieldkitError"),
            ("SFConditionalWriteConflict", "FieldkitError"),
            ("SFConditionalWriteOutcomeUnknown", "FieldkitError"),
        ),
        strict=True,
    ):
        assert isinstance(declaration, ast.ClassDef) and declaration.name == name, "SF leaf class identity changed"
        assert not declaration.decorator_list and not declaration.keywords, "SF leaf class must not execute decorators"
        assert len(declaration.bases) == 1, "SF leaf exception base changed"
        superclass = declaration.bases[0]
        assert isinstance(superclass, ast.Name) and superclass.id == base, "SF leaf exception base changed"
        assert len(declaration.body) == 1 and _is_docstring(declaration.body[0]), "SF leaf class must be docstring-only"
    guidance = tree.body[8]
    assert isinstance(guidance, ast.FunctionDef) and guidance.name == "reauth_hint_message", "SF leaf guidance changed"
    assert not guidance.decorator_list, "SF leaf guidance must not execute decorators"
    assert not (
        guidance.args.posonlyargs
        or guidance.args.args
        or guidance.args.kwonlyargs
        or guidance.args.vararg
        or guidance.args.kwarg
        or guidance.args.defaults
        or guidance.args.kw_defaults
    ), "SF leaf guidance must not evaluate arguments or defaults"
    assert isinstance(guidance.returns, ast.Name) and guidance.returns.id == "str", (
        "SF leaf guidance annotation changed"
    )
    assert len(guidance.body) == 2 and _is_docstring(guidance.body[0]), "SF leaf guidance must return a literal"
    returned = guidance.body[1]
    assert isinstance(returned, ast.Return) and isinstance(returned.value, ast.Constant), (
        "SF leaf guidance must return a literal"
    )
    assert isinstance(returned.value.value, str), "SF leaf guidance must return a string"


def _assert_sf_package_shape(tree: ast.Module) -> None:
    """The parent initializer may contain only its literal docstring."""
    assert len(tree.body) == 1 and _is_docstring(tree.body[0]), "SF package initializer must be docstring-only"


def test_sf_exception_leaf_and_package_initializer_remain_pure() -> None:
    """Importing the leaf cannot execute integration code through either source."""
    _assert_sf_errors_leaf_shape(_parse(_SRC_ROOT / "sf/errors.py"))
    _assert_sf_package_shape(_parse(_SRC_ROOT / "sf/__init__.py"))


@pytest.mark.parametrize(
    "addition",
    (
        "import fieldkit.config",
        "import fieldkit.sf.client",
        "import fieldkit.sf._transport",
        "import fieldkit.sf._responses",
        "import httpx",
        "from . import client",
        "from ..config import ConfigError",
        "def nested():\n    import fieldkit.config",
        "if True:\n    import tenacity",
        "__import__('httpx')",
        "exec('import httpx')",
        "eval('__import__(\"httpx\")')",
        "configure()",
        "class Added:\n    configure()",
    ),
)
def test_sf_leaf_rejects_added_imports_and_execution(addition: str) -> None:
    """Extra imports, dynamic imports, and executable additions break the pure leaf."""
    source = (_SRC_ROOT / "sf/errors.py").read_text(encoding="utf-8")
    with pytest.raises(AssertionError, match="SF leaf"):
        _assert_sf_errors_leaf_shape(ast.parse(source + "\n" + addition))


@pytest.mark.parametrize(
    "before,after",
    (
        ("class SFAuthError(AuthError):", "@configure()\nclass SFAuthError(AuthError):"),
        ("class SFAuthError(AuthError):", "class SFAuthError(resolve_base()):"),
        ("class SFAuthError(AuthError):", "class SFAuthError(AuthError):\n    configure()"),
        ("def reauth_hint_message()", "@configure()\ndef reauth_hint_message()"),
        ("def reauth_hint_message()", "def reauth_hint_message(value=configure())"),
        ("def reauth_hint_message()", "def reauth_hint_message(*, value=configure())"),
        ('return "run', 'import fieldkit.config\n    return "run'),
        ('return "run', "return __import__('httpx') or \"run"),
        ('return "run', "return eval('1') or \"run"),
    ),
)
def test_sf_leaf_rejects_execution_inside_existing_definitions(before: str, after: str) -> None:
    """Existing class and guidance slots cannot hide calls, imports, or defaults."""
    source = (_SRC_ROOT / "sf/errors.py").read_text(encoding="utf-8")
    assert before in source
    with pytest.raises(AssertionError, match="SF leaf"):
        _assert_sf_errors_leaf_shape(ast.parse(source.replace(before, after, 1)))


@pytest.mark.parametrize(
    "addition",
    (
        "from fieldkit.sf.errors import SFAPIError",
        "from fieldkit.sf import client",
        "def __getattr__(name):\n    return __import__('fieldkit.sf.client')",
        "configure()",
        "import httpx",
        "@configure()\nclass Client:\n    pass",
    ),
)
def test_sf_package_initializer_rejects_reexports_and_execution(addition: str) -> None:
    """Parent execution, lazy exports, and eager clients cannot bypass leaf checks."""
    source = (_SRC_ROOT / "sf/__init__.py").read_text(encoding="utf-8")
    with pytest.raises(AssertionError, match="SF package initializer"):
        _assert_sf_package_shape(ast.parse(source + "\n" + addition))


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
