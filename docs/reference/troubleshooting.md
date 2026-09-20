---
last_reviewed: 2026-09-10
covers:
  - src/fieldkit/commands/doctor/
  - src/fieldkit/cli_exit.py
audience: user
---

# Troubleshooting

Start with the smallest command that demonstrates the failure. Do not post
credentials, cookies, tokens, customer records, email content, private hostnames,
or absolute home paths while asking for help.

## Capture a safe diagnostic

```console
fieldkit --version
fieldkit doctor
fieldkit doctor --json
```

Then run the relevant service check, such as `fieldkit doctor sf`,
`fieldkit doctor google`, or `fieldkit doctor gmail`. Record the command, exit
code, operating system, Python version, and sanitized error text.

Exit codes identify the next action:

| Exit | Meaning | Next step |
| --- | --- | --- |
| `0` | Selected work succeeded | No repair needed |
| `1` | Partial result; retry may help | Inspect the named record or provider failure |
| `2` | Authentication needs user action | Reauthenticate through the documented provider flow |
| `3` | Invalid or incomplete data/configuration | Correct the named input; retrying unchanged input will not help |

## Base installation fails

Confirm the executable and Python version:

```console
fieldkit --version
python3 --version
```

For a released uv-tool install, reinstall the same public version and rerun the
[offline first-success path](../getting-started.md). In a contributor checkout,
run commands as `uv run fieldkit` so the checkout—not another globally installed
copy—is under test.

## Configuration is missing or invalid

Run `fieldkit init --minimal <path>` for a credential-free trial or `fieldkit init`
for interactive configuration. fieldkit reports the invalid key or path and exits
`3`; do not repeatedly retry unchanged configuration.

The [configuration reference](config-file.md) explains the workspace, runtime,
and user-configuration roots.

## Salesforce authentication fails

```console
fieldkit doctor sf
fieldkit sf session-check
```

Exit `2` means the configured Salesforce session is absent or expired. Follow
[Connect Salesforce](../guides/salesforce-auth.md) using an organization and
session you are authorized to access. Never attach the `sid` value to an issue.

## Google or Gmail fails

```console
fieldkit doctor google
fieldkit doctor gmail
```

Complete a first OAuth consent flow in an interactive terminal. An unattended
job cannot repair missing user consent. If the local Gmail cache is damaged,
preserve a backup before rebuilding it so unexpected data loss remains
recoverable. See [Connect Gmail](../guides/gmail.md).

## A watcher or brief is incomplete

```console
fieldkit watch status
fieldkit watch logs --list
fieldkit watch logs <watcher> --tail 100
```

A not-yet-run optional source is different from a failed source. Investigate
`partial` and `fatal` outcomes individually; do not restart or reconfigure an
unrelated service. Use `fieldkit brief generate --pipeline-only --no-llm --dry-run`
to isolate local pipeline rendering from watcher aggregation and model synthesis.

## An optional dependency is missing

The error names the required profile. Reinstall with that extra, for example:

```console
uv tool install --force 'fieldkit-cli[google]'
```

Installing a profile does not configure credentials. Follow the corresponding
integration guide after installation.

## Ask for help

Search existing issues, then follow [SUPPORT.md](https://github.com/mpeter/fieldkit-cli/blob/main/SUPPORT.md).
Use [SECURITY.md](https://github.com/mpeter/fieldkit-cli/blob/main/SECURITY.md) for a
suspected vulnerability instead of disclosing details publicly.
