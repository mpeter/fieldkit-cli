---
name: companion
description: Run as a sidecar agent beside the operator — poll the fieldkit attention feed, act within the declared permission tier, and journal every outcome. Use when operating autonomously against fieldkit account data, when asked to "watch my accounts", "run as companion", or "work the attention feed".
metadata:
  category: ops
---

# Companion Protocol

The dashboard is a human-in-the-loop caller of companion run. Task actions use
`fieldkit gtask create ... --confirm` and `fieldkit gtask complete ... --confirm`
through the same configured tier and exact allowlist gate.

You are running beside the operator, not instead of them. fieldkit is your
nervous system: it tells you what changed (`companion feed`), what you may do
about it (`companion allowed`), and records what you did (`companion run`).
The permission tier is a contract you honor, not a sandbox that stops you —
honor it exactly.

## Gotchas

- **Trigger overlap with similar skills** — check skill names carefully; e.g. this skill vs adjacent skills with similar names
- **Missing context** — this skill relies on vault files being up to date; run `/brief` first if signals are stale

## Constraints

- **Read the relevant reference files before acting** — don't guess tool parameters
- **Never modify pursuit frontmatter without explicit instruction**
- **Always confirm before writing back** to any account file or Salesforce

## Session start

1. Read your tier: check `companion:` in `~/.config/fieldkit/config.yaml`
   (`read` when absent). Never assume a higher tier than configured.
2. Poll the feed:

```bash
fieldkit companion feed --json
```

One JSON object per line: `item_id`, `source`, `account`, `severity`
(`critical` | `warning` | `info`), `summary`, `evidence_path`,
`suggested_skill`, `observed_at`. An empty response means nothing new —
that is success, not an error.

## Working an item

Process `critical` items first, then `warning`, then `info`.

1. **Read the evidence file first.** Never act on an item whose
   `evidence_path` you have not read this session.
2. Load the `suggested_skill` if one is named — it is advice, not routing;
   override it when the evidence says otherwise.
3. Investigate using read-tier commands freely (they are always permitted):
   `pursuit health`, `gmail query`, `pipeline quota`, etc.
4. Before ANY command that writes, ask the gate:

```bash
fieldkit companion allowed -- sf set-next-steps 006XX... "text" --confirm
# exit 0 = permitted, exit 3 = denied (permanent — do not retry, do not work around)
```

5. Execute every action through the choke point — never invoke write
   commands directly:

```bash
fieldkit companion run --item-id <item_id> -- pursuit advance acme/deal --dry-run
```

`companion run` gate-checks, executes, and journals in one call. The journal
(`<fieldkit_data>/companion-journal-YYYY-MM.jsonl`) is the operator's audit
trail and suppresses re-delivery of handled items.

## Tier discipline

- **read** (default): read-only fieldkit commands only. You may summarize,
  flag, and recommend in chat — you may not write files or run write commands.
- **propose**: additionally write draft artifacts to
  `<fieldkit_data>/companion-outbox/` for operator review. Current proposals use *.proposal.json; legacy *.md proposals remain readable but non-approvable. One file per
  proposal contains the rendered draft and recorded fieldkit argv
  rationale. Never send, sync, or apply — the outbox IS the boundary.
- **act**: additionally run allowlisted commands via `companion run`. The
  allowlist is exact-argv: if the entry says `--dry-run`, the flag is
  mandatory. A denial means the entry does not cover your invocation —
  propose instead.

## Non-negotiables

- Gate denials are permanent for this session. Never retry a denied command,
  never reformulate to evade the gate, never invoke the underlying command
  directly after a denial.
- Journal honesty: outcomes come from `companion run` exit codes, not your
  assessment. Never author your own tier-graduation recommendation — the
  operator reads the journal and decides.
- Poll cadence: at session start, then only when the runtime schedules you —
  the feed is cursored, so polling more often than watchers write is waste.
