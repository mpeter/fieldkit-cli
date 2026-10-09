"""Tests for ``fieldkit sf drift`` — portfolio pursuit-vs-Salesforce drift report."""

import json
from pathlib import Path
from types import TracebackType
from typing import Any, ClassVar

import pytest
from click.testing import CliRunner, Result

from fieldkit.commands.sf import drift
from fieldkit.sf.client import SFAPIError, SFAuthError, SFNotFoundError
from fieldkit.sf.types import OpportunitySObject

# Far-future close dates keep close-window flags out of tests that are not about them.
_FAR = "2099-03-01"


def _record(
    opp_id: str, stage: str = "Propose", close: str = _FAR, acv: float | None = 100000.0, closed: bool = False
) -> dict[str, Any]:
    return {
        "Id": opp_id,
        "Name": f"Deal {opp_id}",
        "StageName": stage,
        "CloseDate": close,
        "IsClosed": closed,
        "Consulting_Total_USD__c": acv,
    }


class FakeClient:
    """Stands in for SFDirectClient: returns configured records or raises the real SF errors."""

    instances: ClassVar[list["FakeClient"]] = []

    def __init__(self, responses: dict[str, dict[str, Any] | Exception], **_: object) -> None:
        self.responses = responses
        self.fetched: list[tuple[str, str | None]] = []
        FakeClient.instances.append(self)

    def __enter__(self) -> "FakeClient":
        return self

    def __exit__(self, *_: type[BaseException] | BaseException | TracebackType | None) -> None:
        return None

    def fetch_record(self, record_id: str, fields: str | None = None) -> OpportunitySObject:
        self.fetched.append((record_id, fields))
        response = self.responses[record_id]
        if isinstance(response, Exception):
            raise response
        return response  # type: ignore[return-value]


def _pursuit(root: Path, account: str, name: str, **fields: object) -> None:
    directory = root / "accounts" / account / "pursuits"
    directory.mkdir(parents=True, exist_ok=True)
    defaults: dict[str, object] = {
        "stage": "propose",
        "sf_stage": "Propose",
        "sf_close_date": _FAR,
        "sf_consulting_acv": "$100,000",
    }
    lines = [f"{key}: {json.dumps(value)}" for key, value in {**defaults, **fields}.items() if value is not None]
    (directory / f"{name}.md").write_text("---\n" + "\n".join(lines) + f"\n---\n# {name}\n", encoding="utf-8")


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    FakeClient.instances.clear()
    monkeypatch.setattr(drift, "get_fieldkit_home", lambda: tmp_path)
    monkeypatch.setattr(drift, "get_sf_session_id", lambda: "fake-session")
    monkeypatch.setattr(drift, "get_sf_rest_base_url", lambda: "https://example.my.salesforce.com")
    (tmp_path / "accounts").mkdir()
    return tmp_path


def _use_responses(monkeypatch: pytest.MonkeyPatch, responses: dict[str, dict[str, Any] | Exception]) -> None:
    monkeypatch.setattr(drift, "SFDirectClient", lambda **kwargs: FakeClient(responses, **kwargs))


def _run(*args: str) -> Result:
    return CliRunner().invoke(drift.cli, list(args))


def _report(result: Result) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(result.stdout)
    return payload


@pytest.mark.unit
def test_live_from_record_maps_salesforce_fields() -> None:
    live = drift.live_from_record(_record("006A", stage="Closed Won", acv=None, closed=True))  # type: ignore[arg-type]
    assert live == {"stage": "Closed Won", "close_date": _FAR, "consulting_acv": None, "is_closed": True}


