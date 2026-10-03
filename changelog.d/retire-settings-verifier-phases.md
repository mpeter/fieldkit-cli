### Verify repository settings without a cutover phase

`scripts/verify_repository_settings.py` no longer takes `--phase`. Every unmet
control is a failure, `--expected-revision` is required, and the report
(schema version 2) drops the `phase` and `pending` fields. The default
`pre-cutover` phase expected a private repository and failed against the
public one.
