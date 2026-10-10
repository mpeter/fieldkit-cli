### Render --json timestamps as ISO-8601 (#126)

`--json` output now renders any date or time value as ISO-8601 instead of Python's
space-separated form, and a UTC timestamp ends in `Z` (`2026-07-01T06:45:01Z`), so
RFC 3339 parsers and, for whole-second values, `jq`'s `fromdate` accept it. No
current command was found emitting a space-separated timestamp.
