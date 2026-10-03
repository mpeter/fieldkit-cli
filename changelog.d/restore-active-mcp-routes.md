### Changed

- MCP-backed watchers use explicitly configured `mcp_calendar_group`, `mcp_mail_group`, and `mcp_sales_group` values. Set the relevant key to enable each source or watcher. Tool safety hooks continue to block prohibited actions when a group is renamed.
