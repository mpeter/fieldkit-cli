---
name: start
description: >
  Help a new user initialize a fieldkit workspace, confirm the offline first
  success, and add account context only when they choose to. Distinguish the
  installed command from optional task, memory, and live integration workflows.
metadata:
  opencode/slash: "true"
  category: ops
---

# Start using fieldkit

Help the operator get a usable local workspace, then show one working command.
This skill is guidance; the `fieldkit init` CLI performs initialization. Do not
assume the user has a repository checkout, a sales account, a Google credential,
Salesforce access, an agent memory system, or a particular task file.

## Confirm installation and workspace

Run `fieldkit --version` and `fieldkit init --help` through the installed
command. If either fails, report the failure and use the public installation
guide; do not claim a workspace was created.

Ask where the user wants their fieldkit workspace. Explain that it is separate
from the package/code root and holds their configuration and account data.
For an offline start, propose `fieldkit init --minimal PATH` with the user's
chosen absolute path. This creates a generic workspace, an empty account
registry, runtime-data directories, and user configuration. It does **not**
create accounts, `TASKS.md`, lesson memory, or credentials. Show the target
and get approval before initializing; an existing configuration may be
updated. If the operator wants prompted account setup instead, offer
`fieldkit init` and follow the wizard without inventing answers.

After initialization, run `fieldkit skill list` as the offline first success.
Confirm the workspace from the saved configuration and report what was
actually created. If either command fails, keep status partial and explain
the next corrective step. `fieldkit doctor` checks configured services;
an optional unconfigured integration is not a prerequisite for using the
base installation.

## Add context when requested

If the operator wants account work, inspect the configured workspace's
`config/accounts.yaml` and `accounts/` directory. Ask before creating an
account or importing customer material. Read only the account or pursuit
relevant to the current request; do not bulk-load private files into a
session. Distinguish source dates from fresh live data and treat historical
local qualification scores as non-current.

`TASKS.md` and `memory/system/lessons-learned.md` are optional workspace
conventions, not init outputs. If requested, propose exact destinations and
content, then obtain approval before writing. Use the
[task-management skill](../task-management/SKILL.md) for task format and
[task-sync](../task-sync/SKILL.md) only for separately approved Google Tasks
reconciliation. Do not create or modify agent instruction files as working
memory.

Gmail, Calendar, Salesforce, and other live enrichment are separate,
credentialed workflows. Check the intended account and authorization before
using them. Never run `fieldkit gmail sync` simply because the workspace is
new. If offline or unavailable, say so and continue with the local first
success; do not describe cached context as a live refresh.

Report version, workspace path, offline skill-discovery result, and each
optional area as configured, skipped, pending, or unavailable. Link the user
to the next task they chose instead of treating a missing integration as a
failed bootstrap.
