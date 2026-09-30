---
status: Accepted
applies_to: fieldkit-cli
---

# Optional integrations

## Context

A useful base installation must not require every provider SDK or credential.

## Decision

Keep provider packages out of the base dependency set. The CLI dispatcher loads
command modules on selection; commands that need an optional profile check its
dependencies at the execution boundary. Missing packages and authentication
failures must remain explicit, actionable failures. Offer a documented local
or no-LLM path where the workflow supports one.

## Consequences

Offline workflows remain available by default, while configured integrations
can add capability without silently changing the base dependency boundary.
Command discovery can describe an optional command without importing its
provider. The base package does not promise that every provider operation works
without its profile or credentials. See [integration profiles](../integrations.md)
for installation and authentication prerequisites.