@pytest.mark.unit
def test_complete_report_orders_red_first_and_uses_workspace_paths(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pursuit(workspace, "acme-corp", "clean", sf_opportunity_id="006A")
    _pursuit(workspace, "acme-corp", "drifted", sf_opportunity_id="006B")
    _pursuit(workspace, "acme-corp", "overdue", sf_opportunity_id="006C", sf_close_date="2020-01-01")
    _pursuit(workspace, "acme-corp", "unlinked")
    _pursuit(workspace, "acme-corp", "won", stage="closed-won", sf_opportunity_id="006D")
    _pursuit(workspace, "acme-corp", "early", stage="prospect", sf_opportunity_id="006E")
    _use_responses(
        monkeypatch,
        {
            "006A": _record("006A"),
            "006B": _record("006B", stage="Negotiate"),
            "006C": _record("006C", close="2020-01-01"),
        },
    )

    result = _run("--json")

    assert result.exit_code == 0, result.output
    report = _report(result)
    assert report["complete"] is True
    assert report["counts"] == {"red": 1, "yellow": 1, "green": 1, "total": 3}
    assert [row["pursuit"] for row in report["opportunities"]] == [
        "accounts/acme-corp/pursuits/overdue.md",
        "accounts/acme-corp/pursuits/drifted.md",
        "accounts/acme-corp/pursuits/clean.md",
    ]
    assert [flag["code"] for flag in report["opportunities"][1]["flags"]] == ["stage-mismatch", "sf-stage-drift"]
    assert {opp_id for opp_id, _ in FakeClient.instances[0].fetched} == {"006A", "006B", "006C"}


@pytest.mark.unit
def test_include_prospect_and_account_filter_narrow_scope(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _pursuit(workspace, "acme-corp", "early", stage="prospect", sf_opportunity_id="006E")
    _pursuit(workspace, "globex", "other", sf_opportunity_id="006F")
    _use_responses(monkeypatch, {"006E": _record("006E", stage="Prospect"), "006F": _record("006F")})

    result = _run("--json", "--account", "acme-corp", "--include-prospect")

    assert result.exit_code == 0, result.output
    assert [row["opportunity_id"] for row in _report(result)["opportunities"]] == ["006E"]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("error", "detail"),
    [
        pytest.param(
            SFNotFoundError("Opportunity 006B not found"), "opportunity not found in Salesforce", id="not-found"
        ),
        pytest.param(
            SFAPIError("HTTP 500: internal detail https://internal.example"),
            "Salesforce request failed; retry later",
            id="api-error",
        ),
    ],
)
def test_failed_fetch_is_a_red_row_and_partial_exit(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, error: Exception, detail: str
) -> None:
    _pursuit(workspace, "acme-corp", "clean", sf_opportunity_id="006A")
    _pursuit(workspace, "acme-corp", "broken", sf_opportunity_id="006B")
    _use_responses(monkeypatch, {"006A": _record("006A"), "006B": error})

    result = _run("--json")

    assert result.exit_code == 1
    report = _report(result)
    assert report["complete"] is False
    failed = report["opportunities"][0]
    assert failed["status"] == "RED"
    assert failed["flags"] == [{"level": "RED", "code": "sf-fetch-failed", "detail": detail}]
    assert "internal" not in result.output
    assert report["counts"]["green"] == 1


@pytest.mark.unit
def test_expired_session_propagates_instead_of_partial_report(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _pursuit(workspace, "acme-corp", "clean", sf_opportunity_id="006A")
    _use_responses(monkeypatch, {"006A": SFAuthError("session expired")})

    result = _run("--json")

    assert isinstance(result.exception, SFAuthError)
    assert result.stdout == ""


@pytest.mark.unit
def test_unreadable_pursuit_is_reported_and_valid_rows_kept(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _pursuit(workspace, "acme-corp", "clean", sf_opportunity_id="006A")
    broken = workspace / "accounts" / "acme-corp" / "pursuits" / "broken.md"
    broken.write_text("---\nstage: [unclosed\n---\n", encoding="utf-8")
    _use_responses(monkeypatch, {"006A": _record("006A")})

    result = _run("--json")

    assert result.exit_code == 1
    report = _report(result)
    assert report["unassessed"] == [{"pursuit": "accounts/acme-corp/pursuits/broken.md", "reason": "malformed YAML"}]
    assert report["counts"]["total"] == 1


@pytest.mark.unit
def test_missing_session_exits_auth_before_fetching(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _pursuit(workspace, "acme-corp", "clean", sf_opportunity_id="006A")
    _use_responses(monkeypatch, {"006A": _record("006A")})
    monkeypatch.setattr(drift, "get_sf_session_id", lambda: None)

    result = _run("--json")

    assert result.exit_code == 2
    assert "fieldkit auth sf" in result.stderr
    assert FakeClient.instances == []


@pytest.mark.unit
def test_no_linked_pursuits_needs_no_session(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _pursuit(workspace, "acme-corp", "unlinked")
    monkeypatch.setattr(drift, "get_sf_session_id", lambda: None)

    result = _run("--json")

    assert result.exit_code == 0
    assert _report(result)["counts"]["total"] == 0


@pytest.mark.unit
def test_missing_accounts_directory_is_a_data_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(drift, "get_fieldkit_home", lambda: tmp_path)

    result = _run()

    assert result.exit_code == 3
    assert "fieldkit init" in result.stderr


@pytest.mark.unit
def test_table_lists_unassessed_files_then_rows(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _pursuit(workspace, "acme-corp", "drifted", sf_opportunity_id="006B", sf_consulting_acv="$90,000")
    (workspace / "accounts" / "acme-corp" / "pursuits" / "broken.md").write_text("no frontmatter\n", encoding="utf-8")
    _use_responses(monkeypatch, {"006B": _record("006B")})

    result = _run()

    lines = result.stdout.splitlines()
    assert lines[0] == "NOT ASSESSED  accounts/acme-corp/pursuits/broken.md: missing frontmatter"
    assert lines[1].startswith("YELLOW  accounts/acme-corp/pursuits/drifted.md  acv-drift:")
    assert "0 RED, 1 YELLOW, 0 GREEN of 1 linked pursuits; 1 not assessed" in result.stdout
