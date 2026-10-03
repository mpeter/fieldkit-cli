---
title: Align the release promotion evidence with the pinned PyPI publish action
type: bug
severity: high
status: closed
created: 2026-10-02
closed: 2026-10-03
resolution: done
labels: []
---
The release workflow publishes with `pypa/gh-action-pypi-publish@dc37677b2e1c63e2034f94d8a5b11f265b73ba33` (v1.14.2, `.github/workflows/release.yml:275,301`, since #8 / d30c4963), but the promotion evidence still records the v1.9.0 publisher `pypa/gh-action-pypi-publish@ec4db0b4ddc65acdf4bff5fa45ac92d78b56bdf0` in three places on main 47504730:

- `scripts/release_promotion_evidence.py:32` (`_BOUNDARY_ACTIONS["publish_pypi"]`)
- `docs/release-readiness/release-promotion-evidence.schema.json:93` (`const`)
- `.github/workflows/release.yml:468` (unavailable-evidence fallback block)

A release run would therefore produce promotion evidence naming an action that did not run. No gate catches it: `scripts/release_workflow_policy.py` checks only that each `uses:` is an allowed action with a full SHA, and never compares the publish steps with `_BOUNDARY_ACTIONS`. The same drift will recur on every publisher bump.

**Done when:** the evidence constant, schema `const` and fallback block name the publisher revision the publish steps use, and a release-policy check fails when a boundary action in the promotion evidence differs from the corresponding workflow `uses:` reference.
