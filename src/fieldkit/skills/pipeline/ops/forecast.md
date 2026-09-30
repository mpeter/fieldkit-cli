# Calculate a local forecast

Use `fieldkit pursuit forecast --json` to calculate deterministic scenarios from
the configured workspace. Add `--account ACCOUNT` only with a confirmed literal
account directory slug; a wildcard can broaden the scan.
Add `--quota AMOUNT` to override the configured quota target; without it, the
command uses `pipeline.quota.target` when that setting exists.

## Understand which records and amounts count

The command excludes closed-lost, pre-pipeline, and prospect pursuits. Unknown
stages are skipped with a warning. A recognized pursuit with a zero selected
amount remains in the result and produces a data-quality warning.

For standard or unspecified contract types, the selected amount prefers
`sf_consulting_acv`, then `sf_acv`, then `sf_arr`. For `fixed_price`, it prefers
`sf_acv`, then `sf_consulting_acv`, then `sf_arr`. An explicit zero is preserved.

The stage weights are closed-won 100%, negotiate 75%, propose 50%, validate 25%,
discover 10%, and qualify 5%. They are deterministic scenario weights, not
measured win probabilities or current qualification evidence.

## Read the scenarios

- Closed Won is the sum of included closed-won pursuits.
- Commit is closed-won plus negotiate.
- Weighted is each included amount multiplied by its stage weight; it includes
  closed-won at 100%.
- Best Case is the face-value sum of active pursuits and excludes closed-won.

When quota is available, the command reports `quota - commit` and
`quota - weighted`. It does not report a gap from Best Case.

The output is only as current as the local frontmatter. The command neither
queries Salesforce nor writes files. Report skipped stages and zero-value deals
with the totals; do not silently repair them or describe the result as live.
