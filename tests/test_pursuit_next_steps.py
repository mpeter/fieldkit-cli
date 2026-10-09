"""Issue 71: next-action observations are independent of date urgency."""

import json
from datetime import date
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from fieldkit.commands.pursuit.audit import audit_directory, audit_file
from fieldkit.commands.pursuit.audit_cmd import cli
from fieldkit.pursuit.next_steps import NextStepFinding, check_next_steps

pytestmark = pytest.mark.unit

ACTIVE_STAGES = ("prospect", "qualify", "discover", "validate", "propose", "negotiate")
MISSING_MESSAGE = "Missing sf_next_steps — confirm and record the next agreed action"
INVALID_MESSAGE = "sf_next_steps must be text or null"
NEXT_FIELDS = ({}, {"sf_next_steps": None}, {"sf_next_steps": ""}, {"sf_next_steps": " \t\n"})


@pytest.mark.parametrize("stage", ACTIVE_STAGES)
@pytest.mark.parametrize("fields", [*NEXT_FIELDS, {"sf_next_steps": "Confirm discovery agenda"}])
def test_domain_active_stages(stage: str, fields: dict[str, object]) -> None:
    result = check_next_steps({"stage": stage, **fields})
    assert result == (
        None
        if fields.get("sf_next_steps") == "Confirm discovery agenda"
        else NextStepFinding("WARNING", MISSING_MESSAGE)
    )


@pytest.mark.parametrize(
    "stage", ["pre-pipeline", "closed-won", "closed-lost", "won-lost", "closed", "won", "lost", "unknown", None, 42]
)
@pytest.mark.parametrize("value", [None, "", False])
def test_domain_excludes_inactive_and_unknown_stages(stage: object, value: object) -> None:
    result = check_next_steps({"stage": stage, "sf_next_steps": value})
    assert result is None


@pytest.mark.parametrize("key", ["sf_next_steps", "sf-next-steps"])
@pytest.mark.parametrize("value", [True, False, 0, 123, 1.5, [], ["Call"], {}, {"action": "Call"}])
def test_domain_invalid_types(key: str, value: object) -> None:
    result = check_next_steps({"stage": "discover", key: value})
    assert result == NextStepFinding("ERROR", INVALID_MESSAGE)


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"sf-next-steps": "Call"}, None),
        ({"sf-next-steps": ""}, NextStepFinding("WARNING", MISSING_MESSAGE)),
        ({"sf_next_steps": "", "sf-next-steps": "Call"}, NextStepFinding("WARNING", MISSING_MESSAGE)),
        ({"sf_next_steps": None, "sf-next-steps": "Call"}, NextStepFinding("WARNING", MISSING_MESSAGE)),
        ({"sf_next_steps": " \n", "sf-next-steps": "Call"}, NextStepFinding("WARNING", MISSING_MESSAGE)),
        ({"sf_next_steps": "Call", "sf-next-steps": False}, None),
        ({"sf_next_steps": False, "sf-next-steps": "Call"}, NextStepFinding("ERROR", INVALID_MESSAGE)),
    ],
)
def test_domain_canonical_key_presence_wins(fields: dict[str, object], expected: NextStepFinding | None) -> None:
    result = check_next_steps({"stage": "discover", **fields})
    assert result == expected


