#!/usr/bin/env python3
"""Classify worktree changes that require tests, failing closed on uncertainty."""

import argparse
import ast
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING or __package__:
    from scripts.git_worktree import git_environment
else:
    from git_worktree import git_environment

_GIT_TIMEOUT_SECONDS = 30


class _DocstringRemover(ast.NodeTransformer):
    """Remove only leading docstring expressions from AST bodies."""

    @staticmethod
    def _without_docstring(body: list[ast.stmt]) -> list[ast.stmt]:
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            return body[1:]
        return body

    def visit_Module(self, node: ast.Module) -> ast.Module:
        node.body = self._without_docstring(node.body)
        self.generic_visit(node)
        return node

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.ClassDef:
        node.body = self._without_docstring(node.body)
        self.generic_visit(node)
        return node

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.FunctionDef:
        node.body = self._without_docstring(node.body)
        self.generic_visit(node)
        return node

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AsyncFunctionDef:
        node.body = self._without_docstring(node.body)
        self.generic_visit(node)
        return node


def _normalized_tree(source: str) -> str | None:
    """Return source AST without locations or docstrings, or None if invalid."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    normalized = _DocstringRemover().visit(tree)
    return ast.dump(normalized, annotate_fields=True, include_attributes=False)


def _source_changes_semantics(base_source: str, candidate_source: str) -> bool:
    """Return whether two Python sources differ beyond comments and docstrings."""
    base_tree = _normalized_tree(base_source)
    candidate_tree = _normalized_tree(candidate_source)
    if base_tree is None or candidate_tree is None:
        return True
    return base_tree != candidate_tree


def _run_git(repo_root: Path, *arguments: str) -> str | None:
    """Return Git stdout, failing closed when the request cannot be completed."""
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
            env=git_environment(),
        )
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def _changed_paths(base_revision: str, repo_root: Path) -> list[str] | None:
    """Include tracked changes of every type and non-ignored untracked files."""
    tracked = _run_git(repo_root, "diff", "--name-only", "-z", "--no-renames", base_revision, "--")
    staged = _run_git(repo_root, "diff", "--cached", "--name-only", "-z", "--no-renames", base_revision, "--")
    untracked = _run_git(repo_root, "ls-files", "--others", "--exclude-standard", "-z")
    if tracked is None or staged is None or untracked is None:
        return None
    return sorted({path for path in (tracked + staged + untracked).split("\0") if path})


def is_prose_path(path: str) -> bool:
    """Only reader documentation is prose; shipped skills and instructions are not."""
    relative = Path(path)
    if relative.is_absolute() or ".." in relative.parts:
        return False
    return path in {"README.md", "CHANGELOG.md", "CONTRIBUTING.md", "ROADMAP.md", "SECURITY.md", "SUPPORT.md"} or (
        relative.suffix == ".md" and relative.parts[0] in {"docs", "changelog.d"}
    )


def _entry_modes_require_tests(base_revision: str, repo_root: Path, *, candidate_revision: str | None) -> bool:
    """Text equivalence cannot waive executable-mode, symlink or submodule changes."""
    revisions = (base_revision,) if candidate_revision is None else (base_revision, candidate_revision)
    index_options = ((), ("--cached",)) if candidate_revision is None else ((),)
    for index_option in index_options:
        output = _run_git(repo_root, "diff", *index_option, "--raw", "-z", "--no-renames", *revisions, "--")
        if output is None:
            return True
        records = output.split("\0")
        for header in records[:-1:2]:
            fields = header.split()
            if len(fields) != 5 or not fields[0].startswith(":"):
                return True
            old_mode, new_mode = fields[0][1:], fields[1]
            if {old_mode, new_mode} - {"000000", "100644"}:
                return True
    return False


def _git_source(revision: str, path: str, repo_root: Path) -> str | None:
    """Return one tracked source file from a revision, or None if unavailable."""
    return _run_git(repo_root, "show", f"{revision}:{path}")


def _working_tree_source(path: str, repo_root: Path) -> str | None:
    """Return one safe worktree source file, or None when it cannot be read."""
    relative = Path(path)
    if relative.is_absolute() or ".." in relative.parts:
        return None
    source_path = repo_root / relative
    try:
        if (
            source_path.is_symlink()
            or not source_path.is_file()
            or not source_path.resolve().is_relative_to(repo_root.resolve())
        ):
            return None
        return source_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None


def has_test_relevant_changes(base_revision: str, repo_root: Path) -> bool:
    """Require tests for all changes except reader prose and Python comments/docstrings."""
    paths = _changed_paths(base_revision, repo_root)
    if paths is None or _entry_modes_require_tests(base_revision, repo_root, candidate_revision=None):
        return True
    for path in paths:
        if _worktree_entry_requires_tests(path, repo_root):
            return True
        if is_prose_path(path):
            continue
        if not path.endswith(".py"):
            return True
        base_source = _git_source(base_revision, path, repo_root)
        staged_source = _git_source("", path, repo_root)
        candidate_source = _working_tree_source(path, repo_root)
        if base_source is None or staged_source is None or candidate_source is None:
            return True
        if _source_changes_semantics(base_source, staged_source):
            return True
        if _source_changes_semantics(base_source, candidate_source):
            return True
    return False


def _worktree_entry_requires_tests(path: str, repo_root: Path) -> bool:
    """Reject exceptional local entries, including ones absent from the Git index."""
    try:
        mode = (repo_root / path).lstat().st_mode
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return not stat.S_ISREG(mode) or bool(mode & 0o111)


def has_non_prose_changes(base_revision: str, candidate_revision: str, repo_root: Path) -> bool:
    """Classify the exact CI revision range without trusting worktree or index bytes."""
    output = _run_git(repo_root, "diff", "--name-only", "-z", "--no-renames", base_revision, candidate_revision, "--")
    if output is None or _entry_modes_require_tests(base_revision, repo_root, candidate_revision=candidate_revision):
        return True
    return any(not is_prose_path(path) for path in output.split("\0") if path)


def requires_full_test_suite(base_revision: str, repo_root: Path, *, candidate_revision: str | None) -> bool:
    """Only directly collected test edits can safely use import-graph selection.

    Application code, fixtures, plugins and scripts can be consumed dynamically;
    a Python suffix alone does not prove that Tach can discover every consumer.
    """
    if candidate_revision is None:
        unstaged = _run_git(repo_root, "diff", "--name-only", "-z", "--no-renames", "--")
        if unstaged is None or any(not is_prose_path(path) for path in unstaged.split("\0") if path):
            return True
        paths = _changed_paths(base_revision, repo_root)
    else:
        output = _run_git(
            repo_root, "diff", "--name-only", "-z", "--no-renames", base_revision, candidate_revision, "--"
        )
        paths = None if output is None else [path for path in output.split("\0") if path]
    if paths is None or _entry_modes_require_tests(base_revision, repo_root, candidate_revision=candidate_revision):
        return True
    for path in paths:
        if re.fullmatch(r"[A-Za-z0-9_./-]+", path) is None:
            return True
        if candidate_revision is None and _worktree_entry_requires_tests(path, repo_root):
            return True
        relative = Path(path)
        directly_collected_test = (
            relative.parent == Path("tests") and relative.name.startswith("test_") and relative.suffix == ".py"
        )
        if not is_prose_path(path) and not directly_collected_test:
            return True
    return False


def main() -> None:
    """Emit the CI changes output using only the standard library before bootstrap."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    parser.add_argument("--candidate", required=True)
    args = parser.parse_args()
    requires_tests = has_non_prose_changes(args.base, args.candidate, Path.cwd())
    sys.stdout.write(f"code={str(requires_tests).lower()}\n")


if __name__ == "__main__":
    main()
