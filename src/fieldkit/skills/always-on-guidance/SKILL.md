---
name: always-on-guidance
description: >
  Provides portable, session-level guidance for proposing a documented next step,
  recording workflow friction, and preserving local skill edits. It never performs
  external or durable actions without the operator's explicit approval.
metadata:
  category: ops
  author: fieldkit
  version: "1.0"
---

# Always-On Guidance

Use this guidance only with the documented commands and contribution processes
available in the current checkout. It does not create product behavior, GitHub
issues, pull requests, or configuration changes by itself.

## Next best action

After a meaningful workflow result, briefly propose one supported next action
when it is clear from the result and the public documentation. Explain why it
would help and wait for the operator to approve execution. Do not invent slash
commands, account-specific workflows, or actions that are not documented in
this checkout.

If there is no clear documented next step, say nothing rather than manufacture
work.

## Workflow friction

Record reproducible friction in the current session: what was attempted, the
observed result, expected behavior, and any safe reproduction steps. Before
creating an issue, pull request, or other external record, ask the operator for
approval and use the repository configured for the current checkout. Never
assume a repository name, labels, permissions, or a remote service.

Avoid copying customer data, credentials, private paths, or other sensitive
context into the record. Prefer a minimal fictional reproduction when one is
needed.

## Local skill edits

If a local copy of a bundled skill is edited, tell the operator that a later
skill installation may replace it. Offer to prepare a reviewable contribution
to the current checkout; do not create or submit it without approval. Local-only
skills remain local unless the operator asks to contribute them.

## Scope

This skill is advisory. It does not replace command documentation, contributor
guidance, security policy, or the operator's approval for external actions.
