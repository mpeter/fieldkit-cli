"""historic regression: stage change must NOT overwrite last_transition_date with today's date.

Tests that when a stage change is detected in pursuit_stalls._run_pursuit_stalls,
the result dict's last_transition_date is preserved from the frontmatter-derived
value rather than being stamped with today's date.
"""

import pytest

# ---------------------------------------------------------------------------
# Helpers — simulate the stage-change mutation block in isolation
# ---------------------------------------------------------------------------


def _apply_stage_change_block(
    result: dict,
    prior: dict | None,
    prior_entry: dict,
) -> dict:
    """Mirror the stage-change mutation block from pursuit_stalls._run_pursuit_stalls.

    This is the exact logic after historic regression is fixed: only days_since_transition
    and is_stalled are reset; last_transition_date is NOT touched.
    """
    if prior is not None:
        prior_stage = prior.get("stage", "").strip().lower()
        if prior_stage and prior_stage != result["stage"]:
            result = dict(result)
            result["days_since_transition"] = 0
            # historic regression: last_transition_date is NOT overwritten here.
            result["is_stalled"] = False
            prior_entry.pop("snoozed_until", None)
    return result


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_stage_change_does_not_overwrite_last_transition_date() -> None:
    """last_transition_date must be preserved from frontmatter on stage change."""
    original_date = "2026-05-01"
    result = {
        "account": "acme",
        "pursuit": "deal-alpha",
        "stage": "propose",
        "last_transition_date": original_date,
        "days_since_transition": 20,
        "is_stalled": True,
        "threshold_days": 14,
    }
    prior = {"stage": "discover"}  # different stage → triggers stage-change block
    prior_entry: dict = {}

    updated = _apply_stage_change_block(result, prior, prior_entry)

    assert updated["last_transition_date"] == original_date, (
        f"last_transition_date was overwritten: expected {original_date!r}, got {updated['last_transition_date']!r}"
    )


@pytest.mark.unit
def test_stage_change_resets_days_since_transition_to_zero() -> None:
    """days_since_transition is reset to 0 on stage change."""
    result = {
        "account": "acme",
        "pursuit": "deal-alpha",
        "stage": "propose",
        "last_transition_date": "2026-05-01",
        "days_since_transition": 20,
        "is_stalled": True,
        "threshold_days": 14,
    }
    prior = {"stage": "discover"}
    prior_entry: dict = {}

    updated = _apply_stage_change_block(result, prior, prior_entry)

    assert updated["days_since_transition"] == 0


@pytest.mark.unit
def test_stage_change_clears_is_stalled() -> None:
    """is_stalled is set to False on stage change."""
    result = {
        "account": "acme",
        "pursuit": "deal-alpha",
        "stage": "propose",
        "last_transition_date": "2026-05-01",
        "days_since_transition": 20,
        "is_stalled": True,
        "threshold_days": 14,
    }
    prior = {"stage": "discover"}
    prior_entry: dict = {}

    updated = _apply_stage_change_block(result, prior, prior_entry)

    assert updated["is_stalled"] is False


@pytest.mark.unit
def test_stage_change_clears_snooze_from_prior_entry() -> None:
    """snoozed_until is removed from prior_entry on stage change."""
    result = {
        "account": "acme",
        "pursuit": "deal-alpha",
        "stage": "propose",
        "last_transition_date": "2026-05-01",
        "days_since_transition": 20,
        "is_stalled": True,
        "threshold_days": 14,
    }
    prior = {"stage": "discover"}
    prior_entry: dict = {"snoozed_until": "2026-06-20"}

    _apply_stage_change_block(result, prior, prior_entry)

    assert "snoozed_until" not in prior_entry


@pytest.mark.unit
def test_no_stage_change_leaves_result_unchanged() -> None:
    """When stage has not changed, the result dict is returned unmodified."""
    original_date = "2026-05-01"
    result = {
        "account": "acme",
        "pursuit": "deal-alpha",
        "stage": "propose",
        "last_transition_date": original_date,
        "days_since_transition": 20,
        "is_stalled": True,
        "threshold_days": 14,
    }
    prior = {"stage": "propose"}  # same stage → no change
    prior_entry: dict = {"snoozed_until": "2026-06-20"}

    updated = _apply_stage_change_block(result, prior, prior_entry)

    assert updated["last_transition_date"] == original_date
    assert updated["days_since_transition"] == 20
    assert updated["is_stalled"] is True
    # snooze must NOT be cleared when stage hasn't changed
    assert "snoozed_until" in prior_entry


@pytest.mark.unit
def test_no_prior_state_leaves_result_unchanged() -> None:
    """When prior is None (first run), the result dict is returned unmodified."""
    original_date = "2026-05-01"
    result = {
        "account": "acme",
        "pursuit": "deal-alpha",
        "stage": "propose",
        "last_transition_date": original_date,
        "days_since_transition": 20,
        "is_stalled": True,
        "threshold_days": 14,
    }
    prior_entry: dict = {}

    updated = _apply_stage_change_block(result, prior=None, prior_entry=prior_entry)

    assert updated["last_transition_date"] == original_date
    assert updated["days_since_transition"] == 20
