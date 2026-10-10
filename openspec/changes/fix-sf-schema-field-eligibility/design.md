## Context

`build_schema_reference()` describes the object, filters fields through
`_queryable_fields()`, then reads each explicit record in chunks of
`_MAX_QUERY_FIELDS_PER_REQUEST` field names. The filter requires a per-field
`queryable` key that Salesforce never sends, and `_record_observations()`
falls back to `("Id",)` when the field list is empty, which hides the failure.

## Goals / Non-Goals

### Goals
- Return the real field list for any readable object.
- Fail loudly when eligibility filters out everything.
- Make the fixtures structurally faithful to describe responses.

### Non-Goals
- Changing the value-free output contract, the ten-record cap, or the
  observed-population states.
- Adding SOQL, field-level security probing, or persistent output.

## Decisions

1. **Eligibility = named and not deprecated-and-hidden.** These attributes are
   present per field in describe responses. Compound fields (for example an
   address and its components) stay eligible: record reads return compound
   values as nested mappings, which classify as `observed-populated` or
   `observed-null` like any other value.
2. **Rename `_queryable_fields()` to `_eligible_fields()`.** The old name
   describes the wrong rule, and keeping it would mislead the next reader.
3. **Remove the `("Id",)` fallback.** With an explicit empty-set error ahead of
   it, the fallback has no remaining purpose and was what turned a total
   failure into an empty success.
4. **Fixture shape.** One shared fixture builder produces describe responses
   with object-level `queryable` and per-field `name`, `label`, `type`,
   `deprecatedAndHidden`, and `compoundFieldName`, using fictional field names.
   No per-field `queryable` key appears anywhere in the tests.

## Risks / Trade-offs

- **Fields that can't be read in a record GET.** Some field types may still be
  rejected by the record endpoint for a given org or profile. The existing
  "requested record cannot be read" data error covers this; if a live run
  shows a specific type is always rejected, exclude it by `type` in a
  follow-up with its own evidence rather than guessing now.
- **Request count.** About 120 fields at the current chunk size means several
  requests per record, bounded by the ten-record cap and existing timeouts.
