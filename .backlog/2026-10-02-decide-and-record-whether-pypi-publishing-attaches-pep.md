---
title: Decide and record whether PyPI publishing attaches PEP 740 attestations
type: enhancement
severity: medium
status: closed
created: 2026-10-02
closed: 2026-10-03
resolution: done
labels: []
---
`pypa/gh-action-pypi-publish` turned PEP 740 attestations on by default in v1.11.0, and the pinned v1.14.2 (`action.yml` at dc37677b declares `attestations` with `default: 'true'`). The `publish_testpypi` and `publish_pypi` jobs in `.github/workflows/release.yml` (main 47504730, steps at :275 and :301) set no `attestations` input and grant `id-token: write`, so the next TestPyPI or PyPI upload will attach Sigstore-signed attestations for the workflow identity. Since v1.12.0 the action also runs a prebuilt image pulled as `ghcr.io/pypa/gh-action-pypi-publish:<commit-sha>` (a registry tag, not a digest) instead of building from the pinned source.

#8's changelog (`changelog.d/update-github-actions-pins.md`) says permissions and gates are unchanged and records no decision on either behaviour; RELEASING.md does not mention attestations.

**Done when:** the release workflow sets `attestations` explicitly to the chosen value, RELEASING.md states what a release publishes (including attestations and the publisher image source), and the 1.0.1 release notes match.
