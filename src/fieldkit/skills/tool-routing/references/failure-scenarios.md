# Routing failure scenarios

## CLI is absent

Name the missing executable and setup dependency. Do not silently substitute a
different source. Inspect a configured authorized alternative only when it covers
the exact capability and its use is permitted for this task.

## Authentication or identity fails

Inspect the selected route's status and report the failing service or missing
approved identity without exposing credentials. Do not reset OAuth, switch to a
personal identity, borrow another workspace's registration, or run bulk registration
from the task session. Managed authentication repair needs its own authorization.

## Write fails

Keep the exact API error and exit code. Read back the affected resource before
claiming success. For validation errors, inspect the exact method schema before
retrying. Partial writes must be reported explicitly. Do not widen a read-only MCP
group or change transport to bypass missing write authority.

## Browser profile is ambiguous

Run `chrome-use browsers` and pin subsequent actions to the authorized profile.
If that identity cannot be established, report it as unavailable.

## Configured MCP route is unavailable

Inspect the selected route's configuration, loaded tools, and upstream status.
Check gateway registration only when that route uses a gateway; a running gateway
does not prove a tool is available or authenticated. Report the missing capability
and setup dependency. Do not invent direct connections or revive retired aliases.

## Retired vault graph request

Backlink, outlink, orphan, and connection-path graph operations have no maintained
route. Offer native exact search or `qmd` retrieval as labeled alternatives only
when they answer the underlying question; do not recreate a removed vault endpoint.
