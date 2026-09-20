"""Canonical stage sets for pursuit lifecycle management.

Single source of truth for all stage classification across the codebase.
Import from fieldkit.pursuit (re-exported from __init__.py).
"""

from fieldkit.pursuit.enums import Stage

# Stages that represent a closed deal — no further action expected.
# Includes "won-lost" as a legacy alias seen in older pursuit files.
CLOSED_STAGES: frozenset[str] = frozenset(
    {
        Stage.CLOSED_WON,
        Stage.CLOSED_LOST,
        "won-lost",  # legacy alias
    }
)

# All terminal stages — closed + informal aliases seen in the wild.
# Use TERMINAL_STAGES for stall detection and "is this pursuit done?" checks.
# Use CLOSED_STAGES for pipeline filtering and archival checks.
TERMINAL_STAGES: frozenset[str] = CLOSED_STAGES | frozenset(
    {
        "closed",  # non-standard but occurs in some imports
        "won",  # shorthand
        "lost",  # shorthand
    }
)

# Active pipeline stages in deal-progression order.
# Index 0 = earliest stage. Use .index() for stage comparison.
# "pre-pipeline" and "prospect" are pre-qualifying stages tracked in the funnel.
PIPELINE_STAGES: tuple[str, ...] = (
    Stage.PRE_PIPELINE,
    Stage.PROSPECT,
    Stage.QUALIFY,
    Stage.DISCOVER,
    Stage.VALIDATE,
    Stage.PROPOSE,
    Stage.NEGOTIATE,
)

# Canonical pipeline ordering as a list of Stage members.
# Use for sort keys and index-based stage comparison.
STAGE_ORDER: list[Stage] = [
    Stage.PRE_PIPELINE,
    Stage.PROSPECT,
    Stage.QUALIFY,
    Stage.DISCOVER,
    Stage.VALIDATE,
    Stage.PROPOSE,
    Stage.NEGOTIATE,
]

# All known stages (pipeline + closed). Does not include informal aliases
# from TERMINAL_STAGES (closed, won, lost).
ALL_STAGES: frozenset[str] = frozenset(PIPELINE_STAGES) | CLOSED_STAGES
