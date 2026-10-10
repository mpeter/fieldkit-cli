## 1. Tests first

- [ ] 1.1 Add a describe-response fixture builder in `tests/test_sf_schema.py` with object-level `queryable` and per-field `name`, `label`, `type`, `deprecatedAndHidden`, `compoundFieldName`, with no per-field `queryable`, and fictional names
- [ ] 1.2 Add a regression test reproducing #87: a realistic describe with several fields and one sampled record yields a non-empty field list (fails on current `main`)
- [ ] 1.3 Add tests for a `deprecatedAndHidden` field being excluded from output and record requests, and for an all-ineligible describe exiting 3 without record requests
- [ ] 1.4 Remove per-field `"queryable"` keys from the existing fixtures

## 2. Fix

- [ ] 2.1 Rename `_queryable_fields()` to `_eligible_fields()` in `src/fieldkit/sf/schema.py` and apply the eligibility rule
- [ ] 2.2 Raise the data error for a non-empty describe with no eligible fields, and remove the `("Id",)` fallback in `_record_observations()`

## 3. Finish

- [ ] 3.1 [P] Add `changelog.d/87-sf-schema-fields.md`
- [ ] 3.2 [P] Update the `sf schema` reference docs if they describe the field filter
- [ ] 3.3 Run `make pr-check`
- [ ] 3.4 Optional live check, with authorization only: run `fieldkit sf schema Account --record-id <id> --json` against a real org and confirm a non-empty field list; record only the field count in the PR, never values or IDs
- [ ] 3.5 Verify constitution alignment: an empty eligible set is reported with exit 3 (III); eligibility is tested from fixtures with no network (IV)
