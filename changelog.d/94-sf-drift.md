### Report pursuit drift from Salesforce (#94)

`fieldkit sf drift` compares every active, Salesforce-linked pursuit with its live opportunity and reports stage, close-date and consulting-ACV drift plus overdue and soon-closing deals as RED, YELLOW or GREEN. It is read-only, lists unreadable pursuit files instead of skipping them, exits `1` for an incomplete report, and exits `2` when the Salesforce session needs `fieldkit auth sf`. Use `--json` for a machine-readable report.
