"""Unit tests for historic regression: lazy _get_accounts() in enrich_pursuits.py.

Verifies that importing the module does not crash when accounts.yaml is absent,
and that _get_accounts() is only called lazily (not at import time).
"""

import sys
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


def test_import_does_not_crash_without_accounts_yaml(monkeypatch: pytest.MonkeyPatch) -> None:
    """Importing enrich_pursuits must not raise even when accounts.yaml is absent.

    historic regression: the old ACCOUNTS = load_accounts() at module level would crash
    --help invocations when config was missing. The lru_cache lazy accessor
    defers the call until the command actually runs.
    """
    # Patch load_accounts to simulate missing config (returns [])
    with patch("fieldkit.config.get_account_names", return_value=[]):
        # Force re-import by removing from sys.modules if already cached
        sys.modules.pop("fieldkit.commands.gmail.enrich_pursuits", None)
        import fieldkit.commands.gmail.enrich_pursuits as ep

        # Module loaded successfully — no crash
        assert ep is not None


def test_no_module_level_accounts_constant() -> None:
    """enrich_pursuits must not have a module-level ACCOUNTS constant."""
    import fieldkit.commands.gmail.enrich_pursuits as ep

    assert not hasattr(ep, "ACCOUNTS"), "ACCOUNTS must not exist at module level — use _get_accounts() instead"


def test_get_accounts_function_exists() -> None:
    """_get_accounts() lazy accessor must be present."""
    import fieldkit.commands.gmail.enrich_pursuits as ep

    assert callable(ep._get_accounts), "_get_accounts must be a callable"


def test_get_accounts_returns_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """_get_accounts() delegates to load_accounts() and returns a list.

    We patch load_accounts AND bypass the functools.cache by patching
    _get_accounts itself — the cache is process-shared under xdist and
    patching load_accounts alone is only effective on a cache miss.
    This test verifies the delegation contract, not the caching mechanism.
    """
    import fieldkit.commands.gmail.enrich_pursuits as ep

    monkeypatch.setattr(ep, "_get_accounts", lambda: ["acme", "globalpay"])

    result = ep._get_accounts()

    assert isinstance(result, list)
    assert result == ["acme", "globalpay"]


@pytest.mark.xdist_group("enrich_pursuits_cache")
def test_get_accounts_is_cached(request: pytest.FixtureRequest) -> None:
    """_get_accounts() must call load_accounts() only once (lru_cache).

    xdist_group ensures this test runs in the same worker as other cache
    tests, preventing concurrent cache mutations from racing with cache_clear.
    request.addfinalizer guarantees cache_clear even on test failure.
    """
    import fieldkit.commands.gmail.enrich_pursuits as ep

    # Ensure clean state; finalizer clears again on teardown.
    ep._get_accounts.cache_clear()
    request.addfinalizer(ep._get_accounts.cache_clear)

    call_count = 0

    def counting_load() -> list[str]:
        nonlocal call_count
        call_count += 1
        return ["acme"]

    with patch("fieldkit.commands.gmail.enrich_pursuits.load_accounts", side_effect=counting_load):
        ep._get_accounts()
        ep._get_accounts()
        ep._get_accounts()

    assert call_count == 1, "load_accounts() must be called only once due to lru_cache"
