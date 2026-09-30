---
name: create-cli
description: >
  Design a new fieldkit CLI command or materially change its arguments, options,
  output, exit behavior, prompts, or configuration contract. Produces an
  implementation-ready interface spec before code. Do not trigger for ordinary
  domain changes or minor help-copy edits with no behavior change.
metadata:
  opencode/slash: "true"
  category: developer
---

# Create CLI

Define the observable interface before implementation. Keep the result compact
and specific to the requested command.

This is a contributor agent workflow, not a fieldkit command that generates or
installs code. It requires an authorized source checkout of the revision being
changed. A packaged skill installation alone is not that checkout.

## Establish the local contract

Identify the checkout and its revision with the operator. Read its `AGENTS.md`
and `docs/reference/exit-codes.md`; do not resolve these paths relative to the
installed skill directory or assume a private maintainer checkout exists.
If the source or required contract is unavailable, report that prerequisite
as pending rather than inventing the current interface.

Inspect that checkout's live Click tree and adjacent commands. An independently
installed command may run a different revision, so it is not source-candidate
proof. Those sources override generic CLI advice. In particular, fieldkit's
top-level handler maps Click usage errors to data error status 3.

Clarify only decisions that would change the interface. Infer the rest from
adjacent commands and state assumptions explicitly.

## Produce the command spec

Include:

- command tree and usage synopsis;
- each argument and option with type, default, requiredness, placement, and help;
- end-to-end plumbing from Click parsing through the domain input to an
  observable effect;
- human stdout, diagnostics on stderr, stable `--json` shape, ordering, and
  partial-result behavior where relevant;
- failure classes mapped through the canonical 0/1/2/3 exit contract;
- prompt and TTY behavior, plus preview, confirmation, or force semantics for
  mutations;
- configuration sources and precedence, using fieldkit's existing roots and
  accessors;
- idempotence, rerun, timeout, and degraded-mode behavior that applies;
- compatibility with existing invocations, help, consumers, and generated docs;
- representative invocations and grep-checkable acceptance conditions.

For every new option, name the tests that prove its behavior is plumbed. An
accepted option with no observable effect is incomplete. Machine mode must carry
the same result and failure meaning as human mode without mixing diagnostics
into stdout. Secrets do not belong in argv.

Stop after the interface contract unless implementation was also requested.
Do not edit code, stage changes, create issues, publish a package, or alter
repository settings merely because the skill was invoked. A proposal is not
an implemented feature or a passing acceptance test.

## Provenance

Adapted from `steipete/agent-scripts` `skills/create-cli` at commit
`dc4f583a2c1a6f3a93e81a972eee89f59aca32f7`. See the
[upstream license](references/UPSTREAM-LICENSE.txt). fieldkit conventions replace the upstream
generic exit-code and configuration defaults.
