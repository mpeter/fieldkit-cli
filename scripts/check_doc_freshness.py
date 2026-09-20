"""Check doc freshness by comparing last_reviewed dates against git history.

Reads last_reviewed and covers: YAML frontmatter from all docs/**/*.md files
(excluding auto-generated ones). For each covers: path, warns if the source
file has commits newer than the doc's last_reviewed date.

Exits 0 always — this is a warning-only tool, not a blocking gate.
"""

import re
import subprocess
import sys
from pathlib import Path

# Auto-generated docs are CI-gated separately; skip them here.
_EXCLUDED_DOCS = {"cli-reference.md", "dependency-map.md"}

# Regex to extract YAML frontmatter block (between --- delimiters).
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)

# Regex to extract last_reviewed value from frontmatter text.
_LAST_REVIEWED_RE = re.compile(r"^last_reviewed:\s*(.+)$", re.MULTILINE)

# Regex to extract covers: list items (lines starting with "  - ").
_COVERS_ITEM_RE = re.compile(r"^\s+-\s+(.+)$", re.MULTILINE)

# Regex to validate last_reviewed date format (YYYY-MM-DD).
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _repo_root() -> Path:
    """Return the repository root (parent of this script's directory)."""
    return Path(__file__).parent.parent.resolve()


def _is_repo_relative(path_str: str, repo_root: Path) -> bool:
    """Return True if path_str resolves to a descendant of repo_root.

    Rejects absolute paths and paths that escape the repo root via '..'.
    """
    if Path(path_str).is_absolute():
        return False
    resolved = (repo_root / path_str).resolve()
    try:
        resolved.relative_to(repo_root)
        return True
    except ValueError:
        return False


def _parse_frontmatter(doc_path: Path) -> tuple[str | None, list[str]]:
    """Parse last_reviewed and covers: list from a doc's YAML frontmatter.

    Returns (last_reviewed_str, covers_paths). Returns (None, []) on any
    parse failure, printing a warning to stderr.
    """
    try:
        text = doc_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"WARNING: cannot read {doc_path}: {exc}", file=sys.stderr)
        return None, []

    fm_match = _FRONTMATTER_RE.match(text)
    if not fm_match:
        # No frontmatter — silently skip (many docs may not have it yet).
        return None, []

    fm_text = fm_match.group(1)

    # Detect malformed YAML: unbalanced quotes or tabs in key positions.
    # We use a lightweight heuristic rather than a full YAML parser (stdlib only).
    for line in fm_text.splitlines():
        stripped = line.lstrip()
        if stripped and ":" in stripped:
            _key, _, val = stripped.partition(":")
            # Unbalanced quotes in value indicate malformed YAML.
            val = val.strip()
            if val.count('"') % 2 != 0 or val.count("'") % 2 != 0:
                print(
                    f"WARNING: malformed YAML frontmatter in {doc_path} — skipping",
                    file=sys.stderr,
                )
                return None, []

    lr_match = _LAST_REVIEWED_RE.search(fm_text)
    if not lr_match:
        return None, []

    last_reviewed = lr_match.group(1).strip()

    if not _DATE_RE.match(last_reviewed):
        print(
            f"WARNING: {doc_path}: invalid last_reviewed format '{last_reviewed}' (expected YYYY-MM-DD), skipping",
            file=sys.stderr,
        )
        return None, []

    # Extract covers: block — lines after "covers:" that start with "  - ".
    covers_section = re.search(r"^covers:\s*\n((?:\s+-\s+.+\n?)*)", fm_text, re.MULTILINE)
    covers: list[str] = []
    if covers_section:
        covers = [m.group(1).strip() for m in _COVERS_ITEM_RE.finditer(covers_section.group(0))]

    return last_reviewed, covers


def _check_staleness(doc_path: Path, last_reviewed: str, covers_path: str, repo_root: Path) -> None:
    """Warn if covers_path has commits newer than last_reviewed.

    Validates the path is repo-relative before invoking git log.
    """
    if not _is_repo_relative(covers_path, repo_root):
        print(
            f"WARNING: {doc_path.name}: covers path '{covers_path}' is not repo-relative — skipping",
            file=sys.stderr,
        )
        return

    abs_path = repo_root / covers_path
    if not abs_path.exists():
        print(
            f"WARNING: {doc_path.name}: covers path '{covers_path}' does not exist in repo",
            file=sys.stderr,
        )
        return

    try:
        result = subprocess.run(
            ["git", "log", f"--since={last_reviewed}", "--", covers_path],
            capture_output=True,
            text=True,
            cwd=repo_root,
            check=False,
        )
    except FileNotFoundError:
        print(
            "WARNING: git not found — cannot check doc freshness; skipping all staleness checks",
            file=sys.stderr,
        )
        sys.exit(0)

    if result.returncode != 0:
        print(
            f"WARNING: git log failed for '{covers_path}' (exit {result.returncode}) — skipping",
            file=sys.stderr,
        )
        return

    if result.stdout.strip():
        print(
            f"WARNING: {doc_path.name} may be stale — '{covers_path}' has commits since {last_reviewed}",
            file=sys.stderr,
        )


def main() -> None:
    """Entry point: scan docs/ and report staleness warnings."""
    repo_root = _repo_root()
    docs_dir = repo_root / "docs"

    if not docs_dir.is_dir():
        print(f"WARNING: docs/ directory not found at {docs_dir}", file=sys.stderr)
        sys.exit(0)

    doc_files = [p for p in docs_dir.rglob("*.md") if p.name not in _EXCLUDED_DOCS]

    for doc_path in sorted(doc_files):
        last_reviewed, covers = _parse_frontmatter(doc_path)
        if last_reviewed is None:
            continue
        if not covers:
            continue
        for covers_path in covers:
            _check_staleness(doc_path, last_reviewed, covers_path, repo_root)


if __name__ == "__main__":
    main()
