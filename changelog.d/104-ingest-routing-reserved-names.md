### Route ingested material by exact reserved names (#104)

Ingest routing now skips only the reserved pursuit files `template.md` and `gmail-intel.md`, the same exact-name rule reports use. A pursuit such as `gmail-intel-rollout.md` can now receive routed material, and a `pursuits/template.md` no longer can. Files whose names merely contain `.template` are ordinary pursuits for routing, as they already were for reports.
