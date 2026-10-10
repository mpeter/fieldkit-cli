### Report described fields in `fieldkit sf schema` (#87)

`fieldkit sf schema` now lists every described field that is not deprecated and hidden, instead of always returning an empty field list. Eligibility no longer depends on the object-level `queryable` attribute. When the describe response contains no eligible field, the command exits with status 3 and a message rather than reporting an empty reference as success. Objects with many fields need several record requests per sampled record, bounded by the ten-record cap.
