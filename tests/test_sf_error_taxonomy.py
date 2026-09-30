"""Adapter regressions for strict configuration and retryable local failures."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from fieldkit.__main__ import main
from fieldkit.commands.sf import account, listview, opportunity
from fieldkit.config import ConfigError
from fieldkit.errors import FieldkitError, SalesforceSyncPartialError

pytestmark = pytest.mark.unit
_PRIVATE_DIAGNOSTIC = "private-path-must-not-be-printed"


@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize(
    ("failure", "expected"),
    [(ConfigError, 3), (SalesforceSyncPartialError, 1), (OSError, 1), (FieldkitError, 3)],
)
def test_account_lookup_preserves_failure_taxonomy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    as_json: bool,
    dry_run: bool,
    failure: type[Exception],
    expected: int,
) -> None:
    monkeypatch.setattr("fieldkit.__main__.load_dotenv_safe", lambda: None)
    monkeypatch.setattr(account, "get_account_names", lambda: ["acme-corp"])
    monkeypatch.setattr(account, "get_accounts_config", lambda **kwargs: {"accounts": {"acme-corp": {}}})
    monkeypatch.setattr(account, "get_sf_session_id", lambda: "synthetic-session")
    monkeypatch.setattr(account, "get_sf_rest_base_url", lambda: "https://crm.example.com")
    monkeypatch.setattr(account, "get_fieldkit_home", lambda: tmp_path)
    monkeypatch.setattr("fieldkit.commands.sf.account._sf_direct.SFDirectClient", MagicMock())
    resolve = MagicMock(side_effect=failure(_PRIVATE_DIAGNOSTIC))
    monkeypatch.setattr(account, "_resolve_sf_account_id", resolve)
    write = MagicMock(side_effect=AssertionError("unexpected update"))
    monkeypatch.setattr(account, "sync_account", write)

    result = main(["sf", "account", "acme-corp", *(["--json"] if as_json else []), *(["--dry-run"] if dry_run else [])])

    assert result == expected
    resolve.assert_called_once()
    write.assert_not_called()
    output = capsys.readouterr()
    assert _PRIVATE_DIAGNOSTIC not in output.out + output.err


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize(("failure", "expected"), [(ConfigError, 3), (SalesforceSyncPartialError, 1), (OSError, 1)])
def test_opportunity_lookup_preserves_failure_taxonomy(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    dry_run: bool,
    failure: type[Exception],
    expected: int,
) -> None:
    monkeypatch.setattr("fieldkit.__main__.load_dotenv_safe", lambda: None)
    monkeypatch.setattr(opportunity, "_fetch_opportunity", lambda identifier: {})
    monkeypatch.setattr(opportunity, "_fetch_deal_splits", lambda identifier: [])
    monkeypatch.setattr(opportunity, "_resolve_pursuit_file", MagicMock(side_effect=failure(_PRIVATE_DIAGNOSTIC)))
    write = MagicMock(side_effect=AssertionError("unexpected update"))
    monkeypatch.setattr(opportunity, "sync_opportunity", write)

    result = main(["sf", "opportunity", "006000000000AAA", *(["--dry-run"] if dry_run else [])])

    assert result == expected
    write.assert_not_called()
    output = capsys.readouterr()
    assert _PRIVATE_DIAGNOSTIC not in output.out + output.err


@pytest.mark.parametrize("write", [False, True])
@pytest.mark.parametrize("remote", [{}, {"opportunity_id": ""}])
def test_listview_missing_remote_identity_is_an_explicit_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], write: bool, remote: dict[str, str]
) -> None:
    match = MagicMock(side_effect=AssertionError("unexpected matching"))
    update = MagicMock(side_effect=AssertionError("unexpected update"))
    monkeypatch.setattr(listview, "match_pursuit", match)
    monkeypatch.setattr(listview, "sync_opportunity", update)

    result = listview._process_opp(remote, "accounts/acme-corp/pursuits", write=write)

    assert result == (0, 0, 1, None)
    match.assert_not_called()
    update.assert_not_called()
    assert "opportunity identity" in capsys.readouterr().err.lower()
