# Check authentication and setup

Before a call, check the selected CLI's status and authenticated identity:

```bash
gws auth status
gh auth status
```

Use other CLIs' installed auth/status help as needed. Never print tokens or decrypted
credentials into diagnostics. Verify the identity approved for the task; `userId=me`
is only an API selector.

If credentials, OAuth configuration, or scopes are missing, report the exact setup
dependency and follow the managed configuration's repair process. Do not reset
OAuth or change a shared registration from an unrelated task session. An authorized
repair should end with a read-only probe of the required service before a write.

For MCP, discover the selected authorized route from the active harness's
configuration and loaded tools. Inspect gateway registration only if the selected
route uses a gateway. A running gateway does not prove upstream access. Do not
assume fixed aliases, create direct connections, borrow another workspace's route,
or run bulk registration to recover one missing capability.
