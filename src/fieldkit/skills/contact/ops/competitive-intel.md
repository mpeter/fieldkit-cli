# Prepare competitive research

Use this workflow to draft a battlecard for an identified competitor, product
area, and optional account or pursuit. It organizes evidence for a conversation;
it does not produce a verified market ranking, qualification score, or permission
to change an account or CRM record.

## Confirm the scope

Require the competitor name and comparison area. When the request is account- or
deal-specific, confirm the exact account slug and pursuit before reading local
files. If several pursuits or similarly named competitors match, ask the operator
to choose; never select by list order.

State the intended audience and decision: for example, preparing discovery
questions is different from publishing a feature comparison. A broad request
without a product area should remain a high-level research outline until the
operator narrows it.

## Gather attributed evidence

Start with the confirmed workspace account and pursuit files when they exist.
Record the source path and relevant date. Separate customer statements, internal
notes, CRM-derived fields, and prior model synthesis.

Public research is optional and requires an available research tool plus operator
approval for any sensitive query terms. Prefer official product documentation,
release notes, pricing pages, security or support policies, primary company
announcements, and clearly identified independent analysis. For each retained
claim, capture the source URL, publication date, and access date. A search snippet
alone is not adequate support for a product or pricing claim.

Internal conversation context is also optional. Use Slack or another internal
source only when it is configured, authorized, and bounded to the intended
workspace, channels, and dates. Attribute observations without copying private
message bodies into the battlecard. A colleague's opinion, a customer statement,
and a confirmed outcome are different evidence types.

When a source is missing, stale, conflicting, paywalled, or incomplete, label the
claim unavailable or disputed. Do not fill gaps from brand familiarity, generic
sales lore, or an unsourced prior battlecard.

## Build the draft

Use the [battlecard template](competitive-intel-battlecard-template.md). Include
only comparison dimensions relevant to the confirmed scope. Ground every claim,
question, objection response, and win/loss pattern in a cited source or label it
as a hypothesis for review.

Avoid invented win rates, unsupported pricing, loaded “landmine” questions, and
claims about lock-in, support, ecosystem, or ownership that are not established
for the compared versions and date. Do not turn internal or model-generated
material into customer evidence.

The default result is a draft in chat. Show the source ledger, unsupported areas,
and conflicts with the draft. Ask for corrections before offering to save it.

## Save only with approval

If the operator requests a file, propose a path under the confirmed account's
`artifacts/` directory using a sanitized competitor slug and meeting-relevant
date. Confirm the configured workspace root; do not write into the code checkout.
Ask before creating or replacing the file. Refuse a symlinked destination or a
path that escapes the intended account.

After an approved write, read the file back and confirm the heading, scope,
source ledger, and unsupported-claim markers. Saving a battlecard does not
authorize pursuit-frontmatter, Salesforce, task, or qualification changes.
