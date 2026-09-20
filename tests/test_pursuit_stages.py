"""Unit tests for fieldkit.pursuit.stages — canonical stage constants."""

import pytest

from fieldkit.pursuit.stages import ALL_STAGES, CLOSED_STAGES, PIPELINE_STAGES, TERMINAL_STAGES


@pytest.mark.unit
def test_closed_stages_contains_required_values() -> None:
    """CLOSED_STAGES must contain the three canonical closed values."""
    assert "closed-won" in CLOSED_STAGES
    assert "closed-lost" in CLOSED_STAGES
    assert "won-lost" in CLOSED_STAGES


@pytest.mark.unit
def test_terminal_stages_is_superset_of_closed_stages() -> None:
    """TERMINAL_STAGES must be a strict superset of CLOSED_STAGES."""
    assert CLOSED_STAGES < TERMINAL_STAGES


@pytest.mark.unit
def test_terminal_stages_contains_informal_aliases() -> None:
    """TERMINAL_STAGES includes informal shorthand aliases."""
    assert "closed" in TERMINAL_STAGES
    assert "won" in TERMINAL_STAGES
    assert "lost" in TERMINAL_STAGES


@pytest.mark.unit
def test_pipeline_stages_first_element() -> None:
    """PIPELINE_STAGES[0] must be 'pre-pipeline' (earliest stage)."""
    assert PIPELINE_STAGES[0] == "pre-pipeline"


@pytest.mark.unit
def test_pipeline_stages_last_element() -> None:
    """PIPELINE_STAGES[-1] must be 'negotiate' (latest active stage)."""
    assert PIPELINE_STAGES[-1] == "negotiate"


@pytest.mark.unit
def test_pipeline_stages_ordering() -> None:
    """PIPELINE_STAGES must contain all expected active stages in order."""
    expected = ("pre-pipeline", "prospect", "qualify", "discover", "validate", "propose", "negotiate")
    assert expected == PIPELINE_STAGES


@pytest.mark.unit
@pytest.mark.parametrize("stage", PIPELINE_STAGES)
def test_all_stages_contains_pipeline_stages(stage: str) -> None:
    """ALL_STAGES must contain every stage from PIPELINE_STAGES."""
    assert stage in ALL_STAGES, f"Expected {stage!r} in ALL_STAGES"


@pytest.mark.unit
@pytest.mark.parametrize("stage", sorted(CLOSED_STAGES))
def test_all_stages_contains_closed_stages(stage: str) -> None:
    """ALL_STAGES must contain every stage from CLOSED_STAGES."""
    assert stage in ALL_STAGES, f"Expected {stage!r} in ALL_STAGES"


@pytest.mark.unit
def test_all_stages_does_not_contain_informal_aliases() -> None:
    """ALL_STAGES excludes informal TERMINAL_STAGES aliases (closed, won, lost)."""
    assert "closed" not in ALL_STAGES
    assert "won" not in ALL_STAGES
    assert "lost" not in ALL_STAGES


@pytest.mark.unit
def test_won_lost_in_closed_stages_regression() -> None:
    """Regression: 'won-lost' must be in CLOSED_STAGES.

    Previously audit.py used only {"closed-won", "closed-lost"} (2 values),
    missing the legacy 'won-lost' alias. This test documents the behavioral
    change: won-lost is now treated as closed everywhere.
    """
    assert "won-lost" in CLOSED_STAGES


@pytest.mark.unit
def test_stage_constants_are_frozensets() -> None:
    """CLOSED_STAGES, TERMINAL_STAGES, and ALL_STAGES must be frozensets."""
    assert isinstance(CLOSED_STAGES, frozenset)
    assert isinstance(TERMINAL_STAGES, frozenset)
    assert isinstance(ALL_STAGES, frozenset)


@pytest.mark.unit
def test_pipeline_stages_is_tuple() -> None:
    """PIPELINE_STAGES must be a tuple (ordered, immutable)."""
    assert isinstance(PIPELINE_STAGES, tuple)
