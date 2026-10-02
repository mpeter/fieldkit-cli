### Remove unreachable developer-automation code

The web dashboard's operations view no longer shows a "developer" stage. That
stage queried a `fieldkit driver` command that the public CLI never exposed, so
it reported an error and suggested running a command that does not exist.

The unregistered `driver`, `autonomy`, and `health` command modules and their
domains are removed from the package, along with the `FIELDKIT_HARNESS_ROOT`
environment variable, which only they read.

### Remove the GitHub issue tracker and PR queue

`fieldkit issue` and the web dashboard's PR tab are removed. Both managed
fieldkit's own development repository rather than account work. The
`github_repo` configuration key is no longer read; existing configuration
files that set it still load. `fieldkit version --features` no longer reports
an `issues` section.
