"""Damaged inputs remain visible in deterministic read-only portfolio reports."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from fieldkit.__main__ import main
from fieldkit.commands.pursuit.audit import audit_file
from fieldkit.commands.pursuit.forecast import compute_forecast
from fieldkit.commands.pursuit.pipeline_health import health_check
from fieldkit.pursuit.io import ReportAssessment, ReportFailure, read_pursuit_for_report, scan_report_inputs

pytestmark = pytest.mark.unit


def _portfolio(root: Path, content: bytes, mixed: bool = True) -> Path:
    pursuits = root / "accounts/acme-fictional/pursuits"
    pursuits.mkdir(parents=True)
    broken = pursuits / "broken.md"
    broken.write_bytes(content)
    if mixed:
        (pursuits / "pilot.md").write_text(
            "---\nstage: validate\nsf_acv: 100000\n---\n# Fictional pilot\n", encoding="utf-8"
        )
    return broken


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (b"---\nstage: [unterminated\n---\n# private-content\n", "malformed YAML"),
        (b"# No block\n", "missing frontmatter"),
        (b"\xff", "invalid UTF-8"),
        (b"---\n- invalid\n---\n", "invalid frontmatter mapping"),
        (b"---\nnull\n---\n", "invalid frontmatter mapping"),
    ],
)
@pytest.mark.parametrize("command", ["health", "forecast"])
@pytest.mark.parametrize("mixed", [True, False])
@pytest.mark.parametrize("as_json", [True, False])
def test_reports_disclose_failed_input(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    content: bytes,
    reason: str,
    command: str,
    mixed: bool,
    as_json: bool,
) -> None:
    _portfolio(tmp_path, content, mixed)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*.md")}
    with patch(
        f"fieldkit.commands.pursuit.{'pipeline_health' if command == 'health' else 'forecast'}.get_fieldkit_home",
        return_value=tmp_path,
    ):
        result = main(
            [
                "pursuit",
                command,
                *(["--json"] if as_json else []),
                *(["--quota", "200000"] if command == "forecast" else []),
            ]
        )
    assert result == 1
    output = capsys.readouterr()
    assert "acme-fictional/pursuits/broken.md" in output.err
    assert reason in output.err
    assert "assessment incomplete" in output.err
    assert "Traceback" not in output.err
    assert str(tmp_path) not in output.err
    assert "private-content" not in output.err
    if as_json and command == "forecast":
        payload = json.loads(output.out)
        assert payload["assessment"] == {
            "scanned": 2 if mixed else 1,
            "included": 1 if mixed else 0,
            "excluded": 0,
            "failures": [{"relative_path": "acme-fictional/pursuits/broken.md", "reason": reason}],
            "reserved": [],
        }
        assert payload["weighted"] == (25000 if mixed else 0)
    elif as_json:
        # Health keeps its list contract; the failure is disclosed on stderr and by exit 1.
        payload = json.loads(output.out)
        assert isinstance(payload, list)
        assert len(payload) == (1 if mixed else 0)
        if mixed:
            assert payload[0]["qualification_status"] == "unavailable"
    else:
        assert "Assessment incomplete:" in output.out
    assert {p: p.read_bytes() for p in tmp_path.rglob("*.md")} == before


@pytest.mark.parametrize("command", ["health", "forecast"])
def test_unreadable_record_is_disclosed(tmp_path: Path, capsys: pytest.CaptureFixture[str], command: str) -> None:
    broken = _portfolio(tmp_path, b"---\nstage: validate\n---\n")
    read_text = Path.read_text

    def read(path: Path, *args: object, **kwargs: object) -> str:
        if path == broken:
            raise PermissionError("private operating system details")
        return read_text(path, encoding="utf-8")

    with (
        patch.object(Path, "read_text", read),
        patch(
            f"fieldkit.commands.pursuit.{'pipeline_health' if command == 'health' else 'forecast'}.get_fieldkit_home",
            return_value=tmp_path,
        ),
    ):
        result = main(["pursuit", command, "--json", *(["--quota", "200000"] if command == "forecast" else [])])
    assert result == 1
    output = capsys.readouterr()
    if command == "forecast":
        assert json.loads(output.out)["assessment"]["failures"][0]["reason"] == "unreadable input"
    else:
        assert isinstance(json.loads(output.out), list)
        assert "unreadable input" in output.err
    assert "private operating system details" not in output.err


def test_intentional_exclusions_are_complete(tmp_path: Path) -> None:
    broken = _portfolio(tmp_path, b"unused")
    broken.unlink()
    for name, stage in [
        ("template", "validate"),
        ("gmail-intel", "validate"),
        ("done", "closed-lost"),
        ("early", "pre-pipeline"),
    ]:
        (broken.parent / f"{name}.md").write_text(f"---\nstage: {stage}\n---\n", encoding="utf-8")
    forecast = compute_forecast(tmp_path)
    assert forecast.assessment.complete
    assert forecast.assessment.scanned == 5
    assert forecast.assessment.included == 1
    assert forecast.assessment.excluded == 4
    assessment = ReportAssessment()
    items = health_check(tmp_path, assessment=assessment)
    assert len(items) == 1
    assert assessment.complete
    assert assessment.scanned == 5
    assert assessment.excluded == 4


def test_scan_report_inputs_partitions_every_scanned_file(tmp_path: Path) -> None:
    broken = _portfolio(tmp_path, b"---\n- invalid\n---\n")
    (broken.parent / "template.md").write_text("---\nstage: validate\n---\n", encoding="utf-8")
    other = tmp_path / "accounts/other-fictional/pursuits"
    other.mkdir(parents=True)
    (other / "renewal.md").write_text("---\nstage: closed-won\n---\n", encoding="utf-8")

    assessment = ReportAssessment()
    inputs = list(scan_report_inputs(tmp_path, None, assessment))
    assessment.finish(1)

    assert [i.relative_path for i in inputs] == [
        "acme-fictional/pursuits/pilot.md",
        "other-fictional/pursuits/renewal.md",
    ]
    assert inputs[0].frontmatter["stage"] == "validate"
    assert assessment.failures == [ReportFailure("acme-fictional/pursuits/broken.md", "invalid frontmatter mapping")]
    assert (assessment.scanned, assessment.included, assessment.excluded) == (4, 1, 2)

    filtered = ReportAssessment()
    assert [i.relative_path for i in scan_report_inputs(tmp_path, "other-fictional", filtered)] == [
        "other-fictional/pursuits/renewal.md"
    ]
    assert filtered.scanned == 1


def test_reader_and_audit_distinguish_malformed_yaml(tmp_path: Path) -> None:
    path = _portfolio(tmp_path, b"---\nstage: [invalid\n---\n", mixed=False)
    outcome = read_pursuit_for_report(path)
    assert outcome.error == "malformed YAML"
    assert outcome.frontmatter is None
    audit = audit_file(path)
    assert audit.parse_error == "malformed YAML"
    assert "missing" not in audit.findings[0].message.lower()
