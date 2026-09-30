# Review pursuit health

Use `fieldkit pursuit health --json` to classify locally tracked pursuits by
close-date and Salesforce-linkage findings. Add `--account ACCOUNT` to limit the
scan to a validated literal account directory slug; wildcard characters are
rejected rather than broadening the scan.

The command excludes closed-won, closed-lost, and pre-pipeline pursuits.
Prospect-stage pursuits are also excluded unless `--include-prospect` is supplied.

## Read the result

The implemented risk tiers are:

- HIGH when the recorded close date is overdue;
- MEDIUM when the close date is within 30 days and the pursuit is not in a late
  stage, or when `sf_opportunity_id` is missing; and
- LOW when neither condition is present.

Days in stage is displayed when `last-transition` contains a parseable date, but
it does not change the risk tier. Missing or old `sf_last_pulled` data also does
not change the tier. This report does not detect Salesforce staleness.

Qualification is always `unavailable`. The command does not fetch native
ClosePlan evidence and does not use historical local qualification scores.

## Use policy exits deliberately

The default report exits 0 even when HIGH or MEDIUM items exist. Add `--strict`
when automation must exit 1 for one or more HIGH items. MEDIUM alone is not a
strict failure. Exit 3 means the configured workspace, accounts directory, or
usable pursuit set was unavailable.

Use `--json` for automation; it returns the same classification fields without
changing the policy. Do not infer a clean Salesforce state from exit 0.

This command performs no writes. If current Salesforce evidence is required,
hand off to the separately approved `sf-sync` workflow and rerun this report
only after its exact local destinations have been verified.
