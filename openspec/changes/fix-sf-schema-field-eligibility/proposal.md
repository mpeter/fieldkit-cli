## Why

Issue #87: `fieldkit sf schema <SOBJECT> --record-id <ID>` has always returned
`"fields": []` for every object and record. `_queryable_fields()` in
`src/fieldkit/sf/schema.py` keeps only describe entries where
`raw_field.get("queryable") is True`. In a Salesforce describe response,
`queryable` is an attribute of the object, not of each field, so no field ever
qualifies.

Three things let this ship:

- The test fixtures in `tests/test_sf_schema.py` add a per-field
  `"queryable": True` that real responses don't have, so tests encoded the
  wrong shape.
- The spec says the command reports "every eligible described field" without
  defining eligibility.
- When no fields qualify, `_record_observations()` falls back to requesting
  only `Id`, so the empty result looks like a successful run.

## What Changes

1. Define eligibility from attributes that exist per field: every described
   field with a string `name`, excluding fields marked
   `deprecatedAndHidden: true`.
2. Treat an empty eligible set, including an empty `fields` list, as a data
   error (exit 3) instead of an empty success.
3. Replace the synthetic fixtures with a describe shape matching the real
   response structure (object-level `queryable`, per-field `name`, `label`,
   `type`, `deprecatedAndHidden`, `compoundFieldName`), using fictional values.

## Capabilities

### New Capabilities
- None.

### Modified Capabilities
- `sf-schema-reference`: field eligibility is defined, and an empty eligible
  set is reported as a data error.

### Removed Capabilities
- None.

## Impact

- `src/fieldkit/sf/schema.py`, `tests/test_sf_schema.py`.
- User-visible: the command starts returning real field lists. Record reads
  are chunked by `_MAX_QUERY_FIELDS_PER_REQUEST`, so an object with 120 fields
  needs several requests per sampled record; the ten-record cap bounds the
  total.
- Requires a `changelog.d/` fragment.

## Constitution Alignment

Assessed against the Unbound Force org constitution.

### I. Autonomous Collaboration

**Assessment**: PASS

The JSON reference becomes a usable artifact for skills and agents that plan
Salesforce field mappings.

### II. Composability First

**Assessment**: PASS

The Salesforce integration stays optional; nothing is imported until the
command runs.

### III. Observable Quality

**Assessment**: PASS

An empty eligible set now fails with exit 3 and a message instead of passing
as an empty, apparently successful reference.

### IV. Testability

**Assessment**: PASS

Field eligibility is a pure function of a describe mapping and is tested with
fixtures that mirror the real response structure, without network access.
