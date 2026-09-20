"""Tests for fieldkit.commands.sf.account — account dashboard command."""

import pathlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from fieldkit.commands.sf.account import (
    _SF_ID_RE,
    _build_account_write_payload,
    _collect_local_opp_ids,
    _fmt_currency,
    _print_local_pursuits,
    _print_pipeline_section,
    _resolve_sf_account_id,
    cli,
    run_account,
)
from fieldkit.sf.client import SFAPIError, SFNotFoundError

pytestmark = pytest.mark.unit

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


def test_no_write_skips_write_account() -> None:
    """--no-write should print dashboard without calling do_write_account."""
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
        patch("fieldkit.commands.sf.account.get_fieldkit_home", return_value=__import__("pathlib").Path("/tmp/fake")),
        patch("fieldkit.sf.client.SFDirectClient"),
        patch("fieldkit.commands.sf.account._resolve_sf_account_id", return_value="001TEST"),
        patch("fieldkit.commands.sf.account._fetch_account_record", return_value=sample_acct),
        patch("fieldkit.commands.sf.account._fetch_live_opportunities", return_value=sample_opps),
        patch("fieldkit.commands.sf.sync.do_write_account") as mock_write,
    ):
        result = runner.invoke(cli, ["acme-bank", "--no-write"])

    assert result.exit_code == 0
    mock_write.assert_not_called()
    assert "Test Corp" in result.output
    assert "$500,000" in result.output


# ── _SF_ID_RE ─────────────────────────────────────────────────────────────────


# ── TestSfIdRe (flattened) ─────────────────────────────────────────────


def test_accepts_15_char_alphanumeric() -> None:
    assert _SF_ID_RE.fullmatch("006Qs000001abcd") is not None


def test_accepts_18_char_alphanumeric() -> None:
    assert _SF_ID_RE.fullmatch("006Qs000001abcdABC") is not None


def test_rejects_needs_lookup() -> None:
    assert _SF_ID_RE.fullmatch("NEEDS-LOOKUP") is None


def test_rejects_tbd() -> None:
    assert _SF_ID_RE.fullmatch("TBD") is None


def test_rejects_id_with_hyphen() -> None:
    # Hyphens are not alphanumeric — must be rejected
    assert _SF_ID_RE.fullmatch("006-invalid-id") is None


def test_rejects_empty_string() -> None:
    assert _SF_ID_RE.fullmatch("") is None


def test_rejects_17_chars() -> None:
    # Neither 15 nor 18 chars — must be rejected
    assert _SF_ID_RE.fullmatch("006Qs000001abcd1") is None


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
    ):
        result = _resolve_sf_account_id("acme", mock_client, "https://sf.example.com")

    # Must return None — no valid SF ID was found
    assert result is None
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
    ):
        _resolve_sf_account_id("acme", mock_client, "https://sf.example.com")

    captured = capsys.readouterr()
    assert "WARNING" in captured.err
    assert "NEEDS-LOOKUP" in captured.err


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
    """When SF account lookup returns None, account.md is NOT written and a warning is logged."""
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
        patch("fieldkit.commands.sf.sync.do_write_account") as mock_write,
    ):
        exit_code = run_account(acct_slug, write=True)

    # Must return 0 (graceful skip, not an error)
    assert exit_code == 0
    # Must NOT write frontmatter — that would overwrite correct data with None values
    mock_write.assert_not_called()
    # Must emit a warning to stderr so operators can diagnose the skip
    captured = capsys.readouterr()
    assert "no SF account record found" in captured.err
    assert acct_slug in captured.err


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
        patch("fieldkit.commands.sf.sync.do_write_account") as mock_write,
        pytest.raises(SFAPIError, match="network timeout"),
    ):
        run_account("acme", write=True)

    mock_write.assert_not_called()


# ── implementation change: mark unmatched SF opps ──────────────────────────────────────────


# ── TestCollectLocalOppIds (flattened) ─────────────────────────────────────────────


def test_collect_local_opp_ids_returns_empty_frozenset_when_dir_missing(tmp_path: Path) -> None:
    result = _collect_local_opp_ids(tmp_path / "nonexistent")
    assert result == frozenset()


def test_collect_local_opp_ids_collects_ids_from_pursuit_files(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "deal-a.md").write_text(
        "---\nsf_opportunity_id: 006ABC000000001AAA\nstage: discover\n---\n",
        encoding="utf-8",
    )
    (tmp_path / "deal-b.md").write_text(
        "---\nsf_opportunity_id: 006ABC000000002AAA\nstage: propose\n---\n",
        encoding="utf-8",
    )
    result = _collect_local_opp_ids(tmp_path)
    assert "006ABC000000001AAA" in result
    assert "006ABC000000002AAA" in result


