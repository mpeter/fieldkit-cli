# Public CLI routes

## fieldkit

Use the installed `fieldkit` CLI for behavior listed by
`fieldkit commands --json`. Inspect the selected leaf's `--help`, account scope,
write class, dry-run behavior, and confirmation flag before use. A command absent
from the registry is not a hidden fieldkit capability.

## Git and GitHub

Use Git for local repository status, diffs, history, branches, and worktrees. Use
the separately installed `gh` CLI for GitHub issues, pull requests, releases,
workflow state, API calls, and public code search when its current help supports
the operation.

Run `gh auth status` before authenticated GitHub work. Repository discovery does
not authorize a push, merge, release, settings change, or issue mutation. Confirm
the owner and repository immediately before every external write, then read the
affected object or hosted revision back.

## Other external clients

fieldkit does not select or install a browser-automation, Slack, web-search, or
knowledge-service client. If the operator has configured one, inspect that
client's current help and identity before use. Otherwise report the capability
as unavailable. Do not turn a maintainer's local executable name into a public
fieldkit prerequisite.
