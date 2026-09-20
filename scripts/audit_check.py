#!/usr/bin/env python3
"""audit_check.py — Verify codebase against audit-derived standards.

Runs a set of grep/AST checks that codify the findings from the June 2026
codebase audit. Each check has a short ID (e.g. A01) and a shell-verifiable
command. Exits non-zero if any check fails.

Usage:
    uv run python scripts/audit_check.py          # run all checks
    uv run python scripts/audit_check.py --list   # list checks without running
"""

import ast
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent


# ---------------------------------------------------------------------------
# Check definitions
# ---------------------------------------------------------------------------


def _rg(repo_root: Path, pattern: str, *paths: str) -> list[str]:
    """Return non-empty lines matching pattern across given paths (Python reimpl of rg)."""
    compiled = re.compile(pattern)
    results: list[str] = []
    for path_str in paths:
        root = repo_root / path_str if not Path(path_str).is_absolute() else Path(path_str)
        for py_file in sorted(root.rglob("*.py")):
            try:
                for i, line in enumerate(py_file.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                    if compiled.search(line):
                        results.append(f"{py_file.relative_to(repo_root)}:{i}: {line.rstrip()}")
            except OSError:
                continue
    return results


def check_A01_no_bare_exception_subclasses(repo_root: Path = REPO_ROOT) -> list[str]:
    """A01: Domain exception classes must inherit from FieldkitError, not bare Exception."""
    hits = _rg(repo_root, r"class \w+Error\(Exception\)", "src/fieldkit")
    # Allow errors.py (the base hierarchy) and commands/ (CLI exceptions are acceptable)
    return [h for h in hits if "errors.py" not in h and "commands/" not in h]


def check_A02_no_future_annotations(repo_root: Path = REPO_ROOT) -> list[str]:
    """A02: No file should contain 'from __future__ import annotations'."""
    return _rg(repo_root, r"^from __future__ import annotations", "src/fieldkit", "tests", "hooks")


def check_A03_no_subprocess_without_timeout(repo_root: Path = REPO_ROOT) -> list[str]:
    """A03: All subprocess.run() calls must have a timeout= argument."""
    results: list[str] = []
    for path_str in ("src/fieldkit", "hooks"):
        root = repo_root / path_str
        for py_file in sorted(root.rglob("*.py")):
            try:
                text = py_file.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            lines = text.splitlines()
            for i, line in enumerate(lines):
                # Skip comments and docstrings
                stripped = line.lstrip()
                if stripped.startswith("#") or stripped.startswith('"""') or stripped.startswith("'''"):
                    continue
                if "subprocess.run(" not in line:
                    continue
                # Check the next 25 lines for timeout= (some calls span many args)
                window = "\n".join(lines[i : i + 25])
                if "timeout=" not in window:
                    results.append(f"{py_file.relative_to(repo_root)}:{i + 1}: {line.rstrip()}")
    return results


def check_A04_no_hardcoded_org_domain(repo_root: Path = REPO_ROOT) -> list[str]:  # pii-guard: ignore
    """A04: No live code should construct hardcoded org email addresses."""
    hits = _rg(  # pii-guard: ignore
        repo_root,
        r"@redhat-internal\.com",  # pii-guard: ignore — org domain in audit pattern
        "src/fieldkit",  # pii-guard: ignore — org domain in audit pattern
    )
    results = []
    for h in hits:
        if "pii-guard: ignore" in h:
            continue
        # Extract the line content (after file:line: prefix)
        parts = h.split(":", 2)
        line_content = parts[2] if len(parts) >= 3 else ""
        stripped = line_content.lstrip()
        # Skip comments and docstring lines
        if stripped.startswith("#") or stripped.startswith('"""') or stripped.startswith("'''"):
            continue
        # Skip lines that are clearly in string literals used only as documentation
        # (heuristic: line contains the pattern but also 'vs' or 'instead' or similar)
        if re.search(r"\bvs\b|\binstead\b|\brather\b|\beg\b|\be\.g\b", line_content, re.IGNORECASE):
            continue
        results.append(h)
    return results


def check_A05_sf_retry_covers_gateway_errors(repo_root: Path = REPO_ROOT) -> list[str]:
    """A05: Shared retry policy must include 500, 502, 504 (gateway errors)."""
    retry_policy = repo_root / "src" / "fieldkit" / "config" / "retry.py"
    try:
        text = retry_policy.read_text(encoding="utf-8")
    except OSError:
        return ["src/fieldkit/config/retry.py: file not found"]
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return [f"src/fieldkit/config/retry.py: cannot parse retry policy: {exc.msg}"]

    assignments = [
        node.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "RETRY_TRANSIENT_STATUSES" for target in node.targets)
    ]
    if not assignments:
        return ["src/fieldkit/config/retry.py: RETRY_TRANSIENT_STATUSES not found"]
    if len(assignments) != 1:
        return ["src/fieldkit/config/retry.py: RETRY_TRANSIENT_STATUSES must have exactly one assignment"]

    value = assignments[0]
    if not (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id == "frozenset"
        and len(value.args) == 1
        and not value.keywords
        and isinstance(value.args[0], ast.Set)
    ):
        return ["src/fieldkit/config/retry.py: RETRY_TRANSIENT_STATUSES must be a frozenset of integer literals"]
    elements = value.args[0].elts
    statuses: set[int] = set()
    for element in elements:
        if not isinstance(element, ast.Constant) or type(element.value) is not int:
            return ["src/fieldkit/config/retry.py: RETRY_TRANSIENT_STATUSES must be a frozenset of integer literals"]
        statuses.add(element.value)
    missing = {500, 502, 504} - statuses
    if missing:
        return [f"src/fieldkit/config/retry.py: RETRY_TRANSIENT_STATUSES missing {sorted(missing)}"]
    return []


