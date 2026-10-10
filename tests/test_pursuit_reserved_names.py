"""Reserved pursuit file names: one definition, refused at creation, disclosed when skipped."""

import ast
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

import fieldkit
from fieldkit.commands.pursuit.audit_cmd import cli as audit_cli
from fieldkit.commands.pursuit.create_cmd import cli as create_cli
from fieldkit.commands.pursuit.forecast import cli as forecast_cli
from fieldkit.commands.pursuit.pipeline_health import cli as health_cli
from fieldkit.commands.pursuit.rename_cmd import cli as rename_cli
from fieldkit.pursuit.io import find_reserved_pursuit_files, is_reserved_pursuit_path
from fieldkit.pursuit.utils import iterate_pursuits

ACCOUNT = "acme-corp"
PURSUIT = "---\nstage: validate\nsf_acv: 100000\n---\n# Fictional deal\n"


def _workspace(root: Path, *names: str, account: str = ACCOUNT) -> Path:
    pursuits = root / "accounts" / account / "pursuits"
    pursuits.mkdir(parents=True)
    for name in names:
        (pursuits / name).write_text(PURSUIT, encoding="utf-8")
    return root


def _snapshot(root: Path) -> dict[Path, bytes]:
    return {p: p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.mark.unit
@pytest.mark.parametrize(
    ("relative", "reserved"),
    [
        ("accounts/acme-corp/pursuits/template.md", True),
        ("accounts/acme-corp/pursuits/gmail-intel.md", True),
        ("accounts/acme-corp/pursuits/gmail-intel-rollout.md", False),
        ("accounts/acme-corp/pursuits/real-deal.md", False),
        ("accounts/gmail-intel-co/pursuits/real-deal.md", False),
        ("accounts/acme-corp/pursuits/template.md.bak", False),
    ],
)
def test_predicate_matches_exact_file_name(relative: str, reserved: bool) -> None:
    assert is_reserved_pursuit_path(Path(relative)) is reserved


@pytest.mark.unit
def test_iterate_pursuits_matches_exact_names_only(tmp_path: Path) -> None:
    _workspace(tmp_path, "template.md", "gmail-intel.md", "gmail-intel-rollout.md", "real-deal.md")
    (tmp_path / "accounts" / "gmail-intel-co" / "pursuits").mkdir(parents=True)
    (tmp_path / "accounts" / "gmail-intel-co" / "pursuits" / "real-deal.md").write_text(PURSUIT, encoding="utf-8")

    found = [str(p.relative_to(tmp_path)) for p in iterate_pursuits(tmp_path)]

    assert found == [
        "accounts/acme-corp/pursuits/gmail-intel-rollout.md",
        "accounts/acme-corp/pursuits/real-deal.md",
        "accounts/gmail-intel-co/pursuits/real-deal.md",
    ]


@pytest.mark.unit
def test_reserved_literals_live_only_in_pursuit_io() -> None:
    """A second copy of a reserved file name is how the definitions drifted apart."""
    source_root = Path(fieldkit.__file__).parent
    reserved = {"gmail-intel.md", "template.md"}
    offenders = [
        f"{path.relative_to(source_root)}:{node.lineno}"
        for path in sorted(source_root.rglob("*.py"))
        if path != source_root / "pursuit" / "io.py"
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Constant) and node.value in reserved
    ]
    assert offenders == []


@pytest.mark.unit
@pytest.mark.parametrize("name", ["template", "gmail-intel", "Template", "Gmail Intel"])
@pytest.mark.parametrize("extra", [[], ["--dry-run"]])
def test_create_refuses_reserved_name_text(tmp_path: Path, name: str, extra: list[str]) -> None:
    root = _workspace(tmp_path)
    before = _snapshot(root)
    with patch("fieldkit.commands.pursuit.create_cmd._data_root", return_value=root):
        result = CliRunner().invoke(create_cli, ["--account", ACCOUNT, "--name", name, *extra])

    assert result.exit_code == 3
    assert "reserved pursuit name" in result.stderr
    assert _snapshot(root) == before
    assert not (root / "accounts" / ACCOUNT / "pursuits" / "template.md").exists()


