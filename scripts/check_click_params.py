#!/usr/bin/env python3
"""check_click_params.py — Detect Click CLI options/arguments declared but never used.

Finds @click.option and @click.argument decorators whose parameter never appears
in the function body — broken CLI contracts where --help shows a flag that does nothing.

Usage:
    python3 scripts/check_click_params.py [path ...]

    path can be a .py file or a directory (recursed). Defaults to src/fieldkit/commands/.

Exit codes:
    0 — no issues found
    1 — one or more unimplemented parameters detected
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path


def _has_expose_value_false(dec: ast.Call) -> bool:
    """Return True if the decorator has expose_value=False (param not passed to function)."""
    for kw in dec.keywords:
        if kw.arg == "expose_value" and isinstance(kw.value, ast.Constant) and kw.value.value is False:
            return True
    return False


def _derive_param_name(dec: ast.Call) -> str | None:
    """Extract the Python parameter name from a @click.option or @click.argument decorator.

    Returns None for options with expose_value=False — those are handled by callbacks
    and intentionally absent from the function signature.
    """
    # expose_value=False options are consumed by callbacks, not the function signature.
    if _has_expose_value_false(dec):
        return None

    pos_args = [a for a in dec.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
    if not pos_args:
        return None

    first = pos_args[0].value

    # @click.option("--foo-bar", "baz_name") or ("--foo", "-f", "baz_name") —
    # the first non-flag string positional is the explicit param name
    for pa in pos_args[1:]:
        if not pa.value.startswith("-"):
            return pa.value

    # @click.option("--foo-bar") — derive from flag name
    if first.startswith("--"):
        return first.lstrip("-").replace("-", "_")

    # @click.option("-f") — short flag only, skip
    if first.startswith("-"):
        return None

    # @click.argument("NAME") — positional arg, lower-cased
    return first.lower().replace("-", "_")


def _is_click_decorator(dec: ast.expr) -> bool:
    if not isinstance(dec, ast.Call):
        return False
    func = dec.func
    return isinstance(func, ast.Attribute) and func.attr in ("option", "argument")


def _has_tracking_comment(lines: list[str], lineno: int, param: str) -> bool:
    """Check for an intentional TODO with an issue reference near the decorator."""
    window = lines[max(0, lineno - 3) : lineno + 3]
    combined = " ".join(window).lower()
    return (
        param.lower() in combined
        and ("todo" in combined or "fixme" in combined)
        and "#" in combined
        and any(c.isdigit() for c in combined)
    )


def check_file(filepath: str | Path) -> list[tuple[str, int, str, str]]:
    """Return list of (filepath, lineno, param_name, reason) for unimplemented params."""
    path = Path(filepath)
    src = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []

    lines = src.splitlines()
    issues: list[tuple[str, int, str, str]] = []

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        click_params: list[tuple[str, int]] = []
        for dec in node.decorator_list:
            if not _is_click_decorator(dec):
                continue
            param_name = _derive_param_name(dec)  # type: ignore[arg-type]
            if param_name:
                click_params.append((param_name, dec.lineno))

        if not click_params:
            continue

        # Collect names from function signature
        sig_params = {a.arg for a in node.args.args + node.args.kwonlyargs}
        if node.args.vararg:
            sig_params.add(node.args.vararg.arg)
        if node.args.kwarg:
            sig_params.add(node.args.kwarg.arg)

        # Get body text (exclude the def line itself)
        body_start = node.body[0].lineno - 1
        body_end = node.end_lineno or len(lines)
        body_lines = lines[body_start:body_end]
        body_text = "\n".join(body_lines)

        for param_name, dec_lineno in click_params:
            # Not in signature at all
            if param_name not in sig_params:
                if not _has_tracking_comment(lines, dec_lineno, param_name):
                    issues.append((str(path), dec_lineno, param_name, "not in function signature"))
                continue

            # In signature — check if used anywhere in body beyond just receiving it
            pattern = r"\b" + re.escape(param_name) + r"\b"
            uses = re.findall(pattern, body_text)

            # body_text starts at the first statement — the def line is excluded.
            # Any match here is a genuine body use.
            body_uses = len(uses)

            # Also check for **kwargs consumption
            has_kwargs = node.args.kwarg is not None
            if has_kwargs:
                # If kwargs is used to consume params dynamically, don't flag
                kwargs_name = node.args.kwarg.arg  # type: ignore[union-attr]
                if re.search(r"\b" + re.escape(kwargs_name) + r"\b", body_text):
                    continue

            if body_uses <= 0 and not _has_tracking_comment(lines, dec_lineno, param_name):
                issues.append((str(path), dec_lineno, param_name, "declared but never used in body"))

    return issues


def scan(paths: list[str | Path]) -> list[tuple[str, int, str, str]]:
    all_issues: list[tuple[str, int, str, str]] = []
    for p in paths:
        path = Path(p)
        if path.is_file() and path.suffix == ".py":
            all_issues.extend(check_file(path))
        elif path.is_dir():
            for pyfile in sorted(path.rglob("*.py")):
                if "__pycache__" in str(pyfile) or "test" in pyfile.name:
                    continue
                all_issues.extend(check_file(pyfile))
    return all_issues


def main(argv: list[str]) -> int:
    targets = argv[1:] if len(argv) > 1 else ["src/fieldkit/commands/"]
    issues = scan(targets)

    if not issues:
        print("✓ No unimplemented Click parameters found.")
        return 0

    root = Path(__file__).resolve().parent.parent
    print(f"Found {len(issues)} unimplemented Click parameter(s):\n")
    for filepath, lineno, param, reason in issues:
        try:
            rel = Path(filepath).relative_to(root)
        except ValueError:
            rel = Path(filepath)
        flag = "--" + param.replace("_", "-")
        print(f"  {rel}:{lineno}  {flag}  ({reason})")

    print(
        f"\n{'─' * 60}\n"
        f"Each listed parameter appears in --help but has no effect.\n"
        f"Options: implement it, remove the decorator, or add a\n"
        f"# TODO(#NNN): not yet implemented comment to suppress.\n"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
