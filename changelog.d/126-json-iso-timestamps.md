### Render --json timestamps as ISO-8601 (#126)

`--json` output now renders any date or time value with ISO-8601 formatting (`2026-07-01T06:45:01+00:00`) instead of Python's space-separated form, so RFC 3339 parsers such as `jq`'s `fromdate` accept it. No current command was found emitting a space-separated timestamp.
