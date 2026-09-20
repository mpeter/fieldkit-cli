#!/usr/bin/env python3
"""Classify Python changes that require impact-selected tests."""

import ast
import subprocess
from pathlib import Path

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
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def _changed_python_paths(base_revision: str, candidate_revision: str, repo_root: Path) -> list[str] | None:
    """Return Python paths changed from the base through the current worktree."""
    del candidate_revision
    output = _run_git(repo_root, "diff", "--name-only", "--diff-filter=ACMRD", base_revision, "--")
    if output is None:
        return None
    return [path for path in output.splitlines() if path.endswith(".py")]


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
        return source_path.read_text(encoding="utf-8")
    except OSError:
        return None


def has_semantic_python_changes(base_revision: str, candidate_revision: str, repo_root: Path) -> bool:
    """Return True unless all changed Python sources are comments/docstrings only."""
    paths = _changed_python_paths(base_revision, candidate_revision, repo_root)
    if paths is None:
        return True
    for path in paths:
        base_source = _git_source(base_revision, path, repo_root)
        candidate_source = _working_tree_source(path, repo_root)
        if base_source is None or candidate_source is None:
            return True
        if _source_changes_semantics(base_source, candidate_source):
            return True
    return False
