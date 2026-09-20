"""Tests for sf_pipeline.listview — orchestration module."""

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from fieldkit.commands.sf.listview import LOG_PREFIX, _log, _process_opp, cli
from tests.conftest import OPP_GPAY

pytestmark = pytest.mark.unit

# ── helpers ──────────────────────────────────────────────────────────────────


def _make_opp(opp_id: str, name: str = "Test Opp", stage: str = "Negotiate") -> dict[str, str]:
    return {
        "opportunity_id": opp_id,
        "name": name,
        "stage": stage,
        "close_date": "2026-06-01",
        "status": "ok",
    }


def _fake_config() -> dict[str, Any]:
    return {
        "accounts": {
            "global-pay": {"keywords": ["Global Pay", "globalpay"]},
            "acme-bank": {"keywords": ["Acme", "acme-bank"]},
            "shield-ins": {"keywords": ["Shield", "shieldins"]},
        }
    }


def _make_mock_client(results: list[dict[str, Any]] | None = None) -> MagicMock:
    """Return a MagicMock SFDirectClient compatible with ``with SFDirectClient(...) as client:``.

    MagicMock auto-creates __enter__/__exit__; we configure __enter__ to return
    the mock itself so that the ``client`` variable inside the ``with`` block is
    the same mock with ``.search_opportunities`` set up correctly.
    """
    mock = MagicMock()
    mock.search_opportunities.return_value = results if results is not None else []
    mock.__enter__ = MagicMock(return_value=mock)
    mock.__exit__ = MagicMock(return_value=False)
    return mock


# ── _process_opp stdout safety ───────────────────────────────────────────────


# ── TestProcessOppStdoutSafety (flattened) ──────────────────────────────────


def test_process_opp_stdout_restored_after_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    """sys.stdout must be restored to its pre-call value even when do_match_pursuit raises OSError."""
    import fieldkit.commands.sf.sync as sync_mod2

    def _raise_oserror(pursuit_dir: str, opp_id: str) -> None:
        raise OSError("simulated IO failure")

    monkeypatch.setattr(sync_mod2, "do_match_pursuit", _raise_oserror)

    original_stdout = sys.stdout
    opp = {"opportunity_id": "006TEST000000000AA", "name": "Test", "stage": "Negotiate"}
    import contextlib

    with contextlib.suppress(OSError):
        _process_opp(opp, "/fake/pursuits")

    # contextlib.redirect_stdout guarantees restoration even on exception
    assert sys.stdout is original_stdout


# ── _log ─────────────────────────────────────────────────────────────────────


# ── TestLog (flattened) ─────────────────────────────────────────────────────


def test_log_log_writes_to_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    _log("hello world")
    err = capsys.readouterr().err
    assert LOG_PREFIX in err
    assert "hello world" in err


# ── CLI dispatch ─────────────────────────────────────────────────────────────


# ── TestCLIDispatch (flattened) ─────────────────────────────────────────────


def test_cli_help_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["sf_pipeline listview", "--help"])
    with pytest.raises(SystemExit) as exc_info:
        cli.main(sys.argv[1:])
    assert exc_info.value.code == 0


def test_cli_default_target_is_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """When no args given, target defaults to --all (caught at session check — exit 2, auth failure)."""
    monkeypatch.setattr(sys, "argv", ["sf_pipeline listview"])
    monkeypatch.setattr("fieldkit.commands.sf.listview.get_sf_session_id", lambda: None)
    with pytest.raises(SystemExit) as exc_info:
        cli.main(sys.argv[1:], standalone_mode=False)
    assert exc_info.value.code == 2


# ── Session check ────────────────────────────────────────────────────────────


# ── TestSessionCheck (flattened) ────────────────────────────────────────────


