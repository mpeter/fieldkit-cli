### Remove unreachable developer-automation code

The web dashboard's operations view no longer shows a "developer" stage. That
stage queried a `fieldkit driver` command that the public CLI never exposed, so
it reported an error and suggested running a command that does not exist.

The unregistered `driver`, `autonomy`, and `health` command modules and their
domains are removed from the package, along with the `FIELDKIT_HARNESS_ROOT`
environment variable, which only they read. The `issue` command no longer
accepts `driver` or `health` as module values.
