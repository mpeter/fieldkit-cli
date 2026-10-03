### Name the repository when creating the GitHub release

The release workflow's GitHub release step runs without a source checkout, so
`gh release create` could not infer the repository and failed after the PyPI
publication succeeded. It now passes `--repo` and `--verify-tag`, and the
workflow policy (RWF032) rejects a release step without them. `RELEASING.md`
describes how to finish a release by hand when this step fails after PyPI.