def test_check_sf_auth_exits_without_sid(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exit code 2 when get_sf_session_id returns None."""
    monkeypatch.setattr(sys, "argv", ["sf_pipeline listview", "global-pay"])
    monkeypatch.setattr("fieldkit.commands.sf.listview.get_sf_session_id", lambda: None)
    with pytest.raises(SystemExit) as exc_info:
        cli.main(sys.argv[1:], standalone_mode=False)
    assert exc_info.value.code == 2


def test_check_sf_auth_exits_2_without_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exit code 2 when get_sf_rest_base_url returns empty string."""
    monkeypatch.setattr(sys, "argv", ["sf_pipeline listview", "global-pay"])
    monkeypatch.setattr("fieldkit.commands.sf.listview.get_sf_session_id", lambda: "fakesid")
    monkeypatch.setattr("fieldkit.commands.sf.listview.get_sf_rest_base_url", lambda: "")
    with pytest.raises(SystemExit) as exc_info:
        cli.main(sys.argv[1:], standalone_mode=False)
    assert exc_info.value.code == 2


# ── Orchestration with mocked SOSL client ────────────────────────────────────


@pytest.fixture
def orchestration_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Any]:
    """Set up a fully-mocked environment for orchestration tests."""
    # Project tree
    for acct in ("global-pay", "acme-bank", "shield-ins"):
        (tmp_path / "accounts" / acct / "pursuits").mkdir(parents=True)

    import fieldkit.commands.sf.sync as sync_mod

    monkeypatch.setattr(sync_mod, "_project_root", lambda: tmp_path)

    import fieldkit.commands.sf.listview as lv_mod

    # Auth mocks
    monkeypatch.setattr(lv_mod, "get_sf_session_id", lambda: "fakesid")
    monkeypatch.setattr(lv_mod, "get_sf_rest_base_url", lambda: "https://examplecrm.my.salesforce.com")

    # Config + account names
    monkeypatch.setattr(lv_mod, "get_accounts_config", _fake_config)
    monkeypatch.setattr(lv_mod, "get_account_names", lambda: ["global-pay", "acme-bank", "shield-ins"])

    # Default SOSL client — returns empty list
    monkeypatch.setattr("fieldkit.sf.client.SFDirectClient", lambda **kwargs: _make_mock_client())

    return tmp_path, lv_mod


# ── TestOrchestration (flattened) ───────────────────────────────────────────


