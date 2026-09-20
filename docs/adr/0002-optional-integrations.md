---
status: Accepted
applies_to: fieldkit-cli
---

# Optional integrations

## Context

A useful base installation must not require every provider SDK or credential.

## Decision

Load an integration only when its command is invoked and make installation and
authentication failures explicit.

## Consequences

Offline workflows remain available by default, while configured integrations
can add capability without silently changing the base dependency boundary.
