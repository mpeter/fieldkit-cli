---
title: Make the public-tree safety check cover uncommitted changes or say it does not
type: enhancement
severity: low
status: open
created: 2026-10-02
labels: []
---
`scripts/check_public_tree_safety.py` always exports and scans the committed `HEAD` (`_head(repo)` at line 35 on main 47504730; the only option is `--repo`), and `export_public_tree.export_tree` requires a commit (`rev-parse --verify <rev>^{commit}`). A prepared but uncommitted change, or a PR head checked out elsewhere, cannot be scanned without making a commit first; reviewing PR #11 locally needed a detached checkout of the PR head as a stand-in.

**Done when:** the check accepts an explicit revision (for example `--revision <commit>`), defaults to `HEAD`, and has a test covering a non-HEAD revision.