def check_A06_no_domain_sys_exit(repo_root: Path = REPO_ROOT) -> list[str]:
    """A06: Domain modules must not call sys.exit() or raise SystemExit directly."""
    exemptions = {"cli_exit.py", "__main__.py"}
    pattern = re.compile(r"\bsys\.exit\b|\braise SystemExit\b")
    results: list[str] = []

    root = repo_root / "src" / "fieldkit"
    for py_file in sorted(root.rglob("*.py")):
        rel = str(py_file.relative_to(repo_root))
        if "commands/" in rel:
            continue
        if any(ex in rel for ex in exemptions):
            continue
        try:
            text = py_file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        # Remove string literals and comments before scanning
        # Simple approach: remove triple-quoted strings then single-quoted strings
        cleaned = re.sub(r'""".*?"""', '""', text, flags=re.DOTALL)
        cleaned = re.sub(r"'''.*?'''", "''", cleaned, flags=re.DOTALL)
        cleaned = re.sub(r'"[^"\n]*"', '""', cleaned)
        cleaned = re.sub(r"'[^'\n]*'", "''", cleaned)
        cleaned = re.sub(r"#.*$", "", cleaned, flags=re.MULTILINE)
        for i, line in enumerate(cleaned.splitlines(), 1):
            if pattern.search(line):
                original_line = text.splitlines()[i - 1] if i <= len(text.splitlines()) else ""
                results.append(f"{rel}:{i}: {original_line.rstrip()}")
    return results


def check_A07_pursuit_frontmatter_sf_fields_typed(repo_root: Path = REPO_ROOT) -> list[str]:
    """A07: PursuitFrontmatter sf_* fields must not use Any | None (use concrete types)."""
    models = repo_root / "src" / "fieldkit" / "pursuit" / "models.py"
    try:
        text = models.read_text(encoding="utf-8")
    except OSError:
        return ["src/fieldkit/pursuit/models.py: file not found"]
    hits = []
    for i, line in enumerate(text.splitlines(), 1):
        # Match sf_* field definitions typed as Any | None
        if re.search(r"sf_\w+.*:\s*Any\s*\|", line):
            hits.append(f"src/fieldkit/pursuit/models.py:{i}: {line.rstrip()}")
    return hits


def check_A08_prompt_injection_guards(repo_root: Path = REPO_ROOT) -> list[str]:
    """A08: LLM prompt sites must use wrap_user_data() / UNTRUSTED_DATA_PREAMBLE."""
    # Each entry: (file_rel, required_symbol) — the file must import the symbol
    # as evidence that it has been audited and hardened.
    required_guards: list[tuple[str, str]] = [
        ("commands/brief/main.py", "wrap_user_data"),
        ("ingest/pipeline.py", "wrap_user_data"),
        ("commands/pipeline/render.py", "wrap_user_data"),
    ]
    hits = []
    for rel, symbol in required_guards:
        path = repo_root / "src" / "fieldkit" / rel
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            hits.append(f"src/fieldkit/{rel}: file not found")
            continue
        if symbol not in text:
            hits.append(f"src/fieldkit/{rel}: missing '{symbol}' — prompt injection guard absent")
    return hits


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

CHECKS = [
    check_A01_no_bare_exception_subclasses,
    check_A02_no_future_annotations,
    check_A03_no_subprocess_without_timeout,
    check_A04_no_hardcoded_org_domain,
    check_A05_sf_retry_covers_gateway_errors,
    check_A06_no_domain_sys_exit,
    check_A07_pursuit_frontmatter_sf_fields_typed,
    check_A08_prompt_injection_guards,
]


def main() -> int:
    if "--list" in sys.argv:
        for fn in CHECKS:
            check_id = fn.__name__.split("_")[1].upper()
            doc = fn.__doc__.splitlines()[0].strip() if fn.__doc__ else fn.__name__
            print(f"  {check_id}: {doc}")
        return 0

    failures = 0
    for fn in CHECKS:
        check_id = fn.__name__.split("_")[1].upper()
        doc = fn.__doc__.splitlines()[0].strip() if fn.__doc__ else fn.__name__
        violations = fn(REPO_ROOT)
        if violations:
            print(f"FAIL [{check_id}] {doc}")
            for v in violations:
                print(f"       {v}")
            failures += 1
        else:
            print(f"pass [{check_id}] {doc}")

    print()
    if failures:
        print(f"audit-check: {failures} check(s) failed.")
        return 1
    print("audit-check: all checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
