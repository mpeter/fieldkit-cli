### Use operation-specific data-sync timeouts

`fieldkit sync` now gives each subprocess step a named timeout. Dry runs display the selected ceiling, and a timed-out step reports its name and ceiling while the pipeline continues to a partial result.
