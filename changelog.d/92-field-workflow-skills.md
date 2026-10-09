### Bundle five field-workflow skills (#92)

`fieldkit skill install` now offers five more skills:

- `sf-reconcile` reports pursuit-vs-Salesforce drift across every active, linked pursuit as RED/YELLOW/GREEN and syncs a pursuit's `sf_*` fields only after you approve that pursuit.
- `shadowbot` routes a request to your organization's assistant through `fieldkit shadowbot query`, saves the answer under `scratch/`, and keeps it labeled as unverified.
- `sweep` takes two or three small open items from `TASKS.md` and gets each one to a single remaining keystroke. It drafts but never sends, and it tees up Salesforce writes without running them.
- `exec-review-deck` builds a customer review deck anchored to signed scope, gated on a citation for every on-slide claim, and staged privately.
- `waypoint` runs looped document discovery that narrows to one strategic work product and builds it under `scratch/out/waypoint/`.
