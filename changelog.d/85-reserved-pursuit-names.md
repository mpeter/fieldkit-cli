### Reject reserved pursuit names (#85)

`pursuit create` and `pursuit rename --to` now refuse `template` and `gmail-intel` with exit 3 and no changes, instead of writing a file that forecast, health, and audit silently skip. Those reports now name any reserved file they skip on stderr, and forecast JSON lists them in `assessment.reserved`; exit statuses are unchanged. Brief, pipeline, and quota match the exact file name, so pursuits such as `gmail-intel-rollout.md` reappear and a `pursuits/template.md` no longer counts as a pursuit.
