# Dataverse MCP exception

`fieldkit-dataverse` is retained because the authenticated remote Dataverse
service exposes proprietary Rover organization data, Jira data, and Snowflake
analytics without an installed CLI.

Use it for:

- Rover employee, manager-chain, team, and organization queries
- authenticated Jira project and issue data
- Snowflake-backed financial and sales analytics

The service requires its configured token and, where applicable, the corporate
network. If unavailable, report that dependency. Do not invent a separate Jira
group or treat interactive browser access as structured parity.
