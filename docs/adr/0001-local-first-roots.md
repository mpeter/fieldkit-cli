---
status: Accepted
applies_to: fieldkit-cli
---

# Local-first roots

## Context

fieldkit needs to keep installed code, user workspace content, and generated
runtime data distinct.

## Decision

Use the configuration accessors for these three responsibilities:
`get_fieldkit_root()` for an available source checkout,
`get_fieldkit_home()` for the user workspace, and `get_fieldkit_data()` for
generated runtime artifacts. Installed packages use bundled resources when no
source checkout is available. Do not derive one root from the current working
directory or assume the three paths are physically separate.

## Consequences

Commands remain portable across worktrees, and backup or cleanup policy can
distinguish user content from generated data. Runtime data defaults to the
workspace's `data` directory unless configured elsewhere; the roots describe
ownership, not a promise of separate disks or directories. Validate
user-derived output paths at the write boundary. See the
[product model](../concepts.md#persistent-roots-and-harness-scratch) for
documented configuration, data, and scratch locations.
