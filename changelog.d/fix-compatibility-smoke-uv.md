### Fix release compatibility smoke checks

The artifact smoke check now uninstalls with `uv` when `uv` created the test
environment, which has no `pip`. The core compatibility jobs now install `uv`,
so they can verify the documented `uv tool install` path. Both defects had
blocked every compatibility run, and with it every release run.
