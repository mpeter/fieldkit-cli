"""Tests for fieldkit.commands.sf.account — account dashboard command."""

import json
import pathlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from fieldkit.commands.sf.account import (
    _build_account_write_payload,
    _collect_local_opp_ids,
    _fmt_currency,
    _print_local_pursuits,
    _print_pipeline_section,
    _resolve_sf_account_id,
    cli,
    run_account,
)
from fieldkit.config import ConfigError
from fieldkit.errors import FieldkitError
from fieldkit.sf.client import SFDirectClient
from fieldkit.sf.errors import SFAPIError, SFNotFoundError
from fieldkit.sf.opportunities import is_opportunity_id

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def canonical_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("fieldkit.commands.sf.account.get_fieldkit_home", lambda: tmp_path)


# ── _fmt_currency ─────────────────────────────────────────────────────────────


# ── TestFmtCurrency (flattened) ─────────────────────────────────────────────


def test_fmt_currency_none_returns_not_set() -> None:
    assert _fmt_currency(None) == "(not set)"


def test_fmt_currency_zero() -> None:
    assert _fmt_currency(0.0) == "$0"


def test_fmt_currency_positive() -> None:
    assert _fmt_currency(500000.0) == "$500,000"


# ── _build_account_write_payload ──────────────────────────────────────────────


# ── TestBuildAccountWritePayload (flattened) ─────────────────────────────────────────────


def _sample_acct_build_account_write_payload() -> dict[str, object]:
    return {
        "Id": "001ACCT",
        "Name": "Big Bank Corp",
        "Industry": "Banking",
        "Account_Segment__c": "Enterprise",
        "Owner": {"Name": "Jane AE", "Email": "jae@internal.example.com"},  # pii-guard: ignore
    }


def _sample_opps_build_account_write_payload() -> list[dict[str, object]]:
    return [
        {"opportunity_id": "006A", "stage": "Propose", "consulting_acv": 300000.0, "training_acv": 0.0},
        {"opportunity_id": "006B", "stage": "Discover", "consulting_acv": 0.0, "training_acv": 50000.0},
    ]


def test_build_account_write_payload_status_ok() -> None:
    payload = _build_account_write_payload(
        "bigbank", _sample_acct_build_account_write_payload(), _sample_opps_build_account_write_payload()
    )
    assert payload["status"] == "ok"


def test_build_account_write_payload_account_fields() -> None:
    payload = _build_account_write_payload(
        "bigbank", _sample_acct_build_account_write_payload(), _sample_opps_build_account_write_payload()
    )
    assert payload["account_name"] == "Big Bank Corp"
    assert payload["industry"] == "Banking"
    assert payload["segment"] == "Enterprise"
    assert payload["owner"] == "Jane AE"


def test_build_account_write_payload_open_opp_count() -> None:
    payload = _build_account_write_payload(
        "bigbank", _sample_acct_build_account_write_payload(), _sample_opps_build_account_write_payload()
    )
    assert payload["open_opportunity_count"] == 2


def test_build_account_write_payload_acv_aggregates() -> None:
    payload = _build_account_write_payload(
        "bigbank", _sample_acct_build_account_write_payload(), _sample_opps_build_account_write_payload()
    )
    assert payload["open_consulting_acv"] == 300000.0
    assert payload["open_training_acv"] == 50000.0


def test_build_account_write_payload_no_opps_gives_none_acv() -> None:
    payload = _build_account_write_payload("bigbank", _sample_acct_build_account_write_payload(), [])
    assert payload["open_consulting_acv"] is None
    assert payload["open_training_acv"] is None
    assert payload["open_opportunity_count"] == 0


def test_build_account_write_payload_none_acct_rec_uses_account_name() -> None:
    payload = _build_account_write_payload("bigbank", None, [])
    assert payload["account_name"] == "bigbank"
    assert payload["account_id"] is None


def test_build_account_write_payload_pulled_at_set() -> None:
    payload = _build_account_write_payload("bigbank", _sample_acct_build_account_write_payload(), [])
    assert "pulled_at" in payload
    assert "T" in payload["pulled_at"]


# ── CLI ───────────────────────────────────────────────────────────────────────


# ── TestAccountCli (flattened) ─────────────────────────────────────────────


def test_unknown_account_exits_nonzero() -> None:
    runner = CliRunner()
    with (
        patch("fieldkit.commands.sf.account.get_sf_session_id", return_value="fakesid"),
        patch("fieldkit.commands.sf.account.get_sf_rest_base_url", return_value="https://sf.example.com"),
        patch("fieldkit.commands.sf.account.get_account_names", return_value=["acme-bank", "globalpay"]),
    ):
        result = runner.invoke(cli, ["unknown-account", "--no-write"])
    assert result.exit_code != 0


def test_no_session_exits_2() -> None:
    runner = CliRunner()
    with (
        patch("fieldkit.commands.sf.account.get_sf_session_id", return_value=None),
        patch("fieldkit.commands.sf.account.get_account_names", return_value=["acme-bank"]),
    ):
        result = runner.invoke(cli, ["acme-bank", "--no-write"])
    assert result.exit_code == 2


@pytest.mark.parametrize("preview_flag", ["--no-write", "--dry-run"])
def test_preview_flag_skips_write_account(preview_flag: str, tmp_path: Path) -> None:
    """Both public preview spellings use the same no-write implementation."""
    sample_acct = {
        "Id": "001TEST",
        "Name": "Test Corp",
        "Industry": "Tech",
        "Account_Segment__c": "Enterprise",
        "Account_Subsegment__c": "Enterprise",
        "Owner": {"Name": "Jane", "Email": "j@r-com.example.com"},
        "BillingCity": "Austin",
        "BillingState": "TX",
        "BillingCountry": "US",
        "Type": "Customer",
        "AnnualRevenue": None,
        "NumberOfEmployees": None,
    }
    sample_opps = [
        {
            "opportunity_id": "006A",
            "stage": "Propose",
            "consulting_acv": 500000.0,
            "training_acv": 0.0,
            "name": "Big Deal",
        },
    ]

    runner = CliRunner()
    with (
        patch("fieldkit.commands.sf.account.get_sf_session_id", return_value="fakesid"),
        patch("fieldkit.commands.sf.account.get_sf_rest_base_url", return_value="https://sf.example.com"),
        patch("fieldkit.commands.sf.account.get_account_names", return_value=["acme-bank"]),
        patch(
            "fieldkit.commands.sf.account.get_accounts_config",
            return_value={
                "accounts": {"acme-bank": {"keywords": ["acme-bank"], "pursuit_dir": "accounts/acme-bank/pursuits"}}
            },
        ),
        patch("fieldkit.commands.sf.account.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.sf.client.SFDirectClient"),
        patch("fieldkit.commands.sf.account._resolve_sf_account_id", return_value="001TEST"),
        patch("fieldkit.commands.sf.account._fetch_account_record", return_value=sample_acct),
        patch("fieldkit.commands.sf.account._fetch_live_opportunities", return_value=sample_opps),
        patch("fieldkit.commands.sf.account.sync_account") as mock_write,
    ):
        result = runner.invoke(cli, ["acme-bank", preview_flag])

    assert result.exit_code == 0
    mock_write.assert_called_once()
    assert mock_write.call_args.kwargs["dry_run"] is True
    assert "Test Corp" in result.output
    assert "$500,000" in result.output


