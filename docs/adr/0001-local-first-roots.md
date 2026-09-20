---
status: Accepted
applies_to: fieldkit-cli
---

# Local-first roots

## Context

fieldkit needs to keep installed code, user workspace content, and generated
runtime data distinct.

## Decision

Use configured roots for these three content types and resolve them through the
configuration layer rather than a checkout or home-directory assumption.

## Consequences

Commands remain portable across worktrees, backups can distinguish user content
from disposable data, and new writes have one validation boundary.
