#!/usr/bin/env python3
"""Fail closed when the current committed tree cannot safely become public."""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

if __package__:
    from scripts import export_public_tree, git_worktree, public_tree_scan
else:
    import export_public_tree
    import git_worktree
    import public_tree_scan


def check(repo: Path) -> public_tree_scan.ScanReport:
    """Export, verify, and scan exactly the committed tree under the public policy."""
    revision = git_worktree.head_revision(repo)
    git_worktree.require_clean_worktree(repo)
    policy = repo / export_public_tree._POLICY_PATH
    with tempfile.TemporaryDirectory(prefix="fieldkit-public-tree-") as temporary:
        root = Path(temporary)
        destination = root / "export"
        manifest = root / "manifest.json"
        export_public_tree.export_tree(repo, revision, policy, destination, manifest)
        report = public_tree_scan.scan_public_tree(repo, destination, manifest, policy)
    git_worktree.require_clean_worktree(repo)
    if git_worktree.head_revision(repo) != revision or report.source_commit != revision:
        raise ValueError("candidate revision changed during public-tree verification")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path())
    args = parser.parse_args(argv)
    try:
        report = check(args.repo.resolve())
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(f"Public tree safety: ERROR: {error}", file=sys.stderr)
        return 2
    if not report.content_ok:
        print("Public tree safety: FAIL", file=sys.stderr)
        for finding in report.findings:
            print(f"{finding.rule_id}: {finding.path}:{finding.line or 0}", file=sys.stderr)
        return 1
    print(f"Public tree safety: PASS ({report.scanned_entries} classified entries; commit {report.source_commit})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
