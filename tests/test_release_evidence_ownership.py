"""Manual evidence has one reusable owner independent of the release CLI."""

import importlib
from pathlib import Path

import pytest

from scripts import release_approval_input, release_check

pytestmark = pytest.mark.unit


def test_approval_uses_canonical_evidence_validator() -> None:
    evidence = importlib.import_module("scripts.release_manual_evidence")
    assert evidence.validate_bytes.__module__ == evidence.__name__
    assert vars(release_approval_input)["release_manual_evidence"] is evidence
    assert vars(release_check)["release_manual_evidence"] is evidence
    assert evidence.Criterion.__module__ == evidence.__name__
    assert not hasattr(release_check, "_manual_criteria_from_bytes")
    assert not hasattr(release_check, "Criterion")


def test_evidence_validation_does_not_depend_on_cli_or_approval() -> None:
    evidence = importlib.import_module("scripts.release_manual_evidence")
    frontier = importlib.import_module("scripts.release_frontier_evidence")
    assert frontier.valid_proof.__module__ == frontier.__name__
    for module in (evidence, frontier):
        assert module.__file__ is not None
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "import release_check" not in source
        assert "import release_approval_input" not in source
