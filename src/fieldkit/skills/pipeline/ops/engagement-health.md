# Review delivery-project contract dates

Use `fieldkit pursuit projects --json` to classify files under the configured
`accounts/*/projects/` tree. Add `--account ACCOUNT` only with a confirmed
literal account directory slug; a wildcard can broaden the scan.
Template files and hidden account directories are excluded.

The command reads `sf_contract_end`, `sf_stage`, and `sf_opportunity` from local
project frontmatter. It does not query Salesforce or refresh those values.

## Read the classifications

- ZOMBIE means the recorded contract end date is past and `sf_stage` is not one
  of the supported completed values: `Completed`, `Closed`, `completed`, or
  `closed`.
- EXPIRING means the recorded end date is today through 30 days away.
- SOON means it is 31 through 90 days away.
- ACTIVE means it is more than 90 days away. A past project whose stage is one
  of the supported completed values is also classified ACTIVE.
- UNKNOWN means `sf_contract_end` is missing or is not a parseable `YYYY-MM-DD`
  date.

Rows are sorted ZOMBIE, EXPIRING, SOON, ACTIVE, then UNKNOWN; within a tier they
are sorted by days until the recorded end date.

The default report exits 0 for any valid classification. Add `--strict` when
automation must exit 1 for ZOMBIE or UNKNOWN rows. EXPIRING and SOON alone are
not strict failures. Exit 3 means the configured workspace, accounts directory,
or usable project set was unavailable.

This command performs no writes. Treat every result as a local-file observation,
not a live contract or Salesforce assertion. Propose any correction through the
workflow that owns the project file and require approval before writing it.
