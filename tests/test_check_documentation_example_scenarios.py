"""Contracts for installed-artifact documentation example scenarios."""

import hashlib
from pathlib import Path

import pytest

from scripts import check_documentation_example_scenarios as scenarios
from scripts import smoke_artifact

pytestmark = pytest.mark.unit


def _report(artifact: Path, criterion_ids: set[str]) -> smoke_artifact.SmokeReport:
    """Build passing deterministic smoke evidence for one artifact."""
    return smoke_artifact.SmokeReport(
        schema_version=2,
        source_revision="a" * 40,
        artifact_name=artifact.name,
        artifact_sha256=hashlib.sha256(artifact.read_bytes()).hexdigest(),
        profile="base",
        python="3.11.0",
        platform="test",
        criteria=tuple(smoke_artifact.SmokeCriterion(criterion_id, "pass") for criterion_id in criterion_ids),
    )


def test_check_binds_each_safe_documentation_block_to_fixed_smoke_criteria(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A generic artifact build cannot stand in for the public command examples."""
    wheel = tmp_path / "fieldkit_cli-1.0.0-py3-none-any.whl"
    sdist = tmp_path / "fieldkit_cli-1.0.0.tar.gz"
    wheel.write_bytes(b"wheel")
    sdist.write_bytes(b"sdist")
    monkeypatch.setattr(scenarios, "_safe_block_identifiers", lambda _repo: set(scenarios._BLOCK_CRITERIA))
    monkeypatch.setattr(scenarios, "_build_artifacts", lambda *_args: (wheel, sdist))
    expected_criteria = set().union(*scenarios._BLOCK_CRITERIA.values())
    monkeypatch.setattr(
        scenarios.smoke_artifact,
        "smoke",
        lambda artifact, **_kwargs: _report(artifact, expected_criteria),
    )

    evidence = scenarios.check(tmp_path)

    assert evidence.status == "pass"
    assert set(evidence.scenarios) == set(scenarios._BLOCK_CRITERIA)
    assert evidence.scenarios["readme.md.block-2"] == ("SMOKE119", "SMOKE120", "SMOKE113", "SMOKE112", "SMOKE106")
    assert evidence.scenarios["docs.getting.started.md.block-3"] == ("SMOKE119", "SMOKE120")
    assert evidence.scenarios["docs.getting.started.md.block-5"] == ("SMOKE113", "SMOKE115")
    assert evidence.scenarios["docs.getting.started.md.block-6"] == (
        "SMOKE113",
        "SMOKE115",
        "SMOKE112",
        "SMOKE106",
    )
    assert evidence.scenarios["docs.getting.started.md.block-7"] == ("SMOKE112", "SMOKE106")
    assert evidence.scenarios["docs.getting.started.md.block-8"] == ("SMOKE116", "SMOKE117", "SMOKE118")
    assert evidence.scenarios["docs.reference.config.file.md.block-2"] == ("SMOKE113", "SMOKE115")
    assert evidence.scenarios["docs.reference.config.file.md.block-6"] == ("SMOKE112",)
    assert evidence.scenarios["docs.reference.troubleshooting.md.block-1"] == (
        "SMOKE101",
        "SMOKE105",
        "SMOKE112",
    )
    assert evidence.scenarios["docs.user.guide.md.block-1"] == ("SMOKE113", "SMOKE102", "SMOKE106")
    assert evidence.scenarios["docs.guides.watchers.md.block-1"] == ("SMOKE121",)
    assert evidence.scenarios["docs.guides.watchers.md.block-2"] == ("SMOKE122",)
    assert evidence.scenarios["docs.guides.init.md.block-2"] == ("SMOKE123",)
    assert evidence.scenarios["docs.guides.init.md.block-3"] == ("SMOKE123",)
    assert evidence.scenarios["docs.guides.morning.brief.md.block-1"] == ("SMOKE112", "SMOKE124")
    assert evidence.scenarios["docs.guides.morning.brief.md.block-3"] == ("SMOKE124",)
    assert {report.artifact_name for report in evidence.artifacts} == {wheel.name, sdist.name}


def test_check_fails_when_a_documented_command_criterion_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absent command evidence fails closed instead of inheriting an aggregate pass."""
    wheel = tmp_path / "fieldkit_cli-1.0.0-py3-none-any.whl"
    sdist = tmp_path / "fieldkit_cli-1.0.0.tar.gz"
    wheel.write_bytes(b"wheel")
    sdist.write_bytes(b"sdist")
    monkeypatch.setattr(scenarios, "_safe_block_identifiers", lambda _repo: set(scenarios._BLOCK_CRITERIA))
    monkeypatch.setattr(scenarios, "_build_artifacts", lambda *_args: (wheel, sdist))
    monkeypatch.setattr(
        scenarios.smoke_artifact,
        "smoke",
        lambda artifact, **_kwargs: _report(artifact, {"SMOKE002"}),
    )

    evidence = scenarios.check(tmp_path)

    assert evidence.status == "fail"
    assert "readme.md.block-2:SMOKE113" in evidence.failures
