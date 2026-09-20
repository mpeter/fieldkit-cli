"""fieldkit.driver — Autonomous brief-execution driver loop.

Picks the oldest GitHub issue labeled ``agent-ready``, loads the brief file
it points to from the repo, and runs a headless OpenCode session to execute
the work.  Stops at ``git push`` + PR open; never merges.

Entry point: ``fieldkit driver run``
"""