def _write_pursuit(path: Path, fields: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frontmatter = {
        "stage": "discover",
        "gate-status": "pending",
        "last-transition": "2026-10-02",
        "transition-history": [],
        **fields,
    }
    path.write_text("---\n" + yaml.safe_dump(frontmatter) + "---\n\n# Fictional pursuit\n", encoding="utf-8")


@pytest.mark.parametrize("stage", ACTIVE_STAGES)
@pytest.mark.parametrize(
    "close_date",
    [None, "malformed", "2026-10-03", "2026-10-04", "2026-10-05", "2026-11-03", "2026-11-04", "2026-12-31"],
)
@pytest.mark.parametrize("fields", [*NEXT_FIELDS, {"sf_next_steps": "Confirm agenda"}])
def test_audit_missing_step_independent_of_date(
    tmp_path: Path, stage: str, close_date: str | None, fields: dict[str, object]
) -> None:
    path = tmp_path / "fictional.md"
    date_fields = {} if close_date is None else {"sf_close_date": close_date}
    _write_pursuit(path, {"stage": stage, **date_fields, **fields})
    original = path.read_bytes()
    result = audit_file(path, today=date(2026, 10, 4))
    assert result.qualification_status == "unavailable"
    expected = [] if fields.get("sf_next_steps") == "Confirm agenda" else [("WARNING", MISSING_MESSAGE)]
    assert [(f.level, f.message) for f in result.findings if "sf_next_steps" in f.message] == expected
    assert not result.criticals
    assert path.read_bytes() == original
    if close_date == "2026-10-03":
        assert any("SF close date overdue" in f.message for f in result.errors)
    if close_date in {"2026-10-04", "2026-10-05", "2026-11-03"} and stage not in {"propose", "negotiate"}:
        assert any("timeline risk" in f.message for f in result.warnings)


@pytest.mark.parametrize("value", [True, 12, [], {}])
def test_audit_invalid_step_is_one_error(tmp_path: Path, value: object) -> None:
    path = tmp_path / "fictional.md"
    _write_pursuit(path, {"sf_next_steps": value, "sf_close_date": "2026-10-09"})
    result = audit_file(path, today=date(2026, 10, 4))
    assert result.category == "ERROR"
    assert [(f.level, f.message) for f in result.findings if "sf_next_steps" in f.message] == [
        ("ERROR", INVALID_MESSAGE)
    ]


@pytest.mark.parametrize("fields", [{"sf-next-steps": "Call"}, {"sf_next_steps": None, "sf-next-steps": "Call"}])
def test_audit_retains_legacy_naming_finding(tmp_path: Path, fields: dict[str, object]) -> None:
    path = tmp_path / "fictional.md"
    _write_pursuit(path, fields)
    result = audit_file(path, today=date(2026, 10, 4))
    assert result.category == "WARNING"
    assert any("legacy hyphenated variant" in f.message for f in result.warnings)
    assert sum(f.message == MISSING_MESSAGE for f in result.findings) == int("sf_next_steps" in fields)


@pytest.mark.parametrize("stage", ["pre-pipeline", "closed-won", "closed-lost", "unknown", None])
def test_audit_inactive_and_unknown(tmp_path: Path, stage: str | None) -> None:
    path = tmp_path / "fictional.md"
    _write_pursuit(path, {"stage": stage})
    result = audit_file(path, today=date(2026, 10, 4))
    assert result.stage == str(stage).lower()
    assert not any(f.message == MISSING_MESSAGE for f in result.findings)
    if stage in {"unknown", None}:
        assert result.errors


def test_audit_malformed_frontmatter_stays_parse_failure(tmp_path: Path) -> None:
    path = tmp_path / "fictional.md"
    path.write_text("---\nstage: [\n---\n", encoding="utf-8")
    result = audit_file(path)
    assert result.parse_error is not None
    assert result.category == "ERROR"
    assert not any(f.message == MISSING_MESSAGE for f in result.findings)


@pytest.mark.parametrize("as_json", [True, False])
def test_cli_original_distant_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, as_json: bool) -> None:
    path = tmp_path / "accounts/acme-corp/pursuits/fictional-deal.md"
    _write_pursuit(
        path,
        {"sf_opportunity_id": "FICTIONAL-OPP-01", "sf_close_date": "2026-12-31", "sf_acv": 10000, "sf_next_steps": ""},
    )
    config = tmp_path / "config.yaml"
    config.write_text("fictional: true\n", encoding="utf-8")
    original = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    monkeypatch.setattr("fieldkit.commands.pursuit.audit_cmd._data_root", lambda: tmp_path)
    monkeypatch.setattr(
        "fieldkit.commands.pursuit.audit_cmd.audit_directory",
        lambda root, account_filter, today: audit_directory(
            root, account_filter=account_filter, today=date(2026, 10, 4)
        ),
    )
    result = CliRunner().invoke(cli, ["--account", "acme-corp", *(["--json"] if as_json else [])])
    assert result.exit_code == 1, result.output
    if as_json:
        payload = json.loads(result.output)
        assert payload[0]["qualification_status"] == "unavailable"
        assert payload[0]["findings"] == [{"level": "WARNING", "message": MISSING_MESSAGE}]
        assert {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == original
    else:
        assert MISSING_MESSAGE in result.output


def test_cli_compliant_action(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "accounts/acme-corp/pursuits/fictional.md"
    _write_pursuit(path, {"sf_next_steps": "Confirm agenda"})
    monkeypatch.setattr("fieldkit.commands.pursuit.audit_cmd._data_root", lambda: tmp_path)
    result = CliRunner().invoke(cli, ["--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)[0]["findings"] == []
