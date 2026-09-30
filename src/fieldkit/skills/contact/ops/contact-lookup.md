# Look up a contact

Use this workflow to resolve one email address or display name from fieldkit's
local Gmail people index. The result describes cached communication and optional
workspace affiliations; it does not refresh Gmail or contact an external service.

## Run the local lookup

Use `fieldkit contact find QUERY --affiliations --json` when pursuit and account
affiliations are relevant. The configured Gmail database is the default. Use
`--db PATH` only when the operator intentionally selects a different database
and its provenance is known.

The `--affiliations` flag scans the configured account tree; it has no account
filter. Omit it when cross-account workspace search is outside the authorized
scope. If it returns affiliations from several accounts, present them separately
and ask which identity is intended.

Treat the JSON `type` as the control field. The supported outcomes are
`resolved`, `ambiguous`, and `not_found`:

- For `resolved`, show identity, cached communication counts and dates, recent
  threads, recent meetings, and affiliations that are present in the payload.
- For `ambiguous`, show the bounded candidates with email, display name, message
  count, and account when available. Ask the operator to choose an exact email,
  then rerun; never choose the first or busiest result.
- For `not_found`, report the miss. If `affiliations` is non-empty, present those
  workspace matches separately and make clear that the people index did not
  resolve the identity.

Missing fields stay unavailable. The command can return up to eight distinct
recent thread topics from a bounded cache query and up to ten recent calendar
entries when those tables exist. It does not promise a 12-month window or a
complete communication history. A missing people table is a partial result that
requires the documented Gmail sync workflow before retrying.

## Interpret without overclaiming

The `champion_signal` and `decay_signal` fields are initiation and engagement
heuristics calculated from cached counts and trend data.
Do not describe either signal as verified influence, a current buying role,
customer intent, or a ClosePlan qualification result. Explain the observed
inputs and label any interpretation as inference.

The `is_internal` value depends on configured domain classification, not an
authoritative employment directory. Affiliation titles, roles, notes, and source
paths come from workspace Markdown and may be stale. Attribute them to that file
rather than presenting them as current CRM facts.

## Optional context

Slack is optional and is not queried by `fieldkit contact find`. Use it only when
the operator requests and authorizes that source and a separately configured
client can prove the intended workspace and bounded read scope. Follow the
[Slack search protocol](../../tool-routing/references/slack-search-protocol.md).
If that reference is absent from a selective installation, report the
missing prerequisite. If a usable client is absent, mark Slack context
unavailable.

Keep Slack observations separate from customer statements and local cache
metrics. Say “No matches in a completed scope” only after a complete bounded
search; authentication failure, pagination limits, or parse errors are not an
empty result.

An authorized calendar read can confirm a specific attendee or meeting, but do
not select an event by title or recency alone. Resolve the exact account, event,
and person first.

## Present the result

Produce a concise card with these sections when supported by the payload:

- Identity and cache source;
- Workspace affiliations with source paths;
- recent threads and meetings with dates;
- communication counts and observed coverage;
- initiation and engagement heuristics; and
- a “So what” inference that names its supporting observations and uncertainty.

No contact lookup writes a workspace or external resource. If the operator asks
to save an update, show the exact destination and text, then use the owning
workflow for that destination with separate approval.
