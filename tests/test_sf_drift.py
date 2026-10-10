"""Tests for ``fieldkit sf drift`` — portfolio pursuit-vs-Salesforce drift report."""

import json
from pathlib import Path
from types import TracebackType
from typing import Any, ClassVar

import pytest
from click.testing import CliRunner, Result

from fieldkit.cli_exit import EXIT_AUTH, handle_cli_exception
from fieldkit.commands.sf import drift
from fieldkit.sf.client import SFAPIError, SFAuthError, SFNotFoundError
from fieldkit.sf.types import OpportunitySObject

_ID_A = "006A00000000000000"
_ID_B = "006B00000000000000"

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
        """Record the request and return, or raise, the configured response for *record_id*."""
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
    """A workspace with an empty ``accounts`` tree and a configured fake Salesforce session."""
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
    """The live snapshot keeps exactly the fields drift compares, including a null ACV."""
    live = drift.live_from_record(_record("006A00000000000000", stage="Closed Won", acv=None, closed=True))  # type: ignore[arg-type]
    assert live == {"stage": "Closed Won", "close_date": _FAR, "consulting_acv": None, "is_closed": True}


@pytest.mark.unit
def test_complete_report_orders_red_first_and_uses_workspace_paths(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pursuit(workspace, "acme-corp", "clean", sf_opportunity_id="006A00000000000000")
    _pursuit(workspace, "acme-corp", "drifted", sf_opportunity_id="006B00000000000000")
    _pursuit(workspace, "acme-corp", "overdue", sf_opportunity_id="006C00000000000000", sf_close_date="2020-01-01")
    _pursuit(workspace, "acme-corp", "unlinked")
    _pursuit(workspace, "acme-corp", "won", stage="closed-won", sf_opportunity_id="006D00000000000000")
    _pursuit(workspace, "acme-corp", "early", stage="prospect", sf_opportunity_id="006E00000000000000")
    _use_responses(
        monkeypatch,
        {
            "006A00000000000000": _record("006A00000000000000"),
            "006B00000000000000": _record("006B00000000000000", stage="Negotiate"),
            "006C00000000000000": _record("006C00000000000000", close="2020-01-01"),
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
    assert {opp_id for opp_id, _ in FakeClient.instances[0].fetched} == {
        "006A00000000000000",
        "006B00000000000000",
        "006C00000000000000",
    }


@pytest.mark.unit
def test_include_prospect_and_account_filter_narrow_scope(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _pursuit(workspace, "acme-corp", "early", stage="prospect", sf_opportunity_id="006E00000000000000")
    _pursuit(workspace, "globex", "other", sf_opportunity_id="006F00000000000000")
    _use_responses(
        monkeypatch,
        {
            "006E00000000000000": _record("006E00000000000000", stage="Prospect"),
            "006F00000000000000": _record("006F00000000000000"),
        },
    )

    result = _run("--json", "--account", "acme-corp", "--include-prospect")

    assert result.exit_code == 0, result.output
    assert [row["opportunity_id"] for row in _report(result)["opportunities"]] == ["006E00000000000000"]


@pytest.mark.unit
@pytest.mark.unit
def test_failed_request_is_a_red_row_and_partial_exit(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _pursuit(workspace, "acme-corp", "clean", sf_opportunity_id=_ID_A)
    _pursuit(workspace, "acme-corp", "broken", sf_opportunity_id=_ID_B)
    _use_responses(monkeypatch, {_ID_A: _record(_ID_A), _ID_B: SFAPIError("HTTP 500: detail https://internal.example")})

    result = _run("--json")

    assert result.exit_code == 1
    report = _report(result)
    assert report["complete"] is False
    failed = report["opportunities"][0]
    assert failed["status"] == "RED"
    assert failed["flags"] == [{"level": "RED", "code": "sf-fetch-failed", "detail": "Salesforce request failed"}]
    assert "internal" not in result.output
    assert report["counts"]["green"] == 1


@pytest.mark.unit
def test_missing_opportunity_is_a_finding_in_a_complete_report(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale id is answered definitively; retrying cannot help, so the report stays complete."""
    _pursuit(workspace, "acme-corp", "stale", sf_opportunity_id=_ID_B)
    _use_responses(monkeypatch, {_ID_B: SFNotFoundError("Opportunity not found")})

    result = _run("--json")

    assert result.exit_code == 0
    report = _report(result)
    assert report["complete"] is True
    assert [flag["code"] for flag in report["opportunities"][0]["flags"]] == ["opportunity-not-found"]


@pytest.mark.unit
def test_placeholder_ids_are_unlinked_and_malformed_ids_unassessed(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pursuit(workspace, "acme-corp", "placeholder", sf_opportunity_id="TBD")
    _pursuit(workspace, "acme-corp", "malformed", sf_opportunity_id="006/../services?x=1")
    _pursuit(workspace, "acme-corp", "clean", sf_opportunity_id=_ID_A)
    _use_responses(monkeypatch, {_ID_A: _record(_ID_A)})

    result = _run("--json")

    assert result.exit_code == 1
    report = _report(result)
    assert report["unassessed"] == [
        {
            "pursuit": "accounts/acme-corp/pursuits/malformed.md",
            "reason": "sf_opportunity_id is not a 15- or 18-character Salesforce id",
        }
    ]
    assert [opp_id for opp_id, _ in FakeClient.instances[0].fetched] == [_ID_A]


@pytest.mark.unit
def test_hyphenated_id_key_does_not_link_the_pursuit(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = workspace / "accounts" / "acme-corp" / "pursuits"
    directory.mkdir(parents=True)
    (directory / "legacy.md").write_text(f"---\nstage: propose\nsf-opportunity-id: {_ID_A}\n---\n", encoding="utf-8")
    _use_responses(monkeypatch, {_ID_A: _record(_ID_A)})

    result = _run("--json")

    assert result.exit_code == 0, result.output
    assert _report(result)["counts"]["total"] == 0
    assert FakeClient.instances == []


@pytest.mark.unit
@pytest.mark.parametrize(
    "stage",
    [pytest.param(None, id="missing"), pytest.param("", id="blank"), pytest.param("negotiation", id="unknown")],
)
def test_linked_pursuit_without_valid_stage_is_unassessed(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, stage: str | None
) -> None:
    _pursuit(workspace, "acme-corp", "nostage", stage=stage, sf_opportunity_id=_ID_A)
    _use_responses(monkeypatch, {_ID_A: _record(_ID_A)})

    result = _run("--json")

    assert result.exit_code == 1
    report = _report(result)
    assert report["unassessed"] == [
        {
            "pursuit": "accounts/acme-corp/pursuits/nostage.md",
            "reason": "stage is missing or not a recognized pursuit stage",
        }
    ]
    assert report["counts"]["total"] == 0
    assert FakeClient.instances == []


@pytest.mark.unit
def test_legacy_won_lost_pursuit_is_out_of_scope(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _pursuit(workspace, "acme-corp", "old", stage="won-lost", sf_opportunity_id=_ID_A)

    result = _run("--json")

    assert result.exit_code == 0, result.output
    assert _report(result)["counts"]["total"] == 0
    assert FakeClient.instances == []


@pytest.mark.unit
def test_unconfigured_org_url_is_a_data_error_naming_the_key(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _pursuit(workspace, "acme-corp", "clean", sf_opportunity_id=_ID_A)
    _use_responses(monkeypatch, {_ID_A: _record(_ID_A)})
    monkeypatch.setattr(drift, "get_sf_rest_base_url", lambda: "")

    result = _run("--json")

    assert result.exit_code == 3
    assert "sf_org_url" in result.stderr
    assert "No Salesforce session" not in result.stderr
    assert result.stdout == ""
    assert FakeClient.instances == []


@pytest.mark.unit
def test_unknown_account_is_a_data_error(workspace: Path) -> None:
    _pursuit(workspace, "acme-corp", "clean", sf_opportunity_id=_ID_A)

    result = _run("--json", "--account", "no-such-corp")

    assert result.exit_code == 3
    assert "no-such-corp" in result.stderr
    assert result.stdout == ""


@pytest.mark.unit
@pytest.mark.parametrize("account", [".", "..", "acme-corp/pursuits", "../outside", "acme-corp/", "a\\b"])
def test_account_with_path_components_is_a_data_error(workspace: Path, account: str) -> None:
    """``--account`` is a directory name; path components must not widen or escape the scan."""
    _pursuit(workspace, "acme-corp", "clean", sf_opportunity_id=_ID_A)
    (workspace / "outside").mkdir()

    result = _run("--json", "--account", account)

    assert result.exit_code == 3
    assert "Account directory not found" in result.stderr
    assert result.stdout == ""
    assert FakeClient.instances == []


@pytest.mark.unit
@pytest.mark.parametrize("stage", ["closed", "won", "lost", "Closed", "won-lost"])
def test_informal_terminal_stage_pursuit_is_out_of_scope(workspace: Path, stage: str) -> None:
    """Informal terminal stages are closed, so they are neither fetched nor reported."""
    _pursuit(workspace, "acme-corp", "done", stage=stage, sf_opportunity_id=_ID_A)

    result = _run("--json")

    assert result.exit_code == 0, result.output
    assert _report(result)["counts"]["total"] == 0
    assert FakeClient.instances == []


@pytest.mark.unit
@pytest.mark.parametrize("value", [False, 0, [], {}, 12345, True])
def test_non_string_opportunity_id_is_unassessed_not_unlinked(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, value: object
) -> None:
    """A malformed non-string id must surface as incomplete, not hide as an unlinked pursuit."""
    _pursuit(workspace, "acme-corp", "odd", sf_opportunity_id=value)
    _use_responses(monkeypatch, {})

    result = _run("--json")

    assert result.exit_code == 1
    report = _report(result)
    assert [item["pursuit"] for item in report["unassessed"]] == ["accounts/acme-corp/pursuits/odd.md"]
    assert FakeClient.instances == []


@pytest.mark.unit
@pytest.mark.parametrize("value", [None, "", "   ", " tbd "])
def test_absent_or_blank_opportunity_id_is_unlinked(workspace: Path, value: str | None) -> None:
    """Only an absent, blank or placeholder id means the pursuit is not linked yet."""
    _pursuit(workspace, "acme-corp", "unlinked", sf_opportunity_id=value)

    result = _run("--json")

    assert result.exit_code == 0, result.output
    report = _report(result)
    assert report["unassessed"] == []
    assert report["counts"]["total"] == 0


@pytest.mark.unit
def test_glob_character_account_scans_only_that_directory(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An --account such as '*' names a literal directory and must not widen the scan to other accounts."""
    _pursuit(workspace, "*", "star", sf_opportunity_id=_ID_A)
    _pursuit(workspace, "acme-corp", "other", sf_opportunity_id=_ID_B)
    _use_responses(monkeypatch, {_ID_A: _record(_ID_A), _ID_B: _record(_ID_B)})

    result = _run("--json", "--account", "*")

    assert result.exit_code == 0, result.output
    assert [row["pursuit"] for row in _report(result)["opportunities"]] == ["accounts/*/pursuits/star.md"]
    assert [opp_id for opp_id, _ in FakeClient.instances[0].fetched] == [_ID_A]


@pytest.mark.unit
def test_scaffolding_account_is_not_scanned(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A dot-prefixed scaffolding account such as .template never makes the portfolio report incomplete or fetches."""
    _pursuit(workspace, ".template", "sample", sf_opportunity_id=_ID_B)
    _pursuit(workspace, "acme-corp", "clean", sf_opportunity_id=_ID_A)
    _use_responses(monkeypatch, {_ID_A: _record(_ID_A)})

    result = _run("--json")

    assert result.exit_code == 0, result.output
    assert [opp_id for opp_id, _ in FakeClient.instances[0].fetched] == [_ID_A]


@pytest.mark.unit
def test_expired_session_propagates_instead_of_partial_report(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An auth failure reaches the top-level handler so orchestrators stop instead of retrying."""
    _pursuit(workspace, "acme-corp", "clean", sf_opportunity_id="006A00000000000000")
    _use_responses(monkeypatch, {"006A00000000000000": SFAuthError("session expired")})

    result = _run("--json")

    assert isinstance(result.exception, SFAuthError)
    assert result.stdout == ""
    assert handle_cli_exception(result.exception) == EXIT_AUTH


@pytest.mark.unit
def test_unreadable_pursuit_is_reported_and_valid_rows_kept(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One malformed file makes the report incomplete without discarding the valid rows."""
    _pursuit(workspace, "acme-corp", "clean", sf_opportunity_id="006A00000000000000")
    broken = workspace / "accounts" / "acme-corp" / "pursuits" / "broken.md"
    broken.write_text("---\nstage: [unclosed\n---\n", encoding="utf-8")
    _use_responses(monkeypatch, {"006A00000000000000": _record("006A00000000000000")})

    result = _run("--json")

    assert result.exit_code == 1
    report = _report(result)
    assert report["unassessed"] == [{"pursuit": "accounts/acme-corp/pursuits/broken.md", "reason": "malformed YAML"}]
    assert report["counts"]["total"] == 1


@pytest.mark.unit
def test_missing_session_exits_auth_before_fetching(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing credentials are reported before any Salesforce request is attempted."""
    _pursuit(workspace, "acme-corp", "clean", sf_opportunity_id="006A00000000000000")
    _use_responses(monkeypatch, {"006A00000000000000": _record("006A00000000000000")})
    monkeypatch.setattr(drift, "get_sf_session_id", lambda: None)

    result = _run("--json")

    assert result.exit_code == 2
    assert "fieldkit auth sf" in result.stderr
    assert FakeClient.instances == []


@pytest.mark.unit
def test_no_linked_pursuits_needs_no_session(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With nothing to fetch, the command never consults the Salesforce session."""
    _pursuit(workspace, "acme-corp", "unlinked")
    monkeypatch.setattr(drift, "get_sf_session_id", lambda: None)

    result = _run("--json")

    assert result.exit_code == 0
    assert _report(result)["counts"]["total"] == 0


@pytest.mark.unit
def test_missing_accounts_directory_is_a_data_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An uninitialized workspace is a data error that points the user to ``fieldkit init``."""
    monkeypatch.setattr(drift, "get_fieldkit_home", lambda: tmp_path)

    result = _run()

    assert result.exit_code == 3
    assert "fieldkit init" in result.stderr


@pytest.mark.unit
def test_table_lists_unassessed_files_then_rows(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Human output names unassessed files before the comparison rows and summary."""
    _pursuit(workspace, "acme-corp", "drifted", sf_opportunity_id="006B00000000000000", sf_consulting_acv="$90,000")
    (workspace / "accounts" / "acme-corp" / "pursuits" / "broken.md").write_text("no frontmatter\n", encoding="utf-8")
    _use_responses(monkeypatch, {"006B00000000000000": _record("006B00000000000000")})

    result = _run()

    lines = result.stdout.splitlines()
    assert lines[0] == "NOT ASSESSED  accounts/acme-corp/pursuits/broken.md: missing frontmatter"
    assert lines[1].startswith("YELLOW  accounts/acme-corp/pursuits/drifted.md  acv-drift:")
    assert "0 RED, 1 YELLOW, 0 GREEN of 1 linked pursuits; 1 not assessed" in result.stdout
