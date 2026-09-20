"""fieldkit.health — nightly repo-health sensor (autonomy roadmap Stage 3, implementation change).

Runs the frozen quality-gate bundle against a clean checkout of ``origin/main``,
attributes each failure to a specific check, and files one deduplicated GitHub
issue per *new* regression through the same store the ``fieldkit issue create``
command uses. Green runs are silent. The sensor reads the gates; it never moves
one.
"""
