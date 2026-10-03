### Release from one signed tag in a single workflow run

A signed version tag now drives the whole release. The workflow builds one
candidate, publishes and verifies it on TestPyPI, waits for the `pypi`
environment approval, then publishes the same files to PyPI and creates the
GitHub release. A workflow dispatch is a dry run that publishes nothing. After a
transient failure, **Re-run failed jobs** resumes against the original bundle.
