### Measure developer spend on OpenCode 2

The developer spend cap now reads today's sessions from OpenCode 2's
`session_v2` table. After an OpenCode 2 upgrade the v1 `session` table stops
receiving rows, so the cap read zero spend and never stopped a run. Databases
without `session_v2` are still read from `session`.

Developer schedules and their spend accounting now expect `openai/gpt-6.1-sol`,
replacing the retired `gpt-5.6-terra`. Update any developer schedule pinned to
the old model; `scripts/check_developer_schedules.py` reports it.