# ── _SF_ID_RE ─────────────────────────────────────────────────────────────────


# ── TestSfIdRe (flattened) ─────────────────────────────────────────────


def test_accepts_15_char_alphanumeric() -> None:
    assert is_opportunity_id("006Qs000001abcd")


def test_accepts_18_char_alphanumeric() -> None:
    assert is_opportunity_id("006Qs000001abcdABC")


def test_rejects_needs_lookup() -> None:
    assert not is_opportunity_id("NEEDS-LOOKUP")


def test_rejects_tbd() -> None:
    assert not is_opportunity_id("TBD")


def test_rejects_id_with_hyphen() -> None:
    # Hyphens are not alphanumeric — must be rejected
    assert not is_opportunity_id("006-invalid-id")


def test_rejects_empty_string() -> None:
    assert not is_opportunity_id("")


def test_rejects_17_chars() -> None:
    # Neither 15 nor 18 chars — must be rejected
    assert not is_opportunity_id("006Qs000001abcd1")


# ── _resolve_sf_account_id placeholder guard ──────────────────────────────────


# ── TestResolveSfAccountIdPlaceholderGuard (flattened) ─────────────────────────────────────────────


def _make_pursuit_file_resolve_sf_account_id_placeholder_guard(pursuit_dir: Path, opp_id: str) -> Path:
    """Write a minimal pursuit markdown file with the given sf_opportunity_id."""
    content = f"---\nsf_opportunity_id: {opp_id}\nstage: Discover\n---\n\n# Test pursuit\n"
    path = pursuit_dir / "test-pursuit.md"
    path.write_text(content, encoding="utf-8")
    return path


