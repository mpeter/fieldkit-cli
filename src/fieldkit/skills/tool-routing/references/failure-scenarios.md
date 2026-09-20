# Routing failure scenarios

## CLI is absent

Stop and name the missing executable. Do not silently change data sources. For a
Google Workspace operation, `gws` is the required route; an MCP group is not a
fallback.

## CLI authentication fails

Run the CLI's status/help command, report the failing service, and give the
specific re-authentication step. Authentication failure does not grant authority
to perform a write through another transport.

## Google Workspace write fails

Keep the exact API error and exit code. Do not claim partial success without a
read-back. If the API reports a validation error, inspect the exact method schema
with `gws schema <service.resource.method> --resolve-refs` before retrying.

## Browser profile is ambiguous

Run `chrome-use browsers`, select the authorized profile explicitly, and keep all
subsequent commands pinned to it.

## MCP gateway exception group is unavailable

Confirm that the request matches the `fieldkit-sales` or `fieldkit-dataverse`
`MCP-NECESSITY` boundary. Then check `systemctl --user status mcpjungle` and
`mcpjungle list groups`. If the required group remains unavailable, stop and report
it. Do not substitute a different service and call the result equivalent.

## Direct global MCP is unavailable

Confirm that the request matches the `gh_grep`, `brave_search`, or `context7`
`MCP-NECESSITY` boundary, then inspect whether the client loaded that exact server.
Do not look for it in mcpjungle. If the server is absent or its call fails, stop and
report the exact unavailable route; do not silently select another data source.

## Retired vault graph request

State that backlink, outlink, orphan, and connection-path graph operations have
no maintained backend. Offer native exact search or `qmd` retrieval only when it
answers the user's underlying question; do not recreate the removed vault group.