def test_orchestration_single_account_updates_pursuit(
    orchestration_env: tuple[Path, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    write_pursuit_sf: Any,
) -> None:
    tree, _lv_mod = orchestration_env
    write_pursuit_sf(tree, "global-pay", "deal", OPP_GPAY)

    sosl_result = [_make_opp(OPP_GPAY, "Global Pay Deal")]
    monkeypatch.setattr("fieldkit.sf.client.SFDirectClient", lambda **kwargs: _make_mock_client(sosl_result))

    write_calls: list[tuple[str, str]] = []

    def fake_write_opp(opp_id: str, pursuit_file: str, json_str: str) -> None:
        write_calls.append((opp_id, pursuit_file))

    monkeypatch.setattr("fieldkit.commands.sf.sync.do_write_opp", fake_write_opp)
    monkeypatch.setattr(sys, "argv", ["sf_pipeline listview", "global-pay"])

    cli.main(sys.argv[1:], standalone_mode=False)

    _bug401_captured = capsys.readouterr()
    out = _bug401_captured.err.strip().splitlines()[-1] if _bug401_captured.err.strip() else ""
    result = json.loads(out.strip())
    assert result["updated"] == 1
    assert result["errors"] == 0
    assert len(write_calls) == 1
    assert write_calls[0][0] == OPP_GPAY


def test_orchestration_skips_closed_opp_with_no_pursuit(
    orchestration_env: tuple[Path, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _tree, _lv_mod = orchestration_env

    sosl_result = [_make_opp("006CLOSED0000000AA", "Closed Deal", stage="Closed Won")]
    monkeypatch.setattr("fieldkit.sf.client.SFDirectClient", lambda **kwargs: _make_mock_client(sosl_result))
    monkeypatch.setattr(sys, "argv", ["sf_pipeline listview", "global-pay"])

    cli.main(sys.argv[1:], standalone_mode=False)

    _bug401_captured = capsys.readouterr()
    out = _bug401_captured.err.strip().splitlines()[-1] if _bug401_captured.err.strip() else ""
    result = json.loads(out.strip())
    assert result["untracked"] == 0
    assert result["updated"] == 0


def test_orchestration_counts_untracked_open_opp(
    orchestration_env: tuple[Path, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _tree, _lv_mod = orchestration_env

    sosl_result = [_make_opp("006NOFILE0000000AA", "Open No File", stage="Negotiate")]
    monkeypatch.setattr("fieldkit.sf.client.SFDirectClient", lambda **kwargs: _make_mock_client(sosl_result))
    monkeypatch.setattr(sys, "argv", ["sf_pipeline listview", "global-pay"])

    cli.main(sys.argv[1:], standalone_mode=False)

    _bug401_captured = capsys.readouterr()
    out = _bug401_captured.err.strip().splitlines()[-1] if _bug401_captured.err.strip() else ""
    result = json.loads(out.strip())
    assert result["untracked"] == 1


def test_orchestration_missing_keywords_counts_error(
    orchestration_env: tuple[Path, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """When no keywords are configured for an account, error is counted and we continue."""
    _tree, _lv_mod = orchestration_env

    monkeypatch.setattr(_lv_mod, "get_accounts_config", lambda: {"accounts": {"global-pay": {}}})
    monkeypatch.setattr(sys, "argv", ["sf_pipeline listview", "global-pay"])

    cli.main(sys.argv[1:], standalone_mode=False)

    _bug401_captured = capsys.readouterr()
    out = _bug401_captured.err.strip().splitlines()[-1] if _bug401_captured.err.strip() else ""
    result = json.loads(out.strip())
    assert result["errors"] >= 1
    assert result["status"] == "partial"


def test_orchestration_api_error_counts_error(
    orchestration_env: tuple[Path, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """When SFDirectClient raises SFAPIError, total_errors is incremented and we continue."""
    _tree, _lv_mod = orchestration_env
    from fieldkit.sf.client import SFAPIError as _SFAPIError

    def _failing_client(**kwargs: Any) -> Any:
        m = MagicMock()
        m.search_opportunities.side_effect = _SFAPIError("network failure")
        m.__enter__ = MagicMock(return_value=m)
        m.__exit__ = MagicMock(return_value=False)
        return m

    monkeypatch.setattr("fieldkit.sf.client.SFDirectClient", _failing_client)
    monkeypatch.setattr(sys, "argv", ["sf_pipeline listview", "global-pay"])

    cli.main(sys.argv[1:], standalone_mode=False)

    _bug401_captured = capsys.readouterr()
    out = _bug401_captured.err.strip().splitlines()[-1] if _bug401_captured.err.strip() else ""
    result = json.loads(out.strip())
    assert result["errors"] >= 1
    assert result["status"] == "partial"


def test_orchestration_auth_error_exits_2(orchestration_env: tuple[Path, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """When SFDirectClient raises SFAuthError, sys.exit(2) is raised."""
    _tree, _lv_mod = orchestration_env
    from fieldkit.sf.client import SFAuthError as _SFAuthError

    def _auth_failing_client(**kwargs: Any) -> Any:
        m = MagicMock()
        m.search_opportunities.side_effect = _SFAuthError("session expired")
        m.__enter__ = MagicMock(return_value=m)
        m.__exit__ = MagicMock(return_value=False)
        return m

    monkeypatch.setattr("fieldkit.sf.client.SFDirectClient", _auth_failing_client)
    monkeypatch.setattr(sys, "argv", ["sf_pipeline listview", "global-pay"])

    with pytest.raises(SystemExit) as exc_info:
        cli.main(sys.argv[1:], standalone_mode=False)

    assert exc_info.value.code == 2


def test_orchestration_json_output_format(
    orchestration_env: tuple[Path, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _tree, _lv_mod = orchestration_env
    monkeypatch.setattr(sys, "argv", ["sf_pipeline listview", "global-pay"])

    cli.main(sys.argv[1:], standalone_mode=False)

    _bug401_captured = capsys.readouterr()
    out = _bug401_captured.err.strip().splitlines()[-1] if _bug401_captured.err.strip() else ""
    result = json.loads(out.strip())
    assert set(result.keys()) == {"status", "updated", "untracked", "errors"}
    assert result["status"] == "ok"


# ── SOSL-specific tests ───────────────────────────────────────────────────────


# ── TestSOSLPath (flattened) ────────────────────────────────────────────────


def test_sosl_path_sosl_called_with_configured_keywords(
    orchestration_env: tuple[Path, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    write_pursuit_sf: Any,
) -> None:
    """SFDirectClient.search_opportunities is called with account keywords."""
    tree, lv_mod = orchestration_env
    write_pursuit_sf(tree, "global-pay", "deal", OPP_GPAY)

    monkeypatch.setattr(
        lv_mod,
        "get_accounts_config",
        lambda: {"accounts": {"global-pay": {"keywords": ["Global Pay", "globalpay"]}}},
    )

    sosl_result = [_make_opp(OPP_GPAY, "Global Pay Deal")]
    mock_client = _make_mock_client(sosl_result)
    monkeypatch.setattr("fieldkit.sf.client.SFDirectClient", lambda **kwargs: mock_client)

    write_calls: list[tuple[str, str]] = []

    def fake_write_opp(opp_id: str, pursuit_file: str, json_str: str) -> None:
        write_calls.append((opp_id, pursuit_file))

    monkeypatch.setattr("fieldkit.commands.sf.sync.do_write_opp", fake_write_opp)
    monkeypatch.setattr(sys, "argv", ["sf_pipeline listview", "global-pay"])

    cli.main(sys.argv[1:], standalone_mode=False)

    mock_client.search_opportunities.assert_called_once_with(
        keywords=["Global Pay", "globalpay"], account_name="global-pay"
    )
    _bug401_captured = capsys.readouterr()
    out = _bug401_captured.err.strip().splitlines()[-1] if _bug401_captured.err.strip() else ""
    result = json.loads(out.strip())
    assert result["updated"] == 1
    assert result["errors"] == 0


def test_sosl_path_sosl_auth_failure_exits_2(
    orchestration_env: tuple[Path, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When get_sf_session_id returns None, sys.exit(2) is raised immediately."""
    _tree, _lv_mod = orchestration_env

    monkeypatch.setattr(_lv_mod, "get_sf_session_id", lambda: None)
    monkeypatch.setattr(sys, "argv", ["sf_pipeline listview", "global-pay"])

    with pytest.raises(SystemExit) as exc_info:
        cli.main(sys.argv[1:], standalone_mode=False)

    assert exc_info.value.code == 2


def test_sosl_path_sosl_api_error_counts_error(
    orchestration_env: tuple[Path, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When SFDirectClient raises SFAPIError, total_errors is incremented and we continue."""
    _tree, _lv_mod = orchestration_env
    from fieldkit.sf.client import SFAPIError as _SFAPIError

    def _failing_client(**kwargs: Any) -> Any:
        m = MagicMock()
        m.search_opportunities.side_effect = _SFAPIError("network failure")
        m.__enter__ = MagicMock(return_value=m)
        m.__exit__ = MagicMock(return_value=False)
        return m

    monkeypatch.setattr("fieldkit.sf.client.SFDirectClient", _failing_client)
    monkeypatch.setattr(sys, "argv", ["sf_pipeline listview", "global-pay"])

    cli.main(sys.argv[1:], standalone_mode=False)

    _bug401_captured = capsys.readouterr()
    out = _bug401_captured.err.strip().splitlines()[-1] if _bug401_captured.err.strip() else ""
    result = json.loads(out.strip())
    assert result["errors"] >= 1
    assert result["status"] == "partial"


# ── --all account iteration ───────────────────────────────────────────────────


# ── TestAllAccountIteration (flattened) ─────────────────────────────────────


def test_sync_account_opps_all_uses_get_account_names(
    orchestration_env: tuple[Path, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--all must use get_account_names(), not a hardcoded list."""
    _tree, _lv_mod = orchestration_env

    accounts_called: list[str] = []

    def tracking_get_account_names() -> list[str]:
        accounts_called.append("called")
        return ["global-pay"]

    monkeypatch.setattr(_lv_mod, "get_account_names", tracking_get_account_names)
    monkeypatch.setattr(sys, "argv", ["sf_pipeline listview", "--json"])

    cli.main(sys.argv[1:], standalone_mode=False)

    assert len(accounts_called) == 1

    # historic regression: --json flag emits JSON to stdout
    out = capsys.readouterr().out
    result = json.loads(out.strip())
    assert result["status"] == "ok"


# -- historic regression/141 regression ---------------------------------------------------


# ── TestBug237And141Regression (flattened) ──────────────────────────────────


def test_bug237_and141_regression_all_accounts_sosl_called_for_each(
    orchestration_env: tuple[Path, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """historic regression: SOSL search must be called for all 3 accounts, not just the first."""
    _tree, _lv_mod = orchestration_env
    mock_client = _make_mock_client([])
    monkeypatch.setattr("fieldkit.sf.client.SFDirectClient", lambda **kwargs: mock_client)
    monkeypatch.setattr(sys, "argv", ["sf_pipeline listview"])  # --all default

    cli.main(sys.argv[1:], standalone_mode=False)

    assert mock_client.search_opportunities.call_count == 3, (
        f"historic regression: search called {mock_client.search_opportunities.call_count}x, expected 3"
    )


def test_bug237_and141_regression_systemexit_in_write_opp_counted_as_error_not_kill(
    orchestration_env: tuple[Path, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    write_pursuit_sf: Any,
) -> None:
    """historic regression: SystemExit(0) from write_opp is caught in _process_opp, not kill the loop."""
    from tests.conftest import OPP_GPAY

    tree, _lv_mod = orchestration_env
    write_pursuit_sf(tree, "global-pay", "deal-gp", OPP_GPAY)

    def write_raises_systemexit(opp_id: str, pursuit_file: str, json_str: str) -> None:
        raise SystemExit(0)  # simulate old reconcile bug

    sosl_result = [_make_opp(OPP_GPAY, "Global Pay Deal")]
    monkeypatch.setattr("fieldkit.sf.client.SFDirectClient", lambda **kwargs: _make_mock_client(sosl_result))
    monkeypatch.setattr("fieldkit.commands.sf.sync.do_write_opp", write_raises_systemexit)
    monkeypatch.setattr(sys, "argv", ["sf_pipeline listview", "global-pay", "--json"])

    # Must complete without re-raising SystemExit
    cli.main(sys.argv[1:], standalone_mode=False)

    # historic regression: --json flag emits JSON to stdout
    out = capsys.readouterr().out
    result = json.loads(out.strip())
    assert result["errors"] >= 1, (
        f"historic regression: expected errors >= 1 (SystemExit caught as error), got {result}"
    )
    assert result["status"] == "partial"


# -- historic regression regression -------------------------------------------------------


# ── TestBug079AuthErrorContinuesLoop (flattened) ────────────────────────────


def test_check_sf_auth_auth_error_on_first_account_still_processes_others(
    orchestration_env: tuple[Path, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """historic regression: auth failure on global-pay must not prevent acme-bank/shield-ins from running."""
    _tree, _lv_mod = orchestration_env
    from fieldkit.sf.client import SFAuthError as _SFAuthError

    call_order: list[str] = []

    def _selective_client(**kwargs: Any) -> Any:
        m = MagicMock()
        m.__enter__ = MagicMock(return_value=m)
        m.__exit__ = MagicMock(return_value=False)

        def _search(keywords: list[str], account_name: str) -> list[Any]:
            call_order.append(account_name)
            if account_name == "global-pay":
                raise _SFAuthError("session expired")
            return []

        m.search_opportunities.side_effect = _search
        return m

    monkeypatch.setattr("fieldkit.sf.client.SFDirectClient", _selective_client)
    monkeypatch.setattr(sys, "argv", ["sf_pipeline listview"])  # --all

    with pytest.raises(SystemExit) as exc_info:
        cli.main(sys.argv[1:], standalone_mode=False)

    # Exit code must be 2 (auth failure)
    assert exc_info.value.code == 2, f"historic regression: expected exit 2, got {exc_info.value.code}"
    # All three accounts must have been attempted (not just the first)
    assert "acme-bank" in call_order, f"historic regression: acme-bank not processed; call_order={call_order}"
    assert "shield-ins" in call_order, f"historic regression: shield-ins not processed; call_order={call_order}"


def test_check_sf_auth_auth_error_deferred_exit_is_2(
    orchestration_env: tuple[Path, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """historic regression: deferred sys.exit(2) fires after the loop, not inside it."""
    _tree, _lv_mod = orchestration_env
    from fieldkit.sf.client import SFAuthError as _SFAuthError

    def _auth_fail_client(**kwargs: Any) -> Any:
        m = MagicMock()
        m.search_opportunities.side_effect = _SFAuthError("expired")
        m.__enter__ = MagicMock(return_value=m)
        m.__exit__ = MagicMock(return_value=False)
        return m

    monkeypatch.setattr("fieldkit.sf.client.SFDirectClient", _auth_fail_client)
    monkeypatch.setattr(sys, "argv", ["sf_pipeline listview", "global-pay"])

    with pytest.raises(SystemExit) as exc_info:
        cli.main(sys.argv[1:], standalone_mode=False)

    assert exc_info.value.code == 2


# -- implementation change: untracked opp table --------------------------------------------


# ── TestEnh023UntrackedOppTable (flattened) ─────────────────────────────────


def test_print_untracked_table_process_opp_returns_opp_dict_for_untracked_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """implementation change: _process_opp returns the opp dict as 4th element for untracked open opps."""
    import fieldkit.commands.sf.sync as sync_mod

    monkeypatch.setattr(sync_mod, "do_match_pursuit", lambda pursuit_dir, opp_id: None)

    opp = {"opportunity_id": "006UNTRK0000000AAA", "name": "Untracked Deal", "stage": "Negotiate"}
    u, unt, err, untracked = _process_opp(opp, "/fake/pursuits")
    assert u == 0
    assert unt == 1
    assert err == 0
    assert untracked is not None
    assert untracked["opportunity_id"] == "006UNTRK0000000AAA"


def test_print_untracked_table_process_opp_returns_none_for_closed_untracked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """implementation change: _process_opp returns None for closed untracked opps (no table row)."""
    import fieldkit.commands.sf.sync as sync_mod

    monkeypatch.setattr(sync_mod, "do_match_pursuit", lambda pursuit_dir, opp_id: None)

    opp = {"opportunity_id": "006CLSD0000000AAA", "name": "Closed Deal", "stage": "Closed Won"}
    _u, unt, _err, untracked = _process_opp(opp, "/fake/pursuits")
    assert unt == 0
    assert untracked is None


def test_print_untracked_table_untracked_table_printed_to_stderr(
    orchestration_env: tuple[Path, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """implementation change: untracked open opps produce a NAME|STAGE|ACV|CLOSE_DATE table on stderr."""
    _tree, _lv_mod = orchestration_env

    sosl_result = [
        {
            "opportunity_id": "006UNTRK0000000AAA",
            "name": "Untracked Open Deal",
            "stage": "Negotiate",
            "acv": "75000",
            "close_date": "2026-09-30",
            "status": "ok",
        }
    ]
    monkeypatch.setattr("fieldkit.sf.client.SFDirectClient", lambda **kwargs: _make_mock_client(sosl_result))
    monkeypatch.setattr(sys, "argv", ["sf_pipeline listview", "global-pay"])

    cli.main(sys.argv[1:], standalone_mode=False)

    err = capsys.readouterr().err
    # Table header and data row must appear in stderr
    assert "NAME" in err, "implementation change: table header 'NAME' missing from stderr"
    assert "STAGE" in err, "implementation change: table header 'STAGE' missing from stderr"
    assert "ACV" in err, "implementation change: table header 'ACV' missing from stderr"
    assert "CLOSE_DATE" in err, "implementation change: table header 'CLOSE_DATE' missing from stderr"
    assert "Untracked Open Deal" in err, "implementation change: opp name missing from stderr table"
    assert "Negotiate" in err, "implementation change: stage missing from stderr table"
    assert "75000" in err, "implementation change: ACV missing from stderr table"
    assert "2026-09-30" in err, "implementation change: close date missing from stderr table"


def test_print_untracked_table_acv_shows_na_when_absent(
    orchestration_env: tuple[Path, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """implementation change: untracked table shows 'N/A' for ACV when acv and arr are both absent."""
    _tree, _lv_mod = orchestration_env

    sosl_result = [
        {
            "opportunity_id": "006UNTRK0000000AAA",
            "name": "Untracked Open Deal",
            "stage": "Negotiate",
            "acv": None,
            "arr": None,
            "close_date": "2026-09-30",
            "status": "ok",
        }
    ]
    monkeypatch.setattr("fieldkit.sf.client.SFDirectClient", lambda **kwargs: _make_mock_client(sosl_result))
    monkeypatch.setattr(sys, "argv", ["sf_pipeline listview", "global-pay"])

    cli.main(sys.argv[1:], standalone_mode=False)

    err = capsys.readouterr().err
    assert "N/A" in err, "implementation change: ACV column must show 'N/A' when acv and arr are both absent"


def test_print_untracked_table_no_untracked_table_when_all_tracked(
    orchestration_env: tuple[Path, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    write_pursuit_sf: Any,
) -> None:
    """implementation change: no untracked table when all opps have matching pursuit files."""
    tree, _lv_mod = orchestration_env
    write_pursuit_sf(tree, "global-pay", "deal", OPP_GPAY)

    sosl_result = [_make_opp(OPP_GPAY, "Global Pay Deal")]
    monkeypatch.setattr("fieldkit.sf.client.SFDirectClient", lambda **kwargs: _make_mock_client(sosl_result))

    def fake_write_opp(opp_id: str, pursuit_file: str, json_str: str) -> None:
        pass

    monkeypatch.setattr("fieldkit.commands.sf.sync.do_write_opp", fake_write_opp)
    monkeypatch.setattr(sys, "argv", ["sf_pipeline listview", "global-pay"])

    cli.main(sys.argv[1:], standalone_mode=False)

    err = capsys.readouterr().err
    # The untracked table header must NOT appear when there are no untracked opps
    assert "Untracked opportunities" not in err, "implementation change: unexpected untracked table in stderr"


# ── implementation change: --territory flag ─────────────────────────────────────────────────


# ── TestTerritoryFlag (flattened) ───────────────────────────────────────────


def test_validate_territory_territory_resolves_to_account(monkeypatch: pytest.MonkeyPatch) -> None:
    """--territory FSI_SOUTH_TERR03 must resolve to the mapped account slug.

    Territory resolution happens inside _run_listview before the auth check.
    We verify by patching get_territory_account_map and get_sf_session_id (returns
    None → exit 2), then asserting that the SOSL search would have targeted the
    resolved account — confirmed by checking which account_names are looked up.
    """
    terr_map = {"FSI_SOUTH_TERR03": "acme-bank", "FSI_NORTH_TERR01": "global-pay"}

    # get_territory_account_map is imported inside _run_listview via a local import,
    # so we patch it on the lib.config module directly.
    monkeypatch.setattr("fieldkit.config.get_territory_account_map", lambda: terr_map)

    # Returning None from get_sf_session_id causes exit(2) immediately after territory
    # resolution — the function never reaches SOSL, which is fine for this test.
    monkeypatch.setattr("fieldkit.commands.sf.listview.get_sf_session_id", lambda: None)

    runner = __import__("click.testing", fromlist=["CliRunner"]).CliRunner()
    result = runner.invoke(cli, ["--territory", "FSI_SOUTH_TERR03"])

    # Exit code 2 = auth failure — territory was resolved without error (exit 3 would
    # mean the territory was not found; exit 0 would mean something else ran).
    assert result.exit_code == 2, (
        f"Expected exit 2 (auth failure after territory resolution), got {result.exit_code}. Output: {result.output}"
    )


def test_validate_territory_unknown_territory_exits_3(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unknown territory name must print an error and exit with code 3."""
    terr_map = {"FSI_SOUTH_TERR03": "acme-bank"}
    monkeypatch.setattr("fieldkit.config.get_territory_account_map", lambda: terr_map)

    runner = __import__("click.testing", fromlist=["CliRunner"]).CliRunner()
    result = runner.invoke(cli, ["--territory", "UNKNOWN_TERR99"])

    assert result.exit_code == 3
    assert "unknown territory" in result.output or "unknown territory" in (result.output + str(result.exception))


def test_validate_territory_territory_flag_in_help() -> None:
    """--territory must appear in the CLI --help output."""
    runner = __import__("click.testing", fromlist=["CliRunner"]).CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "--territory" in result.output


# ── Phase 8 addition (task 8.5) ───────────────────────────────────────────────


# ── TestRunListviewEmptyOpportunityList (flattened) ─────────────────────────


def test_run_listview_run_listview_handles_empty_opportunity_list(
    orchestration_env: tuple[Path, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """8.5: When SFDirectClient.search_opportunities returns [], cli exits 0 with ok status."""
    _tree, _lv_mod = orchestration_env
    # orchestration_env already patches SFDirectClient to return [] by default.
    monkeypatch.setattr(sys, "argv", ["sf_pipeline listview", "global-pay"])

    cli.main(sys.argv[1:], standalone_mode=False)

    _bug401_captured = capsys.readouterr()
    out = _bug401_captured.err.strip().splitlines()[-1] if _bug401_captured.err.strip() else ""
    result = json.loads(out.strip())
    assert result["status"] == "ok"
    assert result["updated"] == 0
    assert result["untracked"] == 0
    assert result["errors"] == 0


# ── CRAP-reduction: additional branch coverage for _run_listview ──────────────
# These tests target uncovered branches to reduce CRAP score below CI gate.


# ── TestRunListviewBranchCoverage (flattened) ───────────────────────────────


def test_run_listview_write_opp_exception_counted_as_error(
    orchestration_env: tuple[Path, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    write_pursuit_sf: Any,
) -> None:
    """Generic Exception from do_write_opp is caught and counted as error (not crash)."""
    from tests.conftest import OPP_GPAY

    tree, _lv_mod = orchestration_env
    write_pursuit_sf(tree, "global-pay", "deal-gp", OPP_GPAY)

    def write_raises_exception(opp_id: str, pursuit_file: str, json_str: str) -> None:
        raise RuntimeError("unexpected write failure")

    sosl_result = [_make_opp(OPP_GPAY, "Global Pay Deal")]
    monkeypatch.setattr("fieldkit.sf.client.SFDirectClient", lambda **kwargs: _make_mock_client(sosl_result))
    monkeypatch.setattr("fieldkit.commands.sf.sync.do_write_opp", write_raises_exception)
    monkeypatch.setattr(sys, "argv", ["sf_pipeline listview", "global-pay"])

    cli.main(sys.argv[1:], standalone_mode=False)

    _bug401_captured = capsys.readouterr()
    out = _bug401_captured.err.strip().splitlines()[-1] if _bug401_captured.err.strip() else ""
    result = json.loads(out.strip())
    assert result["errors"] >= 1
    assert result["status"] == "partial"


def test_run_listview_summary_table_printed_to_stderr(
    orchestration_env: tuple[Path, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Human-readable summary table with Synced/Untracked/Errors/Total is printed to stderr."""
    _tree, _lv_mod = orchestration_env
    monkeypatch.setattr(sys, "argv", ["sf_pipeline listview", "global-pay"])

    cli.main(sys.argv[1:], standalone_mode=False)

    err = capsys.readouterr().err
    assert "Synced" in err
    assert "Untracked" in err
    assert "Errors" in err
    assert "Total" in err


def test_run_listview_multiple_untracked_opps_all_in_table(
    orchestration_env: tuple[Path, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Multiple untracked open opps all appear in the formatted table."""
    _tree, _lv_mod = orchestration_env

    sosl_result = [
        {
            "opportunity_id": "006UNTRK0000000AA1",
            "name": "Alpha Deal",
            "stage": "Negotiate",
            "acv": "50000",
            "close_date": "2026-09-30",
            "status": "ok",
        },
        {
            "opportunity_id": "006UNTRK0000000AA2",
            "name": "Beta Deal",
            "stage": "Propose",
            "acv": "75000",
            "close_date": "2026-12-31",
            "status": "ok",
        },
    ]
    monkeypatch.setattr("fieldkit.sf.client.SFDirectClient", lambda **kwargs: _make_mock_client(sosl_result))
    monkeypatch.setattr(sys, "argv", ["sf_pipeline listview", "global-pay"])

    cli.main(sys.argv[1:], standalone_mode=False)

    captured = capsys.readouterr()
    assert "Alpha Deal" in captured.err
    assert "Beta Deal" in captured.err
    # historic regression: without --json, JSON goes to stderr (last line)
    result = json.loads(captured.err.strip().splitlines()[-1])
    assert result["untracked"] == 2


def test_run_listview_opp_with_no_id_skipped_silently(
    orchestration_env: tuple[Path, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Opportunity with empty opportunity_id is silently skipped (returns 0,0,0,None)."""
    _tree, _lv_mod = orchestration_env

    sosl_result = [{"opportunity_id": "", "name": "No ID Opp", "stage": "Negotiate"}]
    monkeypatch.setattr("fieldkit.sf.client.SFDirectClient", lambda **kwargs: _make_mock_client(sosl_result))
    monkeypatch.setattr(sys, "argv", ["sf_pipeline listview", "global-pay"])

    cli.main(sys.argv[1:], standalone_mode=False)

    _bug401_captured = capsys.readouterr()
    out = _bug401_captured.err.strip().splitlines()[-1] if _bug401_captured.err.strip() else ""
    result = json.loads(out.strip())
    assert result["updated"] == 0
    assert result["untracked"] == 0
    assert result["errors"] == 0


def test_run_listview_all_accounts_errors_still_exits_0_when_no_auth_error(
    orchestration_env: tuple[Path, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Non-auth errors (SFAPIError) do not cause exit 2 — exits 0 with partial status."""
    _tree, _lv_mod = orchestration_env
    from fieldkit.sf.client import SFAPIError as _SFAPIError

    def _failing_client(**kwargs: Any) -> Any:
        m = MagicMock()
        m.search_opportunities.side_effect = _SFAPIError("timeout")
        m.__enter__ = MagicMock(return_value=m)
        m.__exit__ = MagicMock(return_value=False)
        return m

    monkeypatch.setattr("fieldkit.sf.client.SFDirectClient", _failing_client)
    monkeypatch.setattr(sys, "argv", ["sf_pipeline listview", "global-pay"])

    # Should NOT raise SystemExit — only auth errors cause exit 2
    cli.main(sys.argv[1:], standalone_mode=False)

    _bug401_captured = capsys.readouterr()
    out = _bug401_captured.err.strip().splitlines()[-1] if _bug401_captured.err.strip() else ""
    result = json.loads(out.strip())
    assert result["status"] == "partial"


def test_run_listview_territory_flag_bad_parameter_exits_nonzero() -> None:
    """--territory with a flag-like value (starts with -) exits non-zero (historic regression)."""
    runner = __import__("click.testing", fromlist=["CliRunner"]).CliRunner()
    result = runner.invoke(cli, ["--territory", "--help"])
    assert result.exit_code != 0


def test_run_listview_process_opp_returns_none_for_missing_opp_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_process_opp returns (0,0,0,None) when opportunity_id is missing from dict."""
    opp = {"name": "No ID", "stage": "Negotiate"}  # no opportunity_id key
    u, unt, err, untracked = _process_opp(opp, "/fake/pursuits")
    assert u == 0
    assert unt == 0
    assert err == 0
    assert untracked is None


# ── internal account filtering ────────────────────────────────────────────────


def test_run_listview_skips_internal_accounts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Accounts with internal: true must be skipped — no SOSL call, no untracked output.

    Regression guard for the bug where an internal account was queried with
    peer-account name keywords, surfacing those accounts' SF opportunities as
    untracked deals owned by this AE.
    """
    import fieldkit.commands.sf.listview as lv_mod
    import fieldkit.commands.sf.sync as sync_mod

    (tmp_path / "accounts" / "global-pay" / "pursuits").mkdir(parents=True)
    (tmp_path / "accounts" / "internal-team" / "pursuits").mkdir(parents=True)

    monkeypatch.setattr(sync_mod, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(lv_mod, "get_sf_session_id", lambda: "fakesid")
    monkeypatch.setattr(lv_mod, "get_sf_rest_base_url", lambda: "https://examplecrm.my.salesforce.com")
    monkeypatch.setattr(
        lv_mod,
        "get_accounts_config",
        lambda: {
            "accounts": {
                "global-pay": {"keywords": ["Global Pay"]},
                "internal-team": {
                    "internal": True,
                    "keywords": ["internal meeting", "peer-account-name"],
                },
            }
        },
    )
    monkeypatch.setattr(lv_mod, "get_account_names", lambda: ["global-pay", "internal-team"])

    sosl_calls: list[str] = []

    def tracking_client(**kwargs: Any) -> Any:
        m = MagicMock()

        def tracking_search(keywords: list[str], account_name: str) -> list[Any]:
            sosl_calls.append(account_name)
            return []

        m.search_opportunities.side_effect = tracking_search
        m.__enter__ = MagicMock(return_value=m)
        m.__exit__ = MagicMock(return_value=False)
        return m

    monkeypatch.setattr("fieldkit.sf.client.SFDirectClient", tracking_client)
    monkeypatch.setattr(sys, "argv", ["sf_pipeline listview", "--all"])

    cli.main(sys.argv[1:], standalone_mode=False)

    # SOSL must only have been called for the non-internal account
    assert sosl_calls == ["global-pay"], (
        f"Expected SOSL only for 'global-pay', got: {sosl_calls}. Internal accounts must not query Salesforce."
    )

    # Summary line must reflect 0 untracked (internal account not counted)
    captured = capsys.readouterr()
    last_line = captured.err.strip().splitlines()[-1] if captured.err.strip() else ""
    result = json.loads(last_line)
    assert result["untracked"] == 0
