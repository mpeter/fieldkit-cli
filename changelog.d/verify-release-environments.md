### Verify release environments before tagging

`scripts/verify_repository_settings.py` now checks the `testpypi` and `pypi`
deployment environments against `.github/repository-settings.json`. Both must
deploy only from `v*.*.*` tags, only `pypi` may require a reviewer, and any
undeclared environment is reported. The allowed-actions list no longer includes
`actions/create-github-app-token`, which no workflow uses.
