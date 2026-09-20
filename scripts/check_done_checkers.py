#!/usr/bin/env python3
"""Validate that done-condition checkers are isolated standard-library scripts."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

CHECKER_ROOT = Path(__file__).resolve().parent / "done_checks"

_NETWORK_MODULES = {
    "asyncore",
    "ftplib",
    "http",
    "imaplib",
    "nntplib",
    "poplib",
    "smtplib",
    "socket",
    "socketserver",
    "ssl",
    "telnetlib",
    "urllib",
    "webbrowser",
    "xmlrpc",
}
_PROCESS_MODULES = {"asyncio", "builtins", "ctypes", "multiprocessing", "os", "pty", "runpy", "subprocess"}
_DYNAMIC_IMPORT_CALLS = {"__import__", "importlib.import_module"}
_EVALUATION_CALLS = {"eval", "exec"}
_PROCESS_CALLS = {
    "asyncio.create_subprocess_exec",
    "asyncio.create_subprocess_shell",
    "os.exec",
    "os.execl",
    "os.execle",
    "os.execlp",
    "os.execlpe",
    "os.execv",
    "os.execve",
    "os.execvp",
    "os.execvpe",
    "os.popen",
    "os.posix_spawn",
    "os.posix_spawnp",
    "os.spawnl",
    "os.spawnle",
    "os.spawnlp",
    "os.spawnlpe",
    "os.spawnv",
    "os.spawnve",
    "os.spawnvp",
    "os.spawnvpe",
    "os.system",
}


def _qualified_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _qualified_name(node.value)
        return f"{parent}.{node.attr}" if parent else None
    return None


def validate_checker(path: Path) -> tuple[str, ...]:
    """Return every isolation-policy violation in *path* without importing it."""
    if path.is_symlink() or not path.is_file():
        return (f"{path}: checker must be a regular file",)
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (OSError, SyntaxError, UnicodeError) as exc:
        return (f"{path}: cannot parse checker: {exc}",)

    violations: list[str] = []
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.partition(".")[0]
                aliases[alias.asname or root] = alias.name
                if root not in sys.stdlib_module_names:
                    violations.append(f"{path}:{node.lineno}: non-standard-library import {alias.name!r}")
                elif root in _NETWORK_MODULES:
                    violations.append(f"{path}:{node.lineno}: network import {alias.name!r}")
                elif root == "importlib" or root in _PROCESS_MODULES:
                    violations.append(f"{path}:{node.lineno}: forbidden import {alias.name!r}")
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                violations.append(f"{path}:{node.lineno}: relative imports are forbidden")
                continue
            module = node.module or ""
            root = module.partition(".")[0]
            for alias in node.names:
                aliases[alias.asname or alias.name] = f"{module}.{alias.name}"
            if root not in sys.stdlib_module_names:
                violations.append(f"{path}:{node.lineno}: non-standard-library import {module!r}")
            elif root in _NETWORK_MODULES:
                violations.append(f"{path}:{node.lineno}: network import {module!r}")
            elif root == "importlib" or root in _PROCESS_MODULES:
                violations.append(f"{path}:{node.lineno}: forbidden import {module!r}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith("/"):
            violations.append(f"{path}:{node.lineno}: absolute host path literal is forbidden")

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _qualified_name(node.func)
        if name is None:
            continue
        root, separator, suffix = name.partition(".")
        resolved = aliases.get(root, root)
        if separator:
            resolved = f"{resolved}.{suffix}"
        if resolved in _DYNAMIC_IMPORT_CALLS or name in _DYNAMIC_IMPORT_CALLS:
            violations.append(f"{path}:{node.lineno}: dynamic imports are forbidden")
        elif resolved in _EVALUATION_CALLS or name in _EVALUATION_CALLS:
            violations.append(f"{path}:{node.lineno}: {name} is forbidden")
        elif resolved in _PROCESS_CALLS or resolved.startswith("subprocess."):
            violations.append(f"{path}:{node.lineno}: process launch {resolved!r} is forbidden")

    return tuple(dict.fromkeys(violations))


def validate_all(root: Path = CHECKER_ROOT) -> tuple[str, ...]:
    """Validate each checker directly below *root*."""
    if not root.is_dir():
        return (f"{root}: checker directory does not exist",)
    paths = sorted(root.glob("*.py"))
    if not paths:
        return (f"{root}: no checker files found",)
    return tuple(error for path in paths for error in validate_checker(path))


def main() -> int:
    violations = validate_all()
    if violations:
        for violation in violations:
            print(violation)
        return 1
    print(f"done-checker policy: {len(list(CHECKER_ROOT.glob('*.py')))} checker(s) valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
