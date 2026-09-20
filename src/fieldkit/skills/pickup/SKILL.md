---
name: pickup
description: >
  Resuming work from a previous session's handoff file in .planning/handoffs/ — picking a
  session back up after a break, a restart, or a handoff from another agent.
  Trigger with "pick up where I left off", "resume my last session", "continue from the
  handoff", "load my handoff", "resume work", "what was I doing".
metadata:
  opencode/slash: "true"
  category: ops
---

# Pickup Handoff

Resumes work from a handoff file written by the `handoffs` skill, restoring full context
without the operator having to re-explain anything.

## Gotchas

- **Fresh-eyes packages are not resumable** — an artifact stamped `type: fresh-eyes` exists so a
  reader can judge the work *without* this session's conclusions. Resuming it re-anchors the
  reader on exactly what the package was built to withhold. Check `type:` before summarizing
  anything.
- **No file specified, no handoff found** — if `.planning/handoffs/` is empty, say so plainly;
  don't invent a handoff to resume.
- **Stale handoffs** — a handoff written days ago may describe a state the repo has since moved
  past (files renamed, PRs merged). Skim current repo state (`git log`, `git status`) before
  trusting a handoff's "Current Work" section as still accurate.
- **Confirm before acting** — a handoff's "Next Step" is a proposal, not standing authorization.
  Get the operator's go-ahead before executing it.

## Constraints

- **Never resume silently** — always present the handoff summary and get confirmation first.
- **Never treat a handoff's Next Step as pre-approved** — the usual action-authorization rules
  still apply once work resumes.

## Process

1. **Find available handoffs.**
   ```bash
   ls -la .planning/handoffs/
   ```
   Files are named `YYYY-MM-DD-HHMMSS-<slug>.md`. If none exist, tell the operator and stop.
2. **Select the handoff.** Use the file the operator named; if none was given, list the available
   handoffs (newest first) and ask which to resume.
3. **Read the handoff file** in full, and check its `type:` first.

   On `type: fresh-eyes`, stop and redirect — do not summarize the contents:

   > This is a fresh-eyes package, not a continuity handoff. You're not resuming this work,
   > you're ruling on it independently. Read `01-JUDGE-HANDOFF.md` and follow it. Don't read
   > `02-` or later — those are downstream of your ruling.

   The value of the package comes from you not knowing what the previous session concluded.
4. **Present a summary** of the handoff to the operator: primary intent, current work, pending
   tasks, and the proposed next step (if any). Flag anything that looks stale against current
   repo state.
5. **Confirm before proceeding.** Ask the operator if they want to continue with the next step as
   written, or redirect.
6. **On confirmation**, proceed with the next step described in the handoff.