@pytest.mark.unit
def test_create_refusal_is_machine_readable_under_json(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    with patch("fieldkit.commands.pursuit.create_cmd._data_root", return_value=root):
        result = CliRunner().invoke(create_cli, ["--account", ACCOUNT, "--name", "template", "--json"])

    assert result.exit_code == 3
    payload = json.loads(result.stdout)
    assert payload["error"] == "reserved_name"
    assert payload["slug"] == "template"
    assert list((root / "accounts" / ACCOUNT / "pursuits").iterdir()) == []


@pytest.mark.unit
def test_create_accepts_name_containing_reserved_word(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    with patch("fieldkit.commands.pursuit.create_cmd._data_root", return_value=root):
        result = CliRunner().invoke(create_cli, ["--account", ACCOUNT, "--name", "gmail-intel-rollout", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["created"] is True
    assert (root / "accounts" / ACCOUNT / "pursuits" / "gmail-intel-rollout.md").is_file()


@pytest.mark.unit
@pytest.mark.parametrize("target", ["template", "gmail-intel"])
@pytest.mark.parametrize("extra", [[], ["--dry-run"], ["--json"]])
def test_rename_refuses_reserved_target_without_changes(tmp_path: Path, target: str, extra: list[str]) -> None:
    root = _workspace(tmp_path, "real-deal.md")
    watchers = root / "watchers"
    watchers.mkdir()
    state = {f"{ACCOUNT}/real-deal": {"pursuit": "real-deal"}}
    for name in ("pursuit-stall-state.json", "close-date-countdown-state.json"):
        (watchers / name).write_text(json.dumps(state), encoding="utf-8")
    before = _snapshot(root)

    with patch("fieldkit.commands.pursuit.rename_cmd._data_root", return_value=root):
        result = CliRunner().invoke(rename_cli, ["--account", ACCOUNT, "--from", "real-deal", "--to", target, *extra])

    assert result.exit_code == 3
    assert "reserved pursuit name" in (json.loads(result.stdout)["message"] if "--json" in extra else result.stderr)
    assert _snapshot(root) == before


@pytest.mark.unit
def test_rename_to_ordinary_name_still_succeeds(tmp_path: Path) -> None:
    root = _workspace(tmp_path, "real-deal.md")
    with patch("fieldkit.commands.pursuit.rename_cmd._data_root", return_value=root):
        result = CliRunner().invoke(rename_cli, ["--account", ACCOUNT, "--from", "real-deal", "--to", "gmail-intel-co"])

    assert result.exit_code == 0
    assert (root / "accounts" / ACCOUNT / "pursuits" / "gmail-intel-co.md").is_file()


def _run_reports(root: Path, *, as_json: bool) -> dict[str, tuple[int, str, str]]:
    """Run forecast, health, and audit against ``root``; return (exit, stdout, stderr) for each."""
    flags = ["--json"] if as_json else []
    runs = {
        "forecast": ("fieldkit.commands.pursuit.forecast.get_fieldkit_home", forecast_cli, ["--quota", "200000"]),
        "health": ("fieldkit.commands.pursuit.pipeline_health.get_fieldkit_home", health_cli, []),
        "audit": ("fieldkit.commands.pursuit.audit_cmd.get_fieldkit_home", audit_cli, ["--account", ACCOUNT]),
    }
    outcomes: dict[str, tuple[int, str, str]] = {}
    for name, (target, command, args) in runs.items():
        with patch(target, return_value=root):
            result = CliRunner().invoke(command, [*args, *flags])
        outcomes[name] = (result.exit_code, result.stdout, result.stderr)
    return outcomes


@pytest.mark.unit
@pytest.mark.parametrize("as_json", [True, False])
def test_issue_85_ordinary_pursuit_stays_visible_beside_reserved_files(tmp_path: Path, as_json: bool) -> None:
    root = _workspace(tmp_path, "template.md", "gmail-intel.md", "real-deal.md")

    outcomes = _run_reports(root, as_json=as_json)

    for name, (code, stdout, _stderr) in outcomes.items():
        assert code in {0, 1}, name
        assert "real-deal" in stdout, name


@pytest.mark.unit
@pytest.mark.parametrize("as_json", [True, False])
def test_reports_name_skipped_reserved_files_without_changing_status(tmp_path: Path, as_json: bool) -> None:
    plain = _run_reports(_workspace(tmp_path / "plain", "real-deal.md"), as_json=as_json)
    reserved_root = _workspace(tmp_path / "reserved", "gmail-intel.md", "template.md", "real-deal.md")
    with_reserved = _run_reports(reserved_root, as_json=as_json)

    for name, (code, _stdout, stderr) in with_reserved.items():
        assert code == plain[name][0], name
        for reserved in ("gmail-intel.md", "template.md"):
            assert f"WARNING: {ACCOUNT}/pursuits/{reserved}: skipped — reserved pursuit file name" in stderr, name
        assert "reserved" not in plain[name][2], name


@pytest.mark.unit
def test_forecast_json_assessment_lists_reserved_paths(tmp_path: Path) -> None:
    root = _workspace(tmp_path, "gmail-intel.md", "real-deal.md")
    with patch("fieldkit.commands.pursuit.forecast.get_fieldkit_home", return_value=root):
        result = CliRunner().invoke(forecast_cli, ["--json"])

    payload = json.loads(result.stdout)
    assert result.exit_code == 0
    assert payload["assessment"]["reserved"] == [f"{ACCOUNT}/pursuits/gmail-intel.md"]
    assert payload["assessment"]["excluded"] == 1


@pytest.mark.unit
def test_find_reserved_pursuit_files_honors_account_filter(tmp_path: Path) -> None:
    root = _workspace(tmp_path, "template.md")
    (root / "accounts" / "globex" / "pursuits").mkdir(parents=True)
    (root / "accounts" / "globex" / "pursuits" / "gmail-intel.md").write_text(PURSUIT, encoding="utf-8")

    assert find_reserved_pursuit_files(root, None) == [
        f"{ACCOUNT}/pursuits/template.md",
        "globex/pursuits/gmail-intel.md",
    ]
    assert find_reserved_pursuit_files(root, "globex") == ["globex/pursuits/gmail-intel.md"]