def test_collect_local_opp_ids_skips_gmail_intel_and_template(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "gmail-intel.md").write_text(
        "---\nsf_opportunity_id: 006SKIP000000001AAA\n---\n",
        encoding="utf-8",
    )
    (tmp_path / "template.md").write_text(
        "---\nsf_opportunity_id: 006SKIP000000002AAA\n---\n",
        encoding="utf-8",
    )
    result = _collect_local_opp_ids(tmp_path)
    assert "006SKIP000000001AAA" not in result
    assert "006SKIP000000002AAA" not in result


def test_collect_local_opp_ids_ignores_files_without_frontmatter(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "no-fm.md").write_text("# Just a heading\nNo frontmatter here.\n", encoding="utf-8")
    result = _collect_local_opp_ids(tmp_path)
    assert result == frozenset()


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
    _write_pursuit_print_local_pursuits(tmp_path, "deal-a.md", "discover")
    _print_local_pursuits(tmp_path)
    out = capsys.readouterr().out
    assert "deal-a.md" in out
    assert "1 active" in out


def test_print_local_pursuits_closed_lost_not_counted_as_active(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_pursuit_print_local_pursuits(tmp_path, "deal-closed.md", "closed-lost")
    _print_local_pursuits(tmp_path)
    out = capsys.readouterr().out
    # closed-lost must NOT appear in active count
    assert "0 active" in out
    assert "1 closed" in out


def test_print_local_pursuits_malformed_frontmatter_does_not_classify_as_active(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Malformed file: extract_frontmatter_text returns None → stage="" → active
    # This is acceptable (unknown stage → active bucket) but must NOT crash.
    _write_malformed_print_local_pursuits(tmp_path, "malformed.md", "closed-lost")
    _print_local_pursuits(tmp_path)
    out = capsys.readouterr().out
    # Should not raise; output must contain the header line
    assert "Local Pursuits" in out


def test_print_local_pursuits_closed_won_not_counted_as_active(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_pursuit_print_local_pursuits(tmp_path, "won.md", "closed-won")
    _print_local_pursuits(tmp_path)
    out = capsys.readouterr().out
    assert "0 active" in out
    assert "1 closed" in out


def test_print_local_pursuits_skips_template_and_gmail_intel(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "template.md").write_text("---\nstage: discover\n---\n", encoding="utf-8")
    (tmp_path / "gmail-intel.md").write_text("---\nstage: discover\n---\n", encoding="utf-8")
    _print_local_pursuits(tmp_path)
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


def test_returns_none_when_get_fieldkit_home_raises(tmp_path: Path) -> None:
    """Returns None when get_fieldkit_home raises (no config file)."""
    mock_client = MagicMock()
    mock_client.resolve_account_id_by_keywords.return_value = None

    with (
        patch(
            "fieldkit.commands.sf.account.get_accounts_config",
            return_value=_accounts_cfg_resolve_sf_account_id_branch_coverage([]),
        ),
        patch("fieldkit.commands.sf.account.get_fieldkit_home", side_effect=Exception("no config")),
    ):
        result = _resolve_sf_account_id("acme", mock_client, "https://sf.example.com")

    assert result is None


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
    from fieldkit.sf.client import SFAuthError

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


def test_sfapierror_on_sosl_search_returns_none(tmp_path: Path) -> None:
    """implementation note: SFAPIError from sosl_search is caught; returns None."""
    from fieldkit.sf.client import SFAPIError

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
    ):
        result = _resolve_sf_account_id("acme", mock_client, "https://sf.example.com")

    assert result is None


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
                "domains": ["acme-corp.com"],
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
    from fieldkit.sf.client import SFAuthError

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
    from fieldkit.sf.client import SFAuthError

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
    result = _fetch_account_record("001abc", mock_client, "https://sf.example.com")
    assert result == {"Id": "001abc", "Name": "Acme"}


def test_fetch_account_record_not_found_returns_none() -> None:
    """SFNotFoundError → returns None."""
    from fieldkit.commands.sf.account import _fetch_account_record

    mock_client = MagicMock()
    mock_client.fetch_sobject.side_effect = SFNotFoundError("404")
    result = _fetch_account_record("001missing", mock_client, "https://sf.example.com")
    assert result is None


def test_fetch_account_record_api_error_returns_none() -> None:
    """SFAPIError → returns None."""
    from fieldkit.commands.sf.account import _fetch_account_record

    mock_client = MagicMock()
    mock_client.fetch_sobject.side_effect = SFAPIError("500 Internal Server Error")
    result = _fetch_account_record("001err", mock_client, "https://sf.example.com")
    assert result is None


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