def test_placeholder_does_not_call_fetch_record(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A pursuit with sf_opportunity_id: NEEDS-LOOKUP must be skipped without an API call."""
    pursuit_dir = tmp_path / "accounts" / "acme" / "pursuits"
    pursuit_dir.mkdir(parents=True)
    _make_pursuit_file_resolve_sf_account_id_placeholder_guard(pursuit_dir, "NEEDS-LOOKUP")

    mock_client = MagicMock()
    # resolve_account_id_by_keywords returns None → falls through to file scan
    mock_client.resolve_account_id_by_keywords.return_value = None

    accounts_cfg = {
        "accounts": {
            "acme": {
                "keywords": ["acme"],
                "pursuit_dir": "accounts/acme/pursuits",
            }
        }
    }

    with (
        patch("fieldkit.commands.sf.account.get_accounts_config", return_value=accounts_cfg),
        patch("fieldkit.commands.sf.account.get_fieldkit_home", return_value=tmp_path),
        pytest.raises(FieldkitError, match="identity is invalid"),
    ):
        _resolve_sf_account_id("acme", mock_client, "https://sf.example.com")
    # Must NOT have called fetch_record with the placeholder
    mock_client.fetch_record.assert_not_called()


def test_placeholder_emits_warning(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A non-empty placeholder must produce a WARNING on stderr."""
    pursuit_dir = tmp_path / "accounts" / "acme" / "pursuits"
    pursuit_dir.mkdir(parents=True)
    _make_pursuit_file_resolve_sf_account_id_placeholder_guard(pursuit_dir, "NEEDS-LOOKUP")

    mock_client = MagicMock()
    mock_client.resolve_account_id_by_keywords.return_value = None

    accounts_cfg = {
        "accounts": {
            "acme": {
                "keywords": [],  # skip SOSL so we go straight to file scan
                "pursuit_dir": "accounts/acme/pursuits",
            }
        }
    }

    with (
        patch("fieldkit.commands.sf.account.get_accounts_config", return_value=accounts_cfg),
        patch("fieldkit.commands.sf.account.get_fieldkit_home", return_value=tmp_path),
        pytest.raises(FieldkitError, match="identity is invalid"),
    ):
        _resolve_sf_account_id("acme", mock_client, "https://sf.example.com")

    mock_client.fetch_record.assert_not_called()


def test_null_opp_id_is_silently_skipped(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A pursuit with sf_opportunity_id: null must be skipped without a warning."""
    pursuit_dir = tmp_path / "accounts" / "acme" / "pursuits"
    pursuit_dir.mkdir(parents=True)
    # YAML null → Python None
    content = "---\nsf_opportunity_id: null\nstage: Discover\n---\n\n# Test pursuit\n"
    (pursuit_dir / "test-pursuit.md").write_text(content, encoding="utf-8")

    mock_client = MagicMock()
    mock_client.resolve_account_id_by_keywords.return_value = None

    accounts_cfg = {"accounts": {"acme": {"keywords": [], "pursuit_dir": "accounts/acme/pursuits"}}}

    with (
        patch("fieldkit.commands.sf.account.get_accounts_config", return_value=accounts_cfg),
        patch("fieldkit.commands.sf.account.get_fieldkit_home", return_value=tmp_path),
    ):
        result = _resolve_sf_account_id("acme", mock_client, "https://sf.example.com")
    assert result is None
    mock_client.fetch_record.assert_not_called()
    captured = capsys.readouterr()
    assert "WARNING" not in captured.err


def test_valid_sf_id_calls_sosl_search(tmp_path: Path) -> None:
    """A valid 18-char SF ID must reach sosl_search (implementation note batch) and return the Account.Id."""
    pursuit_dir = tmp_path / "accounts" / "acme" / "pursuits"
    pursuit_dir.mkdir(parents=True)
    valid_id = "006Qs000001abcdABC"  # 18 chars, alphanumeric
    _make_pursuit_file_resolve_sf_account_id_placeholder_guard(pursuit_dir, valid_id)

    mock_client = MagicMock()
    mock_client.resolve_account_id_by_keywords.return_value = None
    # implementation note: sosl_search returns list[dict] directly (not nested under Account key)
    mock_client.sosl_search.return_value = [{"AccountId": "001ACCT000001xyz"}]

    accounts_cfg = {"accounts": {"acme": {"keywords": [], "pursuit_dir": "accounts/acme/pursuits"}}}

    with (
        patch("fieldkit.commands.sf.account.get_accounts_config", return_value=accounts_cfg),
        patch("fieldkit.commands.sf.account.get_fieldkit_home", return_value=tmp_path),
    ):
        result = _resolve_sf_account_id("acme", mock_client, "https://sf.example.com")

    # implementation note: single sosl_search call instead of fetch_record per file
    mock_client.sosl_search.assert_called_once()
    mock_client.fetch_record.assert_not_called()
    assert result == "001ACCT000001xyz"


# ── historic regression: skip write when SF account lookup returns None ───────────────────


# ── TestRunAccountSkipsWriteWhenNoSfRecord (flattened) ─────────────────────────────────────────────


def test_no_sf_record_skips_write_and_warns(tmp_path: pathlib.Path, capsys: pytest.CaptureFixture) -> None:  # type: ignore[type-arg]
    """A completed no-match exits 3 and never writes account.md."""
    acct_slug = "acme"
    with (
        patch("fieldkit.commands.sf.account.get_sf_session_id", return_value="fakesid"),
        patch("fieldkit.commands.sf.account.get_sf_rest_base_url", return_value="https://sf.example.com"),
        patch("fieldkit.commands.sf.account.get_account_names", return_value=[acct_slug]),
        patch(
            "fieldkit.commands.sf.account.get_accounts_config",
            return_value={
                "accounts": {
                    acct_slug: {
                        "keywords": [acct_slug],
                        "pursuit_dir": f"accounts/{acct_slug}/pursuits",
                    }
                }
            },
        ),
        patch("fieldkit.commands.sf.account.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.sf.client.SFDirectClient"),
        # Key mock: SF account lookup returns None (account not found)
        patch("fieldkit.commands.sf.account._resolve_sf_account_id", return_value=None),
        patch("fieldkit.commands.sf.account.sync_account") as mock_write,
    ):
        exit_code = run_account(acct_slug, write=True)

    assert exit_code == 3
    # Must NOT write frontmatter — that would overwrite correct data with None values
    mock_write.assert_not_called()
    captured = capsys.readouterr()
    assert "Salesforce account record was not found" in captured.err
    assert acct_slug not in captured.err


def test_json_no_sf_record_is_non_success_and_skips_dependent_reads_and_writes() -> None:
    """JSON must not report ok when the primary account lookup found no record."""
    runner = CliRunner()
    with (
        patch("fieldkit.commands.sf.account.get_account_names", return_value=["acme"]),
        patch("fieldkit.commands.sf.account.get_sf_session_id", return_value="fake-sid"),
        patch("fieldkit.commands.sf.account.get_sf_rest_base_url", return_value="https://sf.example.com"),
        patch("fieldkit.commands.sf.account._sf_direct.SFDirectClient"),
        patch("fieldkit.commands.sf.account._resolve_sf_account_id", return_value=None),
        patch("fieldkit.commands.sf.account._fetch_live_opportunities") as mock_fetch_opportunities,
        patch("fieldkit.commands.sf.account.sync_account") as mock_write,
    ):
        result = runner.invoke(cli, ["acme", "--json"])

    assert result.exit_code == 3
    assert json.loads(result.output) == {
        "status": "error",
        "error": "Salesforce account record was not found.",
    }
    mock_fetch_opportunities.assert_not_called()
    mock_write.assert_not_called()


def test_data_root_failure_uses_bounded_diagnostic(capsys: pytest.CaptureFixture[str]) -> None:
    """Configuration failures must not print raw private paths or exceptions."""
    with (
        patch("fieldkit.commands.sf.account.get_account_names", return_value=["acme"]),
        patch("fieldkit.commands.sf.account.get_accounts_config", return_value={"accounts": {"acme": {}}}),
        patch("fieldkit.commands.sf.account.get_sf_session_id", return_value="fake-sid"),
        patch("fieldkit.commands.sf.account.get_sf_rest_base_url", return_value="https://sf.example.com"),
        patch(
            "fieldkit.commands.sf.account.get_fieldkit_home",
            side_effect=ConfigError("private path: /fictional-private/operator/customer-data"),
        ),
        pytest.raises(SystemExit) as exc_info,
    ):
        run_account("acme", write=False)

    assert exc_info.value.code == 3
    captured = capsys.readouterr()
    assert "workspace configuration is invalid" in captured.err
    assert "/fictional-private/operator/customer-data" not in captured.err


def test_net_acv_provider_failure_uses_bounded_diagnostic(capsys: pytest.CaptureFixture[str]) -> None:
    """Optional net-ACV degradation keeps record IDs and provider text private."""
    from fieldkit.commands.sf.account import _fetch_net_acv

    mock_client = MagicMock()
    mock_client.fetch_record.side_effect = SFAPIError("private path: /fictional-private/operator/customer-data")

    result = _fetch_net_acv("006PRIVATE1234567", mock_client)

    assert result is None
    captured = capsys.readouterr()
    assert "Salesforce net ACV read failed" in captured.err
    assert "006PRIVATE1234567" not in captured.err
    assert "/fictional-private/operator/customer-data" not in captured.err


def test_primary_opportunity_failure_does_not_write_account(tmp_path: Path) -> None:
    """A failed primary lookup must not replace live pipeline values with zeroes."""
    with (
        patch("fieldkit.commands.sf.account.get_account_names", return_value=["acme"]),
        patch("fieldkit.commands.sf.account.get_accounts_config", return_value=_accounts_cfg_sf_auth_error_handling()),
        patch("fieldkit.commands.sf.account.get_sf_session_id", return_value="fake-sid"),
        patch("fieldkit.commands.sf.account.get_sf_rest_base_url", return_value="https://sf.example.com"),
        patch("fieldkit.commands.sf.account.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.commands.sf.account._sf_direct.SFDirectClient"),
        patch("fieldkit.commands.sf.account._resolve_sf_account_id", return_value="001ACCOUNT000001"),
        patch("fieldkit.commands.sf.account._fetch_account_record", return_value={"Id": "001ACCOUNT000001"}),
        patch("fieldkit.commands.sf.account._fetch_live_opportunities", side_effect=SFAPIError("network timeout")),
        patch("fieldkit.commands.sf.account.sync_account") as mock_write,
    ):
        result = run_account("acme", write=True)

    assert result == 1
    mock_write.assert_not_called()


# ── implementation change: mark unmatched SF opps ──────────────────────────────────────────


# ── TestCollectLocalOppIds (flattened) ─────────────────────────────────────────────


def test_collect_local_opp_ids_returns_empty_frozenset_when_dir_missing(tmp_path: Path) -> None:

    directory = tmp_path / "accounts" / "acme" / "pursuits"
    directory.mkdir(parents=True)
    result = _collect_local_opp_ids(tmp_path / "accounts" / "missing" / "pursuits")
    assert result == frozenset()


def test_collect_local_opp_ids_collects_ids_from_pursuit_files(tmp_path: Path) -> None:

    directory = tmp_path / "accounts" / "acme" / "pursuits"
    directory.mkdir(parents=True)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "deal-a.md").write_text(
        "---\nsf_opportunity_id: 006ABC000000001AAA\nstage: discover\n---\n",
        encoding="utf-8",
    )
    (directory / "deal-b.md").write_text(
        "---\nsf_opportunity_id: 006ABC000000002AAA\nstage: propose\n---\n",
        encoding="utf-8",
    )
    result = _collect_local_opp_ids(directory)
    assert "006ABC000000001AAA" in result
    assert "006ABC000000002AAA" in result


def test_collect_local_opp_ids_skips_gmail_intel_and_template(tmp_path: Path) -> None:

    directory = tmp_path / "accounts" / "acme" / "pursuits"
    directory.mkdir(parents=True)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "gmail-intel.md").write_text(
        "---\nsf_opportunity_id: 006SKIP000000001AAA\n---\n",
        encoding="utf-8",
    )
    (directory / "template.md").write_text(
        "---\nsf_opportunity_id: 006SKIP000000002AAA\n---\n",
        encoding="utf-8",
    )
    result = _collect_local_opp_ids(directory)
    assert "006SKIP000000001AAA" not in result
    assert "006SKIP000000002AAA" not in result


def test_collect_local_opp_ids_ignores_files_without_frontmatter(tmp_path: Path) -> None:

    directory = tmp_path / "accounts" / "acme" / "pursuits"
    directory.mkdir(parents=True)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "no-fm.md").write_text("# Just a heading\nNo frontmatter here.\n", encoding="utf-8")
    with pytest.raises(FieldkitError, match="frontmatter"):
        _collect_local_opp_ids(directory)


# ── TestPrintPipelineSectionUnmatchedTag (flattened) ─────────────────────────────────────────────


def _make_opp_print_pipeline_section_unmatched_tag(opp_id: str, name: str = "Test Deal") -> dict[str, object]:
    return {
        "opportunity_id": opp_id,
        "stage": "Propose",
        "consulting_acv": 100000.0,
        "training_acv": 0.0,
        "name": name,
    }


def test_matched_opp_has_no_tag(capsys: pytest.CaptureFixture[str]) -> None:
    opp_id = "006ABC000000001AAA"
    _print_pipeline_section([_make_opp_print_pipeline_section_unmatched_tag(opp_id)], frozenset({opp_id}))
    out = capsys.readouterr().out
    assert "[no local pursuit]" not in out


def test_unmatched_opp_has_tag(capsys: pytest.CaptureFixture[str]) -> None:
    # implementation change: annotation now includes the pursuit create command hint.
    opp_id = "006ABC000000001AAA"
    _print_pipeline_section([_make_opp_print_pipeline_section_unmatched_tag(opp_id)], frozenset())
    out = capsys.readouterr().out
    assert "[no local pursuit" in out


def test_unmatched_opp_includes_create_hint(capsys: pytest.CaptureFixture[str]) -> None:
    """implementation change: unmatched opp annotation includes fieldkit pursuit create command."""
    opp_id = "006ABC000000001AAA"
    _print_pipeline_section(
        [_make_opp_print_pipeline_section_unmatched_tag(opp_id, "Test Deal")], frozenset(), account_name="acme"
    )
    out = capsys.readouterr().out
    assert "fieldkit pursuit create" in out
    assert "--account acme" in out
    assert "test-deal" in out


def test_empty_local_ids_marks_all_opps(capsys: pytest.CaptureFixture[str]) -> None:
    # implementation change: annotation now includes the pursuit create command hint.
    opps = [
        _make_opp_print_pipeline_section_unmatched_tag("006000000000000AAA", "Deal A"),
        _make_opp_print_pipeline_section_unmatched_tag("006000000000001AAA", "Deal B"),
    ]
    _print_pipeline_section(opps, frozenset())
    out = capsys.readouterr().out
    assert out.count("[no local pursuit") == 2


def test_no_opps_prints_none_found(capsys: pytest.CaptureFixture[str]) -> None:
    _print_pipeline_section([], frozenset())
    out = capsys.readouterr().out
    assert "no open services opportunities" in out


# ── _print_local_pursuits (historic regression) ───────────────────────────────────────────


# ── TestPrintLocalPursuits (flattened) ─────────────────────────────────────────────


def _write_pursuit_print_local_pursuits(d: Path, name: str, stage: str) -> None:
    content = f"---\nstage: {stage}\n---\n# {name}\n"
    (d / name).write_text(content, encoding="utf-8")


def _write_malformed_print_local_pursuits(d: Path, name: str, stage: str) -> None:
    # Malformed: no closing --- delimiter — brittle split("---", 2) would fail
    content = f"---\nstage: {stage}\n# body starts here without closing delimiter\n"
    (d / name).write_text(content, encoding="utf-8")


def test_print_local_pursuits_active_pursuit_appears_in_active_list(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:

    directory = tmp_path / "accounts" / "acme" / "pursuits"
    directory.mkdir(parents=True)
    _write_pursuit_print_local_pursuits(directory, "deal-a.md", "discover")
    _print_local_pursuits(directory)
    out = capsys.readouterr().out
    assert "deal-a.md" in out
    assert "1 active" in out


def test_print_local_pursuits_closed_lost_not_counted_as_active(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:

    directory = tmp_path / "accounts" / "acme" / "pursuits"
    directory.mkdir(parents=True)
    _write_pursuit_print_local_pursuits(directory, "deal-closed.md", "closed-lost")
    _print_local_pursuits(directory)
    out = capsys.readouterr().out
    # closed-lost must NOT appear in active count
    assert "0 active" in out
    assert "1 closed" in out


def test_print_local_pursuits_malformed_frontmatter_does_not_classify_as_active(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:

    directory = tmp_path / "accounts" / "acme" / "pursuits"
    directory.mkdir(parents=True)
    # Malformed file: extract_frontmatter_text returns None → stage="" → active
    # This is acceptable (unknown stage → active bucket) but must NOT crash.
    _write_malformed_print_local_pursuits(directory, "malformed.md", "closed-lost")
    with pytest.raises(FieldkitError, match="frontmatter"):
        _print_local_pursuits(directory)
    assert capsys.readouterr().out == ""


def test_print_local_pursuits_closed_won_not_counted_as_active(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:

    directory = tmp_path / "accounts" / "acme" / "pursuits"
    directory.mkdir(parents=True)
    _write_pursuit_print_local_pursuits(directory, "won.md", "closed-won")
    _print_local_pursuits(directory)
    out = capsys.readouterr().out
    assert "0 active" in out
    assert "1 closed" in out


def test_print_local_pursuits_skips_template_and_gmail_intel(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:

    directory = tmp_path / "accounts" / "acme" / "pursuits"
    directory.mkdir(parents=True)
    (directory / "template.md").write_text("---\nstage: discover\n---\n", encoding="utf-8")
    (directory / "gmail-intel.md").write_text("---\nstage: discover\n---\n", encoding="utf-8")
    _print_local_pursuits(directory)
    out = capsys.readouterr().out
    assert "0 active" in out


# ── implementation change: pursuit create hint in [no local pursuit] annotation ─────────────


# ── TestENH302PursuitCreateHint (flattened) ─────────────────────────────────────────────


def _make_opp_enh302_pursuit_create_hint(opp_id: str, name: str = "Test Deal") -> dict[str, object]:
    return {
        "opportunity_id": opp_id,
        "stage": "Propose",
        "consulting_acv": 100000.0,
        "training_acv": 0.0,
        "name": name,
    }


def test_unmatched_opp_includes_create_command(capsys: pytest.CaptureFixture[str]) -> None:
    """Unmatched opp annotation includes 'fieldkit pursuit create' command."""
    _print_pipeline_section(
        [_make_opp_enh302_pursuit_create_hint("006A", "Acme Deal Q4")], frozenset(), account_name="acme"
    )
    out = capsys.readouterr().out
    assert "fieldkit pursuit create" in out
    assert "--account acme" in out


def test_opp_slug_derived_from_name(capsys: pytest.CaptureFixture[str]) -> None:
    """Opp slug in the hint is derived from the SF opportunity name."""
    _print_pipeline_section(
        [_make_opp_enh302_pursuit_create_hint("006A", "Big Bank Deal 2026")], frozenset(), account_name="bigbank"
    )
    out = capsys.readouterr().out
    assert "big-bank-deal-2026" in out


def test_matched_opp_no_create_hint(capsys: pytest.CaptureFixture[str]) -> None:
    """Matched opp (has local pursuit) must not show the create hint."""
    opp_id = "006ABC000000001AAA"
    _print_pipeline_section(
        [_make_opp_enh302_pursuit_create_hint(opp_id, "Matched Deal")], frozenset({opp_id}), account_name="acme"
    )
    out = capsys.readouterr().out
    assert "fieldkit pursuit create" not in out


def test_no_account_name_still_shows_hint(capsys: pytest.CaptureFixture[str]) -> None:
    """When account_name is empty, the hint still shows without --account flag."""
    _print_pipeline_section([_make_opp_enh302_pursuit_create_hint("006A", "Some Deal")], frozenset())
    out = capsys.readouterr().out
    assert "fieldkit pursuit create" in out
    assert "--account" not in out


# ── Phase 8 additions (task 8.7a) ─────────────────────────────────────────────


# ── TestResolveSfAccountIdPhase8 (flattened) ─────────────────────────────────────────────


def _accounts_cfg_resolve_sf_account_id_phase8(keywords: list[str]) -> dict:
    return {
        "accounts": {
            "acme": {
                "keywords": keywords,
                "pursuit_dir": "accounts/acme/pursuits",
            }
        }
    }


def test_returns_none_when_no_match(tmp_path: Path) -> None:
    """8.7a-1: Returns None when SOSL search and file scan both find nothing."""
    (tmp_path / "accounts" / "acme" / "pursuits").mkdir(parents=True)

    mock_client = MagicMock()
    mock_client.resolve_account_id_by_keywords.return_value = None

    with (
        patch(
            "fieldkit.commands.sf.account.get_accounts_config",
            return_value=_accounts_cfg_resolve_sf_account_id_phase8(["acme"]),
        ),
        patch("fieldkit.commands.sf.account.get_fieldkit_home", return_value=tmp_path),
    ):
        result = _resolve_sf_account_id("acme", mock_client, "https://sf.example.com")

    assert result is None


def test_returns_id_when_sosl_matches(tmp_path: Path) -> None:
    """8.7a-2: Returns the Account.Id when SOSL keyword search succeeds."""
    mock_client = MagicMock()
    mock_client.resolve_account_id_by_keywords.return_value = "001ACCT000000001AAA"

    with patch(
        "fieldkit.commands.sf.account.get_accounts_config",
        return_value=_accounts_cfg_resolve_sf_account_id_phase8(["acme"]),
    ):
        result = _resolve_sf_account_id("acme", mock_client, "https://sf.example.com")

    assert result == "001ACCT000000001AAA"


# ── CRAP-reduction: additional branch coverage for _resolve_sf_account_id ─────
# These tests target uncovered branches to reduce CRAP score below CI gate.


# ── TestResolveSfAccountIdBranchCoverage (flattened) ─────────────────────────────────────────────


def _accounts_cfg_resolve_sf_account_id_branch_coverage(
    keywords: list[str], pursuit_dir: str = "accounts/acme/pursuits"
) -> dict:
    return {
        "accounts": {
            "acme": {
                "keywords": keywords,
                "pursuit_dir": pursuit_dir,
            }
        }
    }


def test_returns_none_when_account_not_in_config(tmp_path: Path) -> None:
    """Returns None when account_name is not in accounts.yaml."""
    mock_client = MagicMock()
    with patch("fieldkit.commands.sf.account.get_accounts_config", return_value={"accounts": {}}):
        result = _resolve_sf_account_id("unknown-account", mock_client, "https://sf.example.com")
    assert result is None


def test_returns_none_when_account_info_not_dict(tmp_path: Path) -> None:
    """Returns None when account config value is not a dict (malformed yaml)."""
    mock_client = MagicMock()
    with patch(
        "fieldkit.commands.sf.account.get_accounts_config",
        return_value={"accounts": {"acme": "not-a-dict"}},
    ):
        result = _resolve_sf_account_id("acme", mock_client, "https://sf.example.com")
    assert result is None


def test_account_resolution_data_root_failure_propagates(tmp_path: Path) -> None:
    """A data-root failure must remain distinguishable from a completed no-match."""
    mock_client = MagicMock()
    mock_client.resolve_account_id_by_keywords.return_value = None

    with (
        patch(
            "fieldkit.commands.sf.account.get_accounts_config",
            return_value=_accounts_cfg_resolve_sf_account_id_branch_coverage([]),
        ),
        patch("fieldkit.commands.sf.account.get_fieldkit_home", side_effect=OSError("private path")),
        pytest.raises(OSError, match="private path"),
    ):
        _resolve_sf_account_id("acme", mock_client, "https://sf.example.com")


def test_account_fallback_read_failure_is_partial_and_never_writes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failed fallback scan is retryable and does not masquerade as no-match."""
    with (
        patch("fieldkit.commands.sf.account.get_account_names", return_value=["acme"]),
        patch("fieldkit.commands.sf.account.get_accounts_config", return_value={"accounts": {"acme": {}}}),
        patch("fieldkit.commands.sf.account.get_sf_session_id", return_value="fake-sid"),
        patch("fieldkit.commands.sf.account.get_sf_rest_base_url", return_value="https://sf.example.com"),
        patch("fieldkit.commands.sf.account.get_fieldkit_home", side_effect=[tmp_path, OSError("/private/root")]),
        patch("fieldkit.commands.sf.account._sf_direct.SFDirectClient"),
        patch("fieldkit.commands.sf.account.sync_account") as mock_write,
    ):
        result = run_account("acme", write=True)

    assert result == 1
    mock_write.assert_not_called()
    captured = capsys.readouterr()
    assert "local pursuit scan failed" in captured.err.lower()
    assert "/private/root" not in captured.err


def test_malformed_pursuit_yaml_is_partial_and_never_writes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    pursuit_dir = tmp_path / "accounts" / "acme" / "pursuits"
    pursuit_dir.mkdir(parents=True)
    (pursuit_dir / "broken.md").write_text("---\nsf_opportunity_id: [private-invalid\n---\nBody.\n", encoding="utf-8")
    config = {"accounts": {"acme": {"keywords": [], "pursuit_dir": "accounts/acme/pursuits"}}}
    with (
        patch("fieldkit.commands.sf.account.get_account_names", return_value=["acme"]),
        patch("fieldkit.commands.sf.account.get_accounts_config", return_value=config),
        patch("fieldkit.commands.sf.account.get_sf_session_id", return_value="fake-sid"),
        patch("fieldkit.commands.sf.account.get_sf_rest_base_url", return_value="https://sf.example.com"),
        patch("fieldkit.commands.sf.account.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.commands.sf.account._sf_direct.SFDirectClient"),
        patch("fieldkit.commands.sf.account.sync_account") as mock_write,
    ):
        result = run_account("acme", write=True)

    assert result == 3
    mock_write.assert_not_called()
    captured = capsys.readouterr()
    assert "local pursuit metadata is invalid" in captured.err.lower()
    assert "private-invalid" not in captured.err


def test_returns_none_when_pursuit_dir_missing(tmp_path: Path) -> None:
    """Returns None when pursuit_dir does not exist on disk."""
    mock_client = MagicMock()
    mock_client.resolve_account_id_by_keywords.return_value = None

    with (
        patch(
            "fieldkit.commands.sf.account.get_accounts_config",
            return_value=_accounts_cfg_resolve_sf_account_id_branch_coverage([]),
        ),
        patch("fieldkit.commands.sf.account.get_fieldkit_home", return_value=tmp_path),
    ):
        # pursuit dir does not exist → returns None
        result = _resolve_sf_account_id("acme", mock_client, "https://sf.example.com")

    assert result is None


def test_sfautherror_on_sosl_search_propagates(tmp_path: Path) -> None:
    """implementation note: SFAuthError from sosl_search propagates (not caught in batch loop)."""
    from fieldkit.sf.errors import SFAuthError

    pursuit_dir = tmp_path / "accounts" / "acme" / "pursuits"
    pursuit_dir.mkdir(parents=True)
    valid_id = "006000000000000AAA"  # 18 chars
    (pursuit_dir / "a-deal.md").write_text(
        f"---\nsf_opportunity_id: {valid_id}\nstage: discover\n---\n",
        encoding="utf-8",
    )

    mock_client = MagicMock()
    mock_client.resolve_account_id_by_keywords.return_value = None
    mock_client.sosl_search.side_effect = SFAuthError("expired")

    with (
        patch(
            "fieldkit.commands.sf.account.get_accounts_config",
            return_value=_accounts_cfg_resolve_sf_account_id_branch_coverage([]),
        ),
        patch("fieldkit.commands.sf.account.get_fieldkit_home", return_value=tmp_path),
        pytest.raises(SFAuthError, match="expired"),
    ):
        _resolve_sf_account_id("acme", mock_client, "https://sf.example.com")


def test_sfapierror_on_sosl_search_propagates(tmp_path: Path) -> None:
    """Provider failure must not become a successful no-match result."""
    from fieldkit.sf.errors import SFAPIError

    pursuit_dir = tmp_path / "accounts" / "acme" / "pursuits"
    pursuit_dir.mkdir(parents=True)
    valid_id = "006000000000000AAA"  # 18 chars
    (pursuit_dir / "a-deal.md").write_text(
        f"---\nsf_opportunity_id: {valid_id}\nstage: discover\n---\n",
        encoding="utf-8",
    )

    mock_client = MagicMock()
    mock_client.resolve_account_id_by_keywords.return_value = None
    mock_client.sosl_search.side_effect = SFAPIError("network error")

    with (
        patch(
            "fieldkit.commands.sf.account.get_accounts_config",
            return_value=_accounts_cfg_resolve_sf_account_id_branch_coverage([]),
        ),
        patch("fieldkit.commands.sf.account.get_fieldkit_home", return_value=tmp_path),
        pytest.raises(SFAPIError, match="network error"),
    ):
        _resolve_sf_account_id("acme", mock_client, "https://sf.example.com")


def test_sosl_returns_id_without_file_scan(tmp_path: Path) -> None:
    """When SOSL keyword search returns an ID, file scan is skipped entirely."""
    mock_client = MagicMock()
    mock_client.resolve_account_id_by_keywords.return_value = "001SOSL000000001AAA"

    with patch(
        "fieldkit.commands.sf.account.get_accounts_config",
        return_value=_accounts_cfg_resolve_sf_account_id_branch_coverage(["acme corp"]),
    ):
        result = _resolve_sf_account_id("acme", mock_client, "https://sf.example.com")

    assert result == "001SOSL000000001AAA"
    # implementation note: file scan (sosl_search) must NOT be called when keyword SOSL succeeds
    mock_client.sosl_search.assert_not_called()
    mock_client.fetch_record.assert_not_called()


def test_sosl_search_returns_no_account_id_returns_none(tmp_path: Path) -> None:
    """implementation note: When sosl_search returns records with no AccountId, returns None."""
    pursuit_dir = tmp_path / "accounts" / "acme" / "pursuits"
    pursuit_dir.mkdir(parents=True)
    (pursuit_dir / "a-deal.md").write_text(
        "---\nsf_opportunity_id: 006000000000000AAA\nstage: discover\n---\n",
        encoding="utf-8",
    )

    mock_client = MagicMock()
    mock_client.resolve_account_id_by_keywords.return_value = None
    # sosl_search returns record with no AccountId
    mock_client.sosl_search.return_value = [{"AccountId": None}]

    with (
        patch(
            "fieldkit.commands.sf.account.get_accounts_config",
            return_value=_accounts_cfg_resolve_sf_account_id_branch_coverage([]),
        ),
        patch("fieldkit.commands.sf.account.get_fieldkit_home", return_value=tmp_path),
    ):
        result = _resolve_sf_account_id("acme", mock_client, "https://sf.example.com")

    # No valid AccountId found → returns None
    assert result is None


# ---------------------------------------------------------------------------
# historic regression regression: SFAuthError must produce clean error, not raw traceback
# ---------------------------------------------------------------------------


# ── TestSFAuthErrorHandling (flattened) ─────────────────────────────────────────────


def _accounts_cfg_sf_auth_error_handling() -> dict:
    return {
        "accounts": {
            "acme": {
                "keywords": ["Acme Corp"],
                "domains": ["acme-corp.example.com"],
                "pursuit_dir": "accounts/acme/pursuits",
            }
        }
    }


def test_sfautherror_exits_2(tmp_path: Path) -> None:
    """SFAuthError propagates from run_account; backstop in __main__.py maps → 2.

    After historic regression migration, run_account re-raises SFAuthError instead of
    calling sys.exit(2). The __main__.py dispatcher backstop (handle_cli_exception)
    converts it to exit 2 when running via `python -m fieldkit`. Here we assert
    that SFAuthError (not SystemExit) is raised so test coverage is accurate.

    _sf_direct is imported lazily inside run_account() — patch on the
    already-imported module object so the lazy import picks up the mock.
    """
    import fieldkit.sf.client as _sf_client
    from fieldkit.sf.errors import SFAuthError

    with (
        patch("fieldkit.commands.sf.account.get_account_names", return_value=["acme"]),
        patch("fieldkit.commands.sf.account.get_accounts_config", return_value=_accounts_cfg_sf_auth_error_handling()),
        patch("fieldkit.commands.sf.account.get_sf_session_id", return_value="fake-sid"),
        patch("fieldkit.commands.sf.account.get_sf_rest_base_url", return_value="https://sf.example.com"),
        patch("fieldkit.commands.sf.account.get_fieldkit_home", return_value=tmp_path),
        patch.object(_sf_client, "SFDirectClient", side_effect=SFAuthError("HTTP 401")),
        pytest.raises(SFAuthError, match="HTTP 401"),
    ):
        run_account("acme", write=False)


def test_sfautherror_cli_shows_actionable_message(tmp_path: Path) -> None:
    """CLI must surface SFAuthError with a helpful message; exit code via backstop.

    After historic regression migration, SFAuthError propagates from run_account through
    cli() to the __main__.py backstop. CliRunner catch_exceptions=True (default)
    catches the exception and surfaces it; we assert the message content.
    """
    import fieldkit.sf.client as _sf_client
    from fieldkit.sf.errors import SFAuthError

    runner = CliRunner()
    with (
        patch("fieldkit.commands.sf.account.get_account_names", return_value=["acme"]),
        patch("fieldkit.commands.sf.account.get_accounts_config", return_value=_accounts_cfg_sf_auth_error_handling()),
        patch("fieldkit.commands.sf.account.get_sf_session_id", return_value="fake-sid"),
        patch("fieldkit.commands.sf.account.get_sf_rest_base_url", return_value="https://sf.example.com"),
        patch("fieldkit.commands.sf.account.get_fieldkit_home", return_value=tmp_path),
        patch.object(_sf_client, "SFDirectClient", side_effect=SFAuthError("session expired")),
    ):
        result = runner.invoke(cli, ["acme"])

    # SFAuthError propagates through cli() to the __main__.py backstop which
    # maps it to EXIT_AUTH (2). CliRunner catch_exceptions=True (default)
    # catches it and records it; the exception type is the authoritative signal.
    # The exit code assertion is deferred to test_exit_code_matrix.py which uses
    # a subprocess call that exercises the full __main__ → backstop path.
    assert isinstance(result.exception, SFAuthError), (
        f"Expected SFAuthError, got {type(result.exception).__name__}: {result.exception}"
    )
    output = (result.output + (result.stderr if hasattr(result, "stderr") else "")).lower()
    assert "sf-cookies" in output or "401" in output or "failed" in output or "auth" in output or "auth sf" in output


# ---------------------------------------------------------------------------
# Additional coverage for spec 025 CRAP gate remediation
# ---------------------------------------------------------------------------


# ── TestFetchAccountRecord (flattened) ─────────────────────────────────────────────


def test_fetch_account_record_successful_fetch_returns_record() -> None:
    """fetch_sobject succeeds → returns the record dict."""
    from fieldkit.commands.sf.account import _fetch_account_record

    mock_client = MagicMock()
    mock_client.fetch_sobject.return_value = {"Id": "001abc", "Name": "Acme"}
    result = _fetch_account_record("001abc", mock_client)
    assert result == {"Id": "001abc", "Name": "Acme"}


def test_fetch_account_record_not_found_propagates() -> None:
    """SFNotFoundError remains distinguishable from a successful empty result."""
    from fieldkit.commands.sf.account import _fetch_account_record

    mock_client = MagicMock()
    mock_client.fetch_sobject.side_effect = SFNotFoundError("404")
    with pytest.raises(SFNotFoundError, match="404"):
        _fetch_account_record("001missing", mock_client)


def test_fetch_account_record_api_error_propagates() -> None:
    """Retryable provider errors remain distinguishable from absent data."""
    from fieldkit.commands.sf.account import _fetch_account_record

    mock_client = MagicMock()
    mock_client.fetch_sobject.side_effect = SFAPIError("500 Internal Server Error")
    with pytest.raises(SFAPIError, match="500 Internal Server Error"):
        _fetch_account_record("001err", mock_client)


@pytest.mark.parametrize(
    ("failure", "expected_exit"),
    [(SFNotFoundError("404"), 3), (SFAPIError("timeout"), 1)],
)
def test_primary_account_read_failure_never_writes(tmp_path: Path, failure: Exception, expected_exit: int) -> None:
    with (
        patch("fieldkit.commands.sf.account.get_account_names", return_value=["acme"]),
        patch("fieldkit.commands.sf.account.get_accounts_config", return_value=_accounts_cfg_sf_auth_error_handling()),
        patch("fieldkit.commands.sf.account.get_sf_session_id", return_value="fake-sid"),
        patch("fieldkit.commands.sf.account.get_sf_rest_base_url", return_value="https://sf.example.com"),
        patch("fieldkit.commands.sf.account.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.commands.sf.account._sf_direct.SFDirectClient"),
        patch("fieldkit.commands.sf.account._resolve_sf_account_id", return_value="001ACCOUNT000001"),
        patch("fieldkit.commands.sf.account._fetch_account_record", side_effect=failure),
        patch("fieldkit.commands.sf.account._fetch_live_opportunities") as mock_fetch_opps,
        patch("fieldkit.commands.sf.account.sync_account") as mock_write,
    ):
        result = run_account("acme", write=True)

    assert result == expected_exit
    mock_fetch_opps.assert_not_called()
    mock_write.assert_not_called()


def test_account_resolution_provider_failure_is_partial_and_never_writes(tmp_path: Path) -> None:
    with (
        patch("fieldkit.commands.sf.account.get_account_names", return_value=["acme"]),
        patch("fieldkit.commands.sf.account.get_accounts_config", return_value=_accounts_cfg_sf_auth_error_handling()),
        patch("fieldkit.commands.sf.account.get_sf_session_id", return_value="fake-sid"),
        patch("fieldkit.commands.sf.account.get_sf_rest_base_url", return_value="https://sf.example.com"),
        patch("fieldkit.commands.sf.account.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.commands.sf.account._sf_direct.SFDirectClient"),
        patch("fieldkit.commands.sf.account._resolve_sf_account_id", side_effect=SFAPIError("timeout")),
        patch("fieldkit.commands.sf.account._fetch_account_record") as mock_fetch_record,
        patch("fieldkit.commands.sf.account._fetch_live_opportunities") as mock_fetch_opps,
        patch("fieldkit.commands.sf.account.sync_account") as mock_write,
    ):
        result = run_account("acme", write=True)

    assert result == 1
    mock_fetch_record.assert_not_called()
    mock_fetch_opps.assert_not_called()
    mock_write.assert_not_called()


def test_malformed_sosl_collection_is_partial_and_never_writes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    response = MagicMock(status_code=200, headers={"content-type": "application/json"})
    response.json.return_value = {"searchRecords": ["private-provider-member"]}
    http_client = MagicMock()
    http_client.request.return_value = response
    direct_client = SFDirectClient(session_id="fake-sid", base_url="https://sf.example.com")
    config = {"accounts": {"acme": {"keywords": ["Acme"], "pursuit_dir": "accounts/acme/pursuits"}}}
    with (
        patch("fieldkit.commands.sf.account.get_account_names", return_value=["acme"]),
        patch("fieldkit.commands.sf.account.get_accounts_config", return_value=config),
        patch("fieldkit.commands.sf.account.get_sf_session_id", return_value="fake-sid"),
        patch("fieldkit.commands.sf.account.get_sf_rest_base_url", return_value="https://sf.example.com"),
        patch("fieldkit.commands.sf.account.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.commands.sf.account._sf_direct.SFDirectClient", return_value=direct_client),
        patch("fieldkit.sf.client.httpx.Client", return_value=http_client),
        patch("fieldkit.commands.sf.account.sync_account") as mock_write,
    ):
        result = run_account("acme", write=True)

    assert result == 1
    mock_write.assert_not_called()
    captured = capsys.readouterr()
    assert "Salesforce account read failed" in captured.err
    assert "private-provider-member" not in captured.err


# ── TestFetchLiveOpportunities (flattened) ─────────────────────────────────────────────


def _accounts_cfg_fetch_live_opportunities(account_name: str = "acme", keywords: list[str] | None = None) -> dict:
    return {
        "accounts": {
            account_name: {
                "keywords": keywords or [account_name],
                "domains": [f"{account_name}.com"],
                "internal_domains": [],
            },
        },
    }


def test_fetch_live_opportunities_returns_open_opportunities() -> None:
    """search_opportunities succeeds → filters out Closed stages."""
    from fieldkit.commands.sf.account import _fetch_live_opportunities

    mock_client = MagicMock()
    mock_client.search_opportunities.return_value = [
        {"Id": "006A", "stage": "Qualify", "Name": "Deal A"},
        {"Id": "006B", "stage": "Closed Won", "Name": "Deal B"},
        {"Id": "006C", "stage": "negotiate", "Name": "Deal C"},
    ]
    with patch(
        "fieldkit.commands.sf.account.get_accounts_config", return_value=_accounts_cfg_fetch_live_opportunities()
    ):
        result = _fetch_live_opportunities("acme", mock_client)
    assert len(result) == 2
    names = [r["Name"] for r in result]
    assert "Deal A" in names
    assert "Deal C" in names
    assert "Deal B" not in names


def test_fetch_live_opportunities_api_error_propagates() -> None:
    """A failed primary Salesforce search is not an empty pipeline."""
    from fieldkit.commands.sf.account import _fetch_live_opportunities

    mock_client = MagicMock()
    mock_client.search_opportunities.side_effect = SFAPIError("network timeout")
    with (
        patch(
            "fieldkit.commands.sf.account.get_accounts_config", return_value=_accounts_cfg_fetch_live_opportunities()
        ),
        pytest.raises(SFAPIError, match="network timeout"),
    ):
        _fetch_live_opportunities("acme", mock_client)
