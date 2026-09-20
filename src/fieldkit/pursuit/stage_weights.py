"""Shared stage probability weights for weighted pipeline calculations.

Single source of truth for stage weights used by both:
  - fieldkit.commands.pursuit.forecast (pursuit forecast command)
  - fieldkit.commands.pipeline.quota (pipeline quota command)

historic regression: Previously these two commands maintained independent weight tables
with different values, causing them to report different weighted pipeline
numbers for the same pursuits. This module resolves the discrepancy.

The correct values are the forecast.py ones (more conservative). These
represent the AE-facing contract for what weighted pipeline means.

Previous quota.py values (for reference):
  negotiate: 0.90  (now 0.75)
  propose:   0.60  (now 0.50)
  validate:  0.40  (now 0.25)
  discover:  0.20  (now 0.10)
  qualify:   0.10  (now 0.05)

User impact: pipeline quota weighted numbers will be lower after this change
(forecast weights are more conservative). This is intentional — the numbers
now agree with what pursuit forecast reports.
"""

# Stage probability weights for weighted pipeline value.
# Keys use the hyphenated stage names from pursuit frontmatter.
#
# prospect/pre-pipeline preserved from quota calculation;
# forecast.py skips pre-pipeline via SKIP_STAGES.
STAGE_WEIGHTS: dict[str, float] = {
    "closed-won": 1.00,
    "negotiate": 0.75,
    "propose": 0.50,
    "validate": 0.25,
    "discover": 0.10,
    "qualify": 0.05,
    # prospect/pre-pipeline preserved from quota calculation;
    # forecast.py skips pre-pipeline via SKIP_STAGES.
    "prospect": 0.05,
    "pre-pipeline": 0.0,
}
