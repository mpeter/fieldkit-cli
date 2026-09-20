"""Tests for sf_pipeline.sync."""

import json
from pathlib import Path
from typing import Any

import pytest

import fieldkit.commands.sf.sync as mod
from fieldkit.commands.sf.sync import (
    _extract_opp_id,
    _known_accounts,
    _parse_json,
    _project_root,
    _validate_opp_id,
    do_list_pursuits,
    do_match_pursuit,
    do_write_account,
    do_write_opp,
)
from tests.conftest import OPP_BANK, OPP_GPAY

# Sync-specific extra constants
OPP_TMPL = "006TMPL00000000AAA"
OPP_GMAI = "006GMAI00000000AAA"


@pytest.fixture
def project_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a minimal project tree and patch _project_root to use it."""
    (tmp_path / "accounts" / "global-pay" / "pursuits").mkdir(parents=True)
    (tmp_path / "accounts" / "acme-bank" / "pursuits").mkdir(parents=True)
    (tmp_path / "accounts" / "shield-ins" / "pursuits").mkdir(parents=True)
    (tmp_path / ".cache" / "salesforce" / "accounts").mkdir(parents=True)

    import fieldkit.commands.sf.sync as mod

    monkeypatch.setattr(mod, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(mod, "_known_accounts", lambda: ("global-pay", "acme-bank", "shield-ins"))
    return tmp_path


def _write_account_file(tree: Path, account: str) -> Path:
    fp = tree / "accounts" / account / "account.md"
    fp.write_text("---\ntitle: Test Account\n---\n\n# Account\n", encoding="utf-8")
    return fp


# ── extract / validate ───────────────────────────────────────────────────────


# ── TestExtractOppId (flattened) ────────────────────────────────────────────


def test_extract_opp_id_extracts_underscore_key(tmp_path: Path) -> None:
    f = tmp_path / "p.md"
    f.write_text(f"---\nsf_opportunity_id: {OPP_GPAY}\n---\n", encoding="utf-8")
    assert _extract_opp_id(f) == OPP_GPAY


def test_extract_opp_id_extracts_hyphen_key(tmp_path: Path) -> None:
    f = tmp_path / "p.md"
    f.write_text(f"---\nsf-opportunity-id: {OPP_GPAY}\n---\n", encoding="utf-8")
    assert _extract_opp_id(f) == OPP_GPAY


def test_extract_opp_id_returns_none_without_frontmatter(tmp_path: Path) -> None:
    f = tmp_path / "p.md"
    f.write_text("# No frontmatter\n", encoding="utf-8")
    assert _extract_opp_id(f) is None


def test_extract_opp_id_returns_none_for_empty_value(tmp_path: Path) -> None:
    f = tmp_path / "p.md"
    f.write_text("---\nsf_opportunity_id: \n---\n", encoding="utf-8")
    assert _extract_opp_id(f) is None


def test_extract_opp_id_strips_quotes(tmp_path: Path) -> None:
    f = tmp_path / "p.md"
    f.write_text(f'---\nsf_opportunity_id: "{OPP_GPAY}"\n---\n', encoding="utf-8")
    assert _extract_opp_id(f) == OPP_GPAY


def test_extract_opp_id_returns_none_for_comment_artifact(tmp_path: Path) -> None:
    """YAML comment artifact `sf_opportunity_id: # was 006...` must return None."""
    f = tmp_path / "p.md"
    f.write_text("---\nsf_opportunity_id: # was 006ABC123456789ABC\n---\n", encoding="utf-8")
    assert _extract_opp_id(f) is None


def test_extract_opp_id_returns_none_for_quoted_comment_string(tmp_path: Path) -> None:
    """Quoted comment string `sf_opportunity_id: '# was 006...'` must return None."""
    f = tmp_path / "p.md"
    f.write_text("---\nsf_opportunity_id: '# was 006ABC123456789ABC'\n---\n", encoding="utf-8")
    assert _extract_opp_id(f) is None


def test_extract_opp_id_warns_on_comment_artifact(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A YAML comment artifact must emit a warning with the filepath."""
    import logging

    f = tmp_path / "p.md"
    f.write_text("---\nsf_opportunity_id: # was 006ABC123456789ABC\n---\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        _extract_opp_id(f)
    assert any("comment artifact" in r.message for r in caplog.records)


def test_extract_opp_id_warns_on_corrupt_quoted_value(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A quoted corrupt value must emit a warning mentioning 'corrupt'."""
    import logging

    f = tmp_path / "p.md"
    f.write_text("---\nsf_opportunity_id: '# was 006ABC123456789ABC'\n---\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        _extract_opp_id(f)
    assert any("corrupt" in r.message for r in caplog.records)


# ── TestValidateOppId (flattened) ───────────────────────────────────────────


def test_validate_opp_id_valid_15() -> None:
    assert _validate_opp_id("006ABC123456789")


def test_validate_opp_id_valid_18() -> None:
    assert _validate_opp_id(OPP_GPAY)


def test_validate_opp_id_invalid_short() -> None:
    assert not _validate_opp_id("006ABC")


def test_validate_opp_id_invalid_special_chars() -> None:
    assert not _validate_opp_id("006ABC12345678!")


def test_validate_opp_id_rejects_comment_string() -> None:
    assert not _validate_opp_id("# was 006ABC123456789ABC")


def test_validate_opp_id_rejects_blank() -> None:
    assert not _validate_opp_id("")


def test_validate_opp_id_rejects_placeholder_tbd() -> None:
    assert not _validate_opp_id("TBD")


def test_validate_opp_id_rejects_placeholder_todo() -> None:
    assert not _validate_opp_id("todo")


def test_validate_opp_id_rejects_placeholder_na() -> None:
    assert not _validate_opp_id("N/A")


# ── TestExtractOppIdPlaceholder (flattened) ─────────────────────────────────


@pytest.mark.parametrize("placeholder", ["TBD", "placeholder", "todo", "N/A", ""])
def test_extract_opp_id_extract_opp_id_placeholder_returns_none(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, placeholder: str
) -> None:
    f = tmp_path / "p.md"
    f.write_text(f"---\nsf_opportunity_id: {placeholder}\n---\n", encoding="utf-8")
    with caplog.at_level("WARNING"):
        result = _extract_opp_id(f)
    assert result is None
    if placeholder:
        assert "placeholder" in caplog.text.lower()


# ── list-pursuits ────────────────────────────────────────────────────────────


# ── TestListPursuits (flattened) ────────────────────────────────────────────


def test_do_list_pursuits_lists_single_account(
    project_tree: Path, capsys: pytest.CaptureFixture[str], write_pursuit_sf: Any
) -> None:
    write_pursuit_sf(project_tree, "global-pay", "deal-a", OPP_GPAY)
    write_pursuit_sf(project_tree, "global-pay", "deal-b")  # no opp id
    do_list_pursuits("global-pay")
    out = capsys.readouterr().out.strip()
    assert OPP_GPAY in out
    assert "deal-b" not in out


def test_do_list_pursuits_lists_all_accounts(
    project_tree: Path, capsys: pytest.CaptureFixture[str], write_pursuit_sf: Any
) -> None:
    write_pursuit_sf(project_tree, "global-pay", "v-deal", OPP_GPAY)
    write_pursuit_sf(project_tree, "acme-bank", "b-deal", OPP_BANK)
    do_list_pursuits("--all")
    out = capsys.readouterr().out
    assert OPP_GPAY in out
    assert OPP_BANK in out


def test_do_list_pursuits_skips_template_and_gmail_intel(
    project_tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (project_tree / "accounts" / "global-pay" / "pursuits" / "template.md").write_text(
        f"---\nsf_opportunity_id: {OPP_TMPL}\n---\n"
    )
    (project_tree / "accounts" / "global-pay" / "pursuits" / "gmail-intel.md").write_text(
        f"---\nsf_opportunity_id: {OPP_GMAI}\n---\n"
    )
    do_list_pursuits("global-pay")
    out = capsys.readouterr().out
    assert out.strip() == ""


# ── write-opp ────────────────────────────────────────────────────────────────


# ── TestWriteOpp (flattened) ────────────────────────────────────────────────


def test_do_write_opp_writes_cache_and_updates_frontmatter(project_tree: Path, write_pursuit_sf: Any) -> None:
    pf = write_pursuit_sf(project_tree, "global-pay", "deal", OPP_GPAY)
    data = {
        "status": "ok",
        "stage": "Negotiate",
        "close_date": "2026-06-01",
        "opportunity_id": OPP_GPAY,  # historic regression: must include opp ID to avoid blank-ID guard
    }
    # historic regression fix: do_write_opp no longer raises SystemExit on success because
    # _run_reconcile now returns a status string instead of calling sys.exit().
    do_write_opp(OPP_GPAY, str(pf), json.dumps(data))

    cache = project_tree / ".cache" / "salesforce" / f"{OPP_GPAY}.json"
    assert cache.exists()
    cached = json.loads(cache.read_text())
    assert cached["status"] == "ok"
    assert "pulled_at" in cached

    text = pf.read_text()
    assert "sf_stage:" in text


def test_do_write_opp_rejects_bad_status(project_tree: Path, write_pursuit_sf: Any) -> None:
    pf = write_pursuit_sf(project_tree, "global-pay", "deal", OPP_GPAY)
    with pytest.raises(SystemExit) as exc_info:
        do_write_opp(OPP_GPAY, str(pf), json.dumps({"status": "error"}))
    assert exc_info.value.code != 0


def test_do_write_opp_rejects_invalid_opp_id(project_tree: Path, write_pursuit_sf: Any) -> None:
    pf = write_pursuit_sf(project_tree, "global-pay", "deal", "bad")
    with pytest.raises(SystemExit) as exc_info:
        do_write_opp("bad", str(pf), json.dumps({"status": "ok"}))
    assert exc_info.value.code != 0


# ── write-account ────────────────────────────────────────────────────────────


# ── TestWriteAccount (flattened) ────────────────────────────────────────────


def test_do_write_account_writes_cache_and_frontmatter(project_tree: Path) -> None:
    _write_account_file(project_tree, "global-pay")
    data = {"status": "ok", "account_id": "001VISA", "name": "Global Pay Inc."}
    do_write_account("global-pay", json.dumps(data))

    cache = project_tree / ".cache" / "salesforce" / "accounts" / "global-pay.json"
    assert cache.exists()
    assert json.loads(cache.read_text())["name"] == "Global Pay Inc."


def test_do_write_account_rejects_unknown_account(project_tree: Path) -> None:
    with pytest.raises(SystemExit) as exc_info:
        do_write_account("unknown-corp", json.dumps({"status": "ok"}))
    assert exc_info.value.code != 0


# ── match-pursuit ────────────────────────────────────────────────────────────


# ── TestMatchPursuit (flattened) ────────────────────────────────────────────


def test_do_match_pursuit_finds_matching_file(
    project_tree: Path, capsys: pytest.CaptureFixture[str], write_pursuit_sf: Any
) -> None:
    write_pursuit_sf(project_tree, "global-pay", "deal", OPP_GPAY)
    pdir = str(project_tree / "accounts" / "global-pay" / "pursuits")
    do_match_pursuit(pdir, OPP_GPAY)
    out = capsys.readouterr().out.strip()
    assert "deal.md" in out


def test_do_match_pursuit_returns_nothing_for_no_match(
    project_tree: Path, capsys: pytest.CaptureFixture[str], write_pursuit_sf: Any
) -> None:
    write_pursuit_sf(project_tree, "global-pay", "deal", OPP_GPAY)
    pdir = str(project_tree / "accounts" / "global-pay" / "pursuits")
    do_match_pursuit(pdir, "006NOMATCH0000AAA")
    assert capsys.readouterr().out.strip() == ""


def test_do_match_pursuit_returns_nothing_for_missing_dir(
    project_tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    do_match_pursuit("/nonexistent/dir", OPP_GPAY)
    assert capsys.readouterr().out.strip() == ""


# ── New gap tests (T03) ───────────────────────────────────────────────────────


# ── TestProjectRoot (flattened) ─────────────────────────────────────────────


def test_project_root_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Setting SF_PIPELINE_ROOT makes _project_root() return that path."""
    monkeypatch.setenv("SF_PIPELINE_ROOT", "/tmp/custom")
    # Call the real function (not the monkeypatched mod attribute from project_tree)
    result = _project_root()
    assert result == Path("/tmp/custom")


def test_project_root_default_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without env var, _project_root() delegates to get_fieldkit_home() from fieldkit.config."""
    monkeypatch.delenv("SF_PIPELINE_ROOT", raising=False)
    monkeypatch.setattr("fieldkit.commands.sf.sync.get_fieldkit_home", lambda: Path("/tmp/config-root"))
    result = _project_root()
    assert result == Path("/tmp/config-root")


def test_project_root_config_derived_not_file_relative(monkeypatch: pytest.MonkeyPatch) -> None:
    """_project_root() uses get_fieldkit_home(), not __file__-relative path."""
    monkeypatch.delenv("SF_PIPELINE_ROOT", raising=False)
    monkeypatch.setattr("fieldkit.commands.sf.sync.get_fieldkit_home", lambda: Path("/tmp/test-data-root"))
    result = _project_root()
    assert result == Path("/tmp/test-data-root")
    # Prove path is NOT derived from __file__ (which would contain 'sf_pipeline')
    assert "sf_pipeline" not in str(result)


def test_project_root_env_override_takes_precedence_over_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """SF_PIPELINE_ROOT env var wins over get_fieldkit_home() from fieldkit.config."""
    monkeypatch.setenv("SF_PIPELINE_ROOT", "/tmp/override")
    monkeypatch.setattr("fieldkit.commands.sf.sync.get_fieldkit_home", lambda: Path("/tmp/config-path"))
    result = _project_root()
    assert result == Path("/tmp/override")


# ── TestParseJson (flattened) ───────────────────────────────────────────────


def test_parse_json_invalid_json_exits() -> None:
    """Invalid JSON string causes SystemExit(1)."""
    with pytest.raises(SystemExit) as exc_info:
        _parse_json("not-json")
    assert exc_info.value.code == 1


def test_parse_json_valid_json_returns_dict() -> None:
    """Valid JSON string returns a dict."""
    result = _parse_json('{"key": "val"}')
    assert result == {"key": "val"}


def test_parse_json_empty_object_returns_empty_dict() -> None:
    """Empty JSON object returns empty dict."""
    assert _parse_json("{}") == {}


# ── TestListPursuitsEdgeCases (flattened) ───────────────────────────────────


def test_do_list_pursuits_account_dir_not_exists_skipped(
    project_tree: Path, capsys: pytest.CaptureFixture[str], write_pursuit_sf: Any
) -> None:
    """An account in KNOWN_ACCOUNTS whose directory does not exist is silently skipped."""
    # project_tree has global-pay/acme-bank/shield-ins dirs but no pursuit files;
    # we rely on the fact that do_list_pursuits("--all") iterates all accounts
    # and hits the `if not pdir.is_dir(): continue` branch for any missing ones.
    # Remove one directory to force the missing-dir branch.
    import shutil

    shutil.rmtree(project_tree / "accounts" / "acme-bank")
    write_pursuit_sf(project_tree, "global-pay", "v-deal", OPP_GPAY)
    do_list_pursuits("--all")
    out = capsys.readouterr().out
    # global-pay deal is present; no crash even though acme-bank dir is gone
    assert OPP_GPAY in out


def test_do_list_pursuits_nonexistent_single_account_skipped(
    project_tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Passing a specific account whose dir is missing produces no output and no crash."""
    do_list_pursuits("nonexistent-account")
    assert capsys.readouterr().out.strip() == ""


# ── TestWriteAccountEdgeCases (flattened) ───────────────────────────────────


def test_do_write_account_unknown_account_exits(project_tree: Path) -> None:
    """An account name not in KNOWN_ACCOUNTS causes SystemExit(1)."""
    with pytest.raises(SystemExit) as exc_info:
        do_write_account("unknown-acct", json.dumps({"status": "ok"}))
    assert exc_info.value.code == 1


def test_do_write_account_account_file_not_found_exits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Account is known but account.md does not exist → SystemExit(1)."""
    # Monkeypatch _known_accounts to include a test account name
    monkeypatch.setattr(mod, "_known_accounts", lambda: ("global-pay", "acme-bank", "shield-ins", "test-acct"))
    # Monkeypatch _project_root to use tmp_path
    monkeypatch.setattr(mod, "_project_root", lambda: tmp_path)
    # Create the account directory but NOT account.md
    (tmp_path / "accounts" / "test-acct").mkdir(parents=True)
    (tmp_path / ".cache" / "salesforce" / "accounts").mkdir(parents=True)
    with pytest.raises(SystemExit) as exc_info:
        do_write_account("test-acct", json.dumps({"status": "ok"}))
    assert exc_info.value.code == 1


def test_do_write_account_bad_status_exits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Account is known, account.md exists, but JSON status != 'ok' → SystemExit(1)."""
    monkeypatch.setattr(mod, "_known_accounts", lambda: ("global-pay", "acme-bank", "shield-ins", "test-acct"))
    monkeypatch.setattr(mod, "_project_root", lambda: tmp_path)
    (tmp_path / "accounts" / "test-acct").mkdir(parents=True)
    (tmp_path / ".cache" / "salesforce" / "accounts").mkdir(parents=True)
    # Create account.md so the not-found branch is skipped
    (tmp_path / "accounts" / "test-acct" / "account.md").write_text("---\ntitle: Test\n---\n", encoding="utf-8")
    with pytest.raises(SystemExit) as exc_info:
        do_write_account("test-acct", json.dumps({"status": "error"}))
    assert exc_info.value.code == 1


# ── TestWriteOppReconcile (flattened) ───────────────────────────────────────


def test_do_write_opp_reconcile_called(project_tree: Path, write_pursuit_sf: Any) -> None:
    """do_write_opp calls _reconcile_with_path; patch it to a no-op and verify reachability."""
    reconcile_called = []

    def _fake_reconcile(path: str) -> None:
        reconcile_called.append(path)

    monkeypatch_obj = pytest.MonkeyPatch()
    monkeypatch_obj.setattr(mod, "_reconcile_with_path", _fake_reconcile)

    pf = write_pursuit_sf(project_tree, "global-pay", "deal", OPP_GPAY)
    _write_account_file(project_tree, "global-pay")
    data = {"status": "ok", "stage": "Negotiate", "close_date": "2026-06-01"}

    # Patch _run_sf_mode to avoid real frontmatter write side-effects
    monkeypatch_obj.setattr(mod, "_run_sf_mode", lambda *a, **kw: None)

    do_write_opp(OPP_GPAY, str(pf), json.dumps(data))

    monkeypatch_obj.undo()
    assert reconcile_called, "_reconcile_with_path was not called by do_write_opp"


# ── Regression: _known_accounts must NOT be cached ───────────────────────────


# ── TestKnownAccountsNotCached (flattened) ──────────────────────────────────


def test_known_accounts_known_accounts_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    call_count = [0]

    def _fake_load_known_accounts() -> tuple[str, ...]:
        call_count[0] += 1
        if call_count[0] == 1:
            return ("acme-bank",)
        return ("global-pay",)

    monkeypatch.setattr(mod, "_load_known_accounts", _fake_load_known_accounts)

    first = _known_accounts()
    second = _known_accounts()

    assert first != second, "_known_accounts() returned the same value on both calls — @cache may have been re-added"


# ── S09 regression: warning log paths ────────────────────────────────────────


# Frontmatter with a valid sf_opportunity_id but an invalid stage value.
# load_pursuit raises ValueError on invalid stage, triggering the fallback path.
INVALID_STAGE_FM = """\
---
sf_opportunity_id: {opp_id}
stage: invalid-stage
gate-status: pending
meddpicc:
  metrics: 0
  economic-buyer: 0
  decision-criteria: 0
  decision-process: 0
  paper-process: 0
  identify-pain: 0
  champion: 0
  competition: 0
---

# Pursuit
"""


# ── TestExtractOppIdWarnsOnInvalidFrontmatter (flattened) ───────────────────


def test_extract_opp_id_warns_and_returns_opp_id(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """File with invalid stage triggers warning but still returns the opp_id."""
    import logging

    f = tmp_path / "p.md"
    f.write_text(INVALID_STAGE_FM.format(opp_id=OPP_GPAY), encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="fieldkit.commands.sf.sync"):
        result = _extract_opp_id(f)

    assert result == OPP_GPAY
    warning_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("load_pursuit failed" in m or "fallback" in m for m in warning_msgs), (
        f"Expected a warning about load_pursuit failure/fallback, got: {warning_msgs}"
    )


# ── TestDoWriteOppWarnsOnPostWriteValidationFailure (flattened) ─────────────


def test_do_write_opp_warns_on_post_write_failure(
    project_tree: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    write_pursuit_sf: Any,
) -> None:
    """When load_pursuit raises ValueError after write, a warning is emitted."""
    import logging

    pf = write_pursuit_sf(project_tree, "global-pay", "deal", OPP_GPAY)

    # No-op the write-side effects so we reach post-write validation
    monkeypatch.setattr(mod, "_run_sf_mode", lambda *a, **kw: None)
    monkeypatch.setattr(mod, "_reconcile_with_path", lambda *a, **kw: None)

    # Make load_pursuit raise ValueError to simulate a file that still fails validation
    def _bad_load_pursuit(path: object) -> object:
        raise ValueError("bad stage")

    monkeypatch.setattr(mod, "load_pursuit", _bad_load_pursuit)

    data = {"status": "ok", "stage": "Negotiate", "close_date": "2026-06-01"}
    with caplog.at_level(logging.WARNING, logger="fieldkit.commands.sf.sync"):
        do_write_opp(OPP_GPAY, str(pf), json.dumps(data))

    warning_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("fails validation after write" in m for m in warning_msgs), (
        f"Expected a warning about post-write validation failure, got: {warning_msgs}"
    )


# ── Reconcile silent-pass fix ─────────────────────────────────────────────────


# ── TestWriteOppReconcileFailureLogged (flattened) ──────────────────────────


def test_do_write_opp_reconcile_failure_logs_warning(
    project_tree: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    write_pursuit_sf: Any,
) -> None:
    """When _reconcile_with_path raises RuntimeError, a warning with exc_info is emitted."""
    import logging

    pf = write_pursuit_sf(project_tree, "global-pay", "deal", OPP_GPAY)

    monkeypatch.setattr(mod, "_run_sf_mode", lambda *a, **kw: None)

    def _bad_reconcile(path: str) -> None:
        raise RuntimeError("reconcile exploded")

    monkeypatch.setattr(mod, "_reconcile_with_path", _bad_reconcile)
    # Suppress post-write validation so it doesn't add noise
    monkeypatch.setattr(mod, "load_pursuit", lambda *a, **kw: None)

    data = {"status": "ok", "stage": "Negotiate", "close_date": "2026-06-01"}
    with caplog.at_level(logging.WARNING):
        do_write_opp(OPP_GPAY, str(pf), json.dumps(data))

    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    reconcile_records = [r for r in warning_records if "Reconcile failed" in r.message]
    assert reconcile_records, (
        f"Expected 'Reconcile failed' warning, got messages: {[r.message for r in warning_records]}"
    )
    assert reconcile_records[0].exc_info is not None, "Expected exc_info to be set on the reconcile warning record"


# ── sf_deal_splits frontmatter write (T03) ───────────────────────────────────


# ── TestDealSplitsFrontmatter (flattened) ───────────────────────────────────


def _deal_splits_frontmatter_run_sf(pf: Path, extra: dict) -> None:
    """Call _run_sf_mode with a minimal ok payload + extra fields."""
    import json as _json

    from fieldkit.commands.sf.frontmatter import _run_sf_mode
    from tests.conftest import OPP_GPAY

    data = {
        "status": "ok",
        "stage": "discover",
        "close_date": "2026-12-31",
        "opportunity_id": OPP_GPAY,  # historic regression: include opp ID to avoid blank-ID guard
        **extra,
    }
    _run_sf_mode(str(pf), _json.dumps(data))


def test_deal_splits_frontmatter_writes_split_entries(tmp_path: Path) -> None:
    """Two split entries are written as a YAML list."""
    from tests.conftest import MINIMAL_PURSUIT_FM, OPP_GPAY

    pf = tmp_path / "deal.md"
    pf.write_text(MINIMAL_PURSUIT_FM.format(opp_id=OPP_GPAY), encoding="utf-8")

    splits = [
        {"offering": "Ansible Automation Platform", "pct": 50.0},
        {"offering": "Example Enterprise Linux", "pct": 50.0},
    ]
    _deal_splits_frontmatter_run_sf(pf, {"deal_splits": splits})

    text = pf.read_text()
    assert "sf_deal_splits:" in text
    assert "offering: Ansible Automation Platform" in text
    assert "pct: 50.0" in text
    assert "offering: Example Enterprise Linux" in text


def test_deal_splits_frontmatter_writes_empty_list(tmp_path: Path) -> None:
    """No splits → sf_deal_splits: is written with an empty block."""
    from tests.conftest import MINIMAL_PURSUIT_FM, OPP_GPAY

    pf = tmp_path / "deal.md"
    pf.write_text(MINIMAL_PURSUIT_FM.format(opp_id=OPP_GPAY), encoding="utf-8")

    _deal_splits_frontmatter_run_sf(pf, {"deal_splits": []})

    text = pf.read_text()
    assert "sf_deal_splits:" in text
    # no list items
    assert "offering:" not in text


def test_deal_splits_frontmatter_missing_deal_splits_writes_empty(tmp_path: Path) -> None:
    """When deal_splits key is absent from payload, sf_deal_splits: is still written."""
    from tests.conftest import MINIMAL_PURSUIT_FM, OPP_GPAY

    pf = tmp_path / "deal.md"
    pf.write_text(MINIMAL_PURSUIT_FM.format(opp_id=OPP_GPAY), encoding="utf-8")

    _deal_splits_frontmatter_run_sf(pf, {})  # no deal_splits key

    text = pf.read_text()
    assert "sf_deal_splits:" in text
    assert "offering:" not in text


def test_deal_splits_frontmatter_old_splits_stripped_before_rewrite(tmp_path: Path) -> None:
    """Existing sf_deal_splits block is replaced, not appended."""
    from tests.conftest import OPP_GPAY

    pf = tmp_path / "deal.md"
    # Build frontmatter that already contains sf_deal_splits inside the block
    initial = (
        "---\n"
        f"sf_opportunity_id: {OPP_GPAY}\n"
        "stage: discover\n"
        "gate-status: pending\n"
        "meddpicc:\n"
        "  metrics: 0\n"
        "  economic-buyer: 0\n"
        "  decision-criteria: 0\n"
        "  decision-process: 0\n"
        "  paper-process: 0\n"
        "  identify-pain: 0\n"
        "  champion: 0\n"
        "  competition: 0\n"
        "sf_deal_splits:\n"
        "  - offering: OldProduct\n"
        "    pct: 100.0\n"
        "---\n\n# Pursuit\n"
    )
    pf.write_text(initial, encoding="utf-8")

    # Overwrite with a different split
    _deal_splits_frontmatter_run_sf(pf, {"deal_splits": [{"offering": "NewProduct", "pct": 75.0}]})

    text = pf.read_text()
    assert "OldProduct" not in text
    assert "NewProduct" in text
    assert text.count("sf_deal_splits:") == 1


# ── Phase 8 additions (task 8.7b) ─────────────────────────────────────────────


# ── TestExtractOppIdPhase8 (flattened) ──────────────────────────────────────


def test_extract_opp_id_returns_none_for_non_matching_content(tmp_path: Path) -> None:
    """8.7b-1: File with no sf_opportunity_id key returns None."""
    f = tmp_path / "no_opp.md"
    f.write_text("---\ntitle: No Opp ID Here\nstage: discover\n---\n# Body\n", encoding="utf-8")
    assert _extract_opp_id(f) is None


def test_extract_opp_id_returns_id_for_matching_content(tmp_path: Path) -> None:
    """8.7b-2: File with a valid sf_opportunity_id returns the ID string."""
    valid_id = OPP_GPAY  # 18-char alphanumeric from conftest
    f = tmp_path / "has_opp.md"
    f.write_text(f"---\nsf_opportunity_id: {valid_id}\nstage: discover\n---\n# Body\n", encoding="utf-8")
    assert _extract_opp_id(f) == valid_id
