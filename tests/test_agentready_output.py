"""Fresh assessment outputs cannot inherit a previous run's report."""

from pathlib import Path

import pytest

from scripts import agentready_output


@pytest.mark.unit
def test_begin_invalidates_only_previous_latest_pointer(tmp_path: Path) -> None:
    """Old timestamped evidence survives, but cannot satisfy a new run."""
    previous = tmp_path / "assessment-old.json"
    previous.write_text("{}", encoding="utf-8")
    latest = tmp_path / "assessment-latest.json"
    latest.symlink_to(previous.name)

    run = agentready_output.begin_run(tmp_path)

    assert run.parent == tmp_path
    assert not latest.exists()
    assert previous.read_text(encoding="utf-8") == "{}"
    with pytest.raises(FileNotFoundError):
        agentready_output.publish_run(run, tmp_path)


@pytest.mark.unit
def test_publish_accepts_confined_tool_symlink(tmp_path: Path) -> None:
    """The pinned assessor's relative latest symlink is supported."""
    run = agentready_output.begin_run(tmp_path)
    report = run / "assessment-new.json"
    report.write_text("{}", encoding="utf-8")
    (run / "assessment-latest.json").symlink_to(report.name)

    result = agentready_output.publish_run(run, tmp_path)

    assert result is None
    assert (tmp_path / "assessment-latest.json").read_text(encoding="utf-8") == "{}"


@pytest.mark.unit
def test_publish_rejects_link_to_old_report(tmp_path: Path) -> None:
    """A freshly created pointer to prior evidence is still stale."""
    old = tmp_path / "old.json"
    old.write_text("{}", encoding="utf-8")
    run = agentready_output.begin_run(tmp_path)
    (run / "assessment-latest.json").symlink_to(old)

    with pytest.raises(ValueError, match="outside"):
        agentready_output.publish_run(run, tmp_path)
