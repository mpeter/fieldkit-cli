"""Read-only next-action validation of a local pursuit snapshot."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from fieldkit.pursuit.enums import Stage
from fieldkit.pursuit.stages import PIPELINE_STAGES


@dataclass(frozen=True)
class NextStepFinding:
    """A missing or invalid action in a recognized active pursuit."""

    level: Literal["WARNING", "ERROR"]
    message: str


def check_next_steps(frontmatter: Mapping[str, object]) -> NextStepFinding | None:
    """Check all active stages, preferring canonical key presence over legacy data.

    Dates and historical qualification have no bearing on this observation.
    Unknown stages are left to structural validation.
    """
    stage = frontmatter.get("stage")
    if not isinstance(stage, str) or stage.lower() not in PIPELINE_STAGES or stage.lower() == Stage.PRE_PIPELINE:
        return None
    value = frontmatter.get("sf_next_steps") if "sf_next_steps" in frontmatter else frontmatter.get("sf-next-steps")
    if value is not None and not isinstance(value, str):
        return NextStepFinding("ERROR", "sf_next_steps must be text or null")
    if value is None or not value.strip():
        return NextStepFinding("WARNING", "Missing sf_next_steps — confirm and record the next agreed action")
    return None
