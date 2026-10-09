### Reject empty compatibility-test evidence (#80)

CI now fails full-suite test evidence when zero tests execute or every test is
skipped. Reports retain the executed and skipped counts and explain how to rerun
the complete suite. Passing runs with some skipped tests remain valid.
Evidence also records the full-suite policy as the normalized selection reason.
