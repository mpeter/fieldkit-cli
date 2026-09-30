# Stakeholder map

Use this workflow to draft an account-wide or pursuit-specific buying-committee
map from attributable evidence. The output separates observed facts, operator
judgment, and unknowns; it does not silently update an account or pursuit file.

## Establish identity and scope

Require one exact configured account key. For a pursuit-specific map, also
require the exact pursuit path or identifier. Resolve ambiguous people by exact
email before joining records, and do not merge people solely because their names
match.

Read `accounts/<account>/account.md` and the selected pursuit record when they
exist. Preserve the provenance of any existing role or support assessment;
missing provenance makes it operator judgment, not verified customer intent.

## Collect shipped fieldkit evidence

For each known exact email, query the local contact index:

```bash
fieldkit contact find <email> --affiliations --json
```

`--affiliations` can return the contact's associations across accounts; it is
not scoped to this map's account. Review only the associations relevant to the
confirmed account and omit unrelated customer context from the draft. If that
cross-account read is not authorized, omit the flag and keep affiliations unknown.

For a bounded, dated view of the account's existing Gmail cache:

```bash
fieldkit gmail query account <account> --since <YYYY-MM-DD> --limit 10 --json
```

These are read-only queries of a ready published cache. They do not refresh Gmail, prove complete
coverage, establish sentiment, or prove that a reply is owed. Record the cache's
known date range. A missing cache, unknown contact, nonzero exit, partial result,
or malformed output is `unavailable`, not a negative finding.

If an exact Salesforce Opportunity ID is already present and current opportunity
context is needed, inspect the local help first:

```console
fieldkit sf opportunity --help
```

After authorizing the credentialed read, use its read-only mode:

```console
fieldkit sf opportunity <opportunity_id> --no-write
```

Its opportunity summary does not promise Contact
Roles; do not claim a Salesforce stakeholder role unless the returned evidence
actually contains it.

The help invocation is a safe local check. The opportunity read requires a
configured and authorized Salesforce identity and approval for that exact
opportunity. A help result does not prove the live read. Record its observation
time and completeness; failed or incomplete reads remain unavailable.

## Add optional evidence deliberately

Calendar, Slack, public-web, directory, and account-intelligence tools are
optional operator-provided sources. fieldkit does not install or guarantee any
of them. Use one only after confirming its installed read-only interface,
authenticated identity, account scope, date range, pagination bound, and output
shape.

Attribute every observation to its source and date:

- Calendar organizer or attendee membership shows an invited or scheduled
  participant, not confirmed attendance. Require separate evidence before
  claiming attendance; neither membership nor attendance establishes buying
  authority or support.
- An email or internal comment may identify a concern, but silence does not prove
  opposition.
- An account-intelligence score is a provider suggestion, not a customer fact.
- Public research may support title and background when linked and dated; it
  does not establish private priorities or relationship strength.
- A failed, unavailable, ambiguous, or incomplete read remains explicitly
  `unavailable` or `pending`. Do not silently switch sources.

## Draft roles and gaps

Use buying-committee roles such as Economic Buyer, Technical Buyer, Champion,
Influencer, End User, Procurement, or Legal only when evidence or explicit
operator judgment supports them. Otherwise set the role to `Unknown`. A person
may hold more than one role.

Capture only what the evidence supports:

- name, title, and function;
- role and whether it is verified or operator-assessed;
- influence and support, with source or `Unknown`;
- last verified contact date and method;
- stated priorities and concerns, preserving the speaker and date;
- relationship owner, when explicitly assigned;
- next step as a proposed action, not a commitment.

Replace sample statuses with observed outcomes. A template row marked `verified`
is not evidence: missing, failed, partial, or unknown coverage stays unavailable
or pending. Keep proposed actions separate from observed facts.

```markdown
# Stakeholder Map — [account] — [opportunity or account-wide]
Last reviewed: [YYYY-MM-DD]

## Source status
| Source | As of / scope | Status | Note |
|---|---|---|---|
| account record | [date or unknown] | verified | [scope] |
| Gmail cache | [range] | unavailable | [reason] |

## Stakeholders
| Name | Title | Role | Basis | Influence | Support | Last contact | Owner | Gap? |
|---|---|---|---|---|---|---|---|---|
| [name] | [title] | Unknown | insufficient evidence | Unknown | Unknown | [date] | Unknown | Y |

## Evidence notes
### [name]
- Observed: [fact] — Source: [source, date]
- Operator assessment: [assessment or none]
- Unknowns: [role, priorities, concerns, or relationship state]
- Proposed next step: [owner and timing to confirm]

## Coverage gaps
- Economic Buyer: [name and evidence, or NOT IDENTIFIED]
- Champion: [name and evidence, or NOT IDENTIFIED]
- Uncontacted high-influence stakeholders: [verified list or UNKNOWN]
- Single-threaded risk: [evidence-based assessment or UNKNOWN]
- Priority actions: [reviewable proposals]
```

Present the draft for review. Do not write to `account.md`, a pursuit file, or
an external system. If the operator asks to save an approved map, identify the
owned body section and exact file first, preserve frontmatter and generated
regions, obtain confirmation, and verify the resulting diff. An existing section
or file needs explicit overwrite approval.
