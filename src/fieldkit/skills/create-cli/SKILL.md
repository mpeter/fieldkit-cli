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

## Establish the local contract

Read [CLI architecture](../../../../docs/cli-architecture.md),
[exit codes](../../../../docs/reference/exit-codes.md), and the relevant parts
of [developer conventions](../../../../docs/dev-conventions.md). Inspect the live Click tree and adjacent
commands. Those sources override generic CLI advice. In particular, fieldkit's
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

## Provenance

Adapted from `steipete/agent-scripts` `skills/create-cli` at commit
`dc4f583a2c1a6f3a93e81a972eee89f59aca32f7`. See the
[upstream license](references/UPSTREAM-LICENSE.txt). fieldkit conventions replace the upstream
generic exit-code and configuration defaults.
