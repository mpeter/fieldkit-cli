---
title: Make the public-tree safety check cover uncommitted changes or say it does not
type: enhancement
severity: low
status: open
created: 2026-10-02
labels: []
---
`scripts/check_public_tree_safety.py` always exports and scans the committed `HEAD` (`_head(repo)` at line 35 on main 47504730; the only option is `--repo`), and `export_public_tree.export_tree` requires a commit (`rev-parse --verify <rev>^{commit}`). A prepared but uncommitted change, or a PR head checked out elsewhere, cannot be scanned without making a commit first; reviewing PR #11 locally needed a detached checkout of the PR head as a stand-in.

Passing a revision through is not enough on its own. `export_public_tree._load_committed_policy` (`scripts/export_public_tree.py:304-321` on main 69c2177a) requires the working-tree `docs/release-readiness/public-tree-policy.json` to be byte-identical to the policy committed at the scanned revision, and otherwise fails with `public-tree policy differs from committed candidate`. A `--revision` flag would therefore work only when the policy is unchanged, and would fail closed for a policy-changing PR such as #43, the case that motivated this item. Doing this properly means deciding whether the export may read the policy from the scanned commit alone; that changes a release control and needs maintainer review.

**Done when:** the check accepts an explicit revision (for example `--revision <commit>`), defaults to `HEAD`, and has a test covering a non-HEAD revision.
