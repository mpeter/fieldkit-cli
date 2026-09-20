import os
import shutil
import socket
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

import fieldkit.commands.watch.backstory_health as _w_backstory
import fieldkit.commands.watch.close_date_countdown as _w_close_date_cmd
import fieldkit.commands.watch.contract_expiry as _w_contract_cmd
import fieldkit.commands.watch.pursuit_stalls as _w_pursuit_stalls
import fieldkit.commands.watch.slack_threads as _w_slack
import fieldkit.config as _config
import fieldkit.config._loader as _config_loader
import fieldkit.watch._pursuit_stall_render as _w_pursuit_stall_render
import fieldkit.watch.close_date_countdown as _w_close_date
import fieldkit.watch.draft_queue as _w_draft
import fieldkit.watch.morning_brief as _w_morning_brief
import fieldkit.watch.repair as _w_repair
import fieldkit.watch.waiting_on_tracker as _w_waiting

# Domain modules added incrementally by watch-domain-migration (implementation change).
# Each import is guarded so conftest works even when only some slices are committed.
try:
    import fieldkit.watch.contract_expiry as _w_contract  # slice 2.8
except ImportError:
    _w_contract = None  # type: ignore[assignment]
try:
    import fieldkit.watch.backstory_health as _w_backstory_domain  # slice 2.7
except ImportError:
    _w_backstory_domain = None  # type: ignore[assignment]
try:
    import fieldkit.watch.slack_threads as _w_slack_domain  # slice 2.10
except ImportError:
    _w_slack_domain = None  # type: ignore[assignment]
try:
    import fieldkit.watch.pursuit_stalls as _w_pursuit_stalls_domain  # slice 2.11
except ImportError:
    _w_pursuit_stalls_domain = None  # type: ignore[assignment]
try:
    import fieldkit.watch.morning_brief as _w_morning_brief_domain  # slice 2.9
except ImportError:
    _w_morning_brief_domain = None  # type: ignore[assignment]
try:
    import fieldkit.watch.morning_brief_collect as _w_morning_brief_collect_domain  # slice 2.9
except ImportError:
    _w_morning_brief_collect_domain = None  # type: ignore[assignment]
from fieldkit.config import clear_config_caches
from fieldkit.gmail.discover import clear_gmail_caches
from fieldkit.pursuit import clear_pursuit_caches

ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# CI config bootstrap — runs before xdist workers spawn
# ---------------------------------------------------------------------------

_CI_CONFIG_TMPDIR: str | None = None
_HARNESS_TMPDIR: str | None = None
_PRIOR_HARNESS_ROOT: str | None = None
_PRIOR_HOME: str | None = None
_PRIOR_FIELDKIT_DATA_DIR: str | None = None
_PRIOR_CONFIG_PATH: Path | None = None


def pytest_configure(config: pytest.Config) -> None:
    """Give every test run the same isolated fieldkit config as CI.

    This hook runs in the main pytest process before xdist workers are created,
    so the config file is on disk when workers import fieldkit.config._loader
    and evaluate CONFIG_PATH. Pinning HOME and FIELDKIT_DATA_DIR here ensures
    workers inherit the isolated environment instead of operator configuration.

    fieldkit_data is intentionally omitted so get_fieldkit_data() falls back
    to <fieldkit_home>/data, keeping ``p.parent.name == "data"`` valid.
    """
    global _CI_CONFIG_TMPDIR, _HARNESS_TMPDIR, _PRIOR_HARNESS_ROOT  # noqa: PLW0603
    global _PRIOR_HOME, _PRIOR_FIELDKIT_DATA_DIR, _PRIOR_CONFIG_PATH  # noqa: PLW0603
    # historic regression: driver/health worktree roots resolve from FIELDKIT_HARNESS_ROOT
    # (default ~/.cache/fieldkit). Pin it to a session tmp dir UNCONDITIONALLY —
    # even when the operator (or a parent driver run) already exported one — so
    # no test reaching _worktrees_root() or health's _cleanup_stale_worktrees()
    # can ever sweep a real cache. Stash the prior value; pytest_unconfigure
    # restores it. Set before xdist workers spawn so it reaches them via
    # inherited env; a monkeypatch would not (L02).
    _PRIOR_HARNESS_ROOT = os.environ.get("FIELDKIT_HARNESS_ROOT")
    _HARNESS_TMPDIR = tempfile.mkdtemp(prefix="fieldkit-harness-ci-")
    os.environ["FIELDKIT_HARNESS_ROOT"] = _HARNESS_TMPDIR
    _PRIOR_HOME = os.environ.get("HOME")
    _PRIOR_FIELDKIT_DATA_DIR = os.environ.get("FIELDKIT_DATA_DIR")
    _PRIOR_CONFIG_PATH = _config_loader.CONFIG_PATH
    _CI_CONFIG_TMPDIR = tempfile.mkdtemp(prefix="fieldkit-ci-")
    test_home = Path(_CI_CONFIG_TMPDIR) / "home"
    test_home.mkdir(parents=True, exist_ok=True)
    os.environ["HOME"] = str(test_home)
    os.environ.pop("FIELDKIT_DATA_DIR", None)
    standard_path = Path("~/.config/fieldkit/config.yaml").expanduser()
    _config_loader.CONFIG_PATH = standard_path
    _config.CONFIG_PATH = standard_path
    clear_config_caches()
    fieldkit_home = test_home / "fieldkit_home"
    fieldkit_home.mkdir(parents=True, exist_ok=True)
    (fieldkit_home / "data").mkdir(parents=True, exist_ok=True)
    standard_path.parent.mkdir(parents=True, exist_ok=True)
    standard_path.write_text(f"fieldkit_home: {fieldkit_home}\n", encoding="utf-8")


def pytest_unconfigure(config: pytest.Config) -> None:
    """Remove test config and restore the parent environment after the session."""
    global _CI_CONFIG_TMPDIR, _HARNESS_TMPDIR, _PRIOR_HARNESS_ROOT  # noqa: PLW0603
    global _PRIOR_HOME, _PRIOR_FIELDKIT_DATA_DIR, _PRIOR_CONFIG_PATH  # noqa: PLW0603
    if _HARNESS_TMPDIR is not None:
        if _PRIOR_HARNESS_ROOT is None:
            os.environ.pop("FIELDKIT_HARNESS_ROOT", None)
        else:
            os.environ["FIELDKIT_HARNESS_ROOT"] = _PRIOR_HARNESS_ROOT
        shutil.rmtree(_HARNESS_TMPDIR, ignore_errors=True)
        _HARNESS_TMPDIR = None
        _PRIOR_HARNESS_ROOT = None
    if _CI_CONFIG_TMPDIR is not None:
        if _PRIOR_HOME is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = _PRIOR_HOME
        if _PRIOR_FIELDKIT_DATA_DIR is None:
            os.environ.pop("FIELDKIT_DATA_DIR", None)
        else:
            os.environ["FIELDKIT_DATA_DIR"] = _PRIOR_FIELDKIT_DATA_DIR
        if _PRIOR_CONFIG_PATH is not None:
            _config_loader.CONFIG_PATH = _PRIOR_CONFIG_PATH
            _config.CONFIG_PATH = _PRIOR_CONFIG_PATH
            clear_config_caches()
        shutil.rmtree(_CI_CONFIG_TMPDIR, ignore_errors=True)
        _CI_CONFIG_TMPDIR = None
        _PRIOR_HOME = None
        _PRIOR_FIELDKIT_DATA_DIR = None
        _PRIOR_CONFIG_PATH = None


sys.path.insert(0, str(ROOT / "scripts"))


def _find_data_root() -> Path | None:
    """Locate fieldkit-data: check sibling dir, then parent umbrella submodule."""
    # Sibling: ~/work/fieldkit-data/
    sibling = ROOT.parent / "fieldkit-data"
    if (sibling / "accounts").is_dir():
        return sibling
    # Parent submodule: ~/work/fieldkit/fieldkit-data/
    parent_sub = ROOT.parent / "fieldkit" / "fieldkit-data"
    if (parent_sub / "accounts").is_dir():
        return parent_sub
    # Legacy monorepo: same dir has accounts/
    if (ROOT / "accounts").is_dir():
        return ROOT
    return None


DATA_ROOT = _find_data_root()


def _clear_watcher_dir_caches() -> None:
    """Clear per-watcher _alerts_file()/_state_file()/_accounts_dir() caches to prevent stale paths across tests.

    get_watchers_dir() itself is @_config_cache-decorated and already cleared
    by clear_config_caches() (called alongside this function) — no per-module
    handling needed for it here (implementation change).
    """
    for mod in (
        _w_backstory,
        _w_backstory_domain,
        _w_close_date,
        _w_close_date_cmd,
        _w_contract,
        _w_contract_cmd,
        _w_draft,
        _w_morning_brief,
        _w_morning_brief_domain,
        _w_morning_brief_collect_domain,
        _w_pursuit_stalls,
        _w_pursuit_stalls_domain,
        _w_pursuit_stall_render,
        _w_slack,
        _w_slack_domain,
        _w_waiting,
    ):
        if hasattr(mod, "_alerts_file"):
            mod._alerts_file.cache_clear()
        if hasattr(mod, "_state_file"):
            mod._state_file.cache_clear()
    # repair domain module uses _accounts_dir (not get_watchers_dir)
    if hasattr(_w_repair, "_accounts_dir"):
        _w_repair._accounts_dir.cache_clear()


_REAL_SOCKET_CONNECT = socket.socket.connect

#: Destinations the network guard still permits. Loopback covers tests that
#: bind a throwaway local server; everything else is an escape from the suite.
_LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", ""})


@pytest.fixture(autouse=True)
def _block_outbound_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[misc]
    """Fail loudly when a test opens a connection to anything but loopback.

    Third guard of the same family as ``_mock_write_run_status`` and
    ``_mock_watcher_logging_dir``: the suite must not touch live resources.

    The motivating bug: three ``run_opportunity`` tests in test_sf_opportunity.py
    reached the real Salesforce API through ``_fetch_contract_type``, which opens
    its own ``SFDirectClient``. It returns "standard" early only when no session
    is configured, so on CI — no credentials — the call short-circuited and the
    tests passed. On an operator's machine with a stale ``sid`` cookie they
    issued a live request and failed with HTTP 401. A test that passes or fails
    depending on whether the person running it holds credentials is not testing
    what it claims to, and it fails in the one place least able to debug it.

    Patching sockets rather than httpx is deliberate: ``httpx.MockTransport`` and
    every mocked client never reach a socket, so this fires only on genuinely
    unmocked egress. Subprocesses (``gh``, ``git``) have their own address space
    and are unaffected — this guards in-process calls only.

    Tests that legitimately need egress mark themselves ``@pytest.mark.network``.
    Nothing in the suite does today; the marker exists so that adding one is a
    visible, reviewable act rather than a silent deletion of this fixture.
    """
    if request.node.get_closest_marker("network") is not None:
        return

    def _guarded_connect(sock: socket.socket, address: object, *args: object, **kwargs: object) -> object:
        host = address[0] if isinstance(address, tuple) else address
        if isinstance(host, str) and host in _LOCAL_HOSTS:
            return _REAL_SOCKET_CONNECT(sock, address, *args, **kwargs)  # type: ignore[arg-type]
        raise RuntimeError(
            f"Test attempted an outbound network connection to {host!r}. "
            "Mock the client, or mark the test @pytest.mark.network if egress is genuinely required."
        )

    monkeypatch.setattr(socket.socket, "connect", _guarded_connect)


@pytest.fixture(autouse=True)
def _clear_config_cache() -> None:  # type: ignore[misc]
    """Clear lib.config, pursuit, and watcher-dir caches between tests for isolation."""
    clear_config_caches()
    clear_gmail_caches()
    clear_pursuit_caches()
    _clear_watcher_dir_caches()
    yield  # type: ignore[misc]
    clear_config_caches()
    clear_gmail_caches()
    clear_pursuit_caches()
    _clear_watcher_dir_caches()


@pytest.fixture(autouse=True)
def _mock_write_run_status() -> None:  # type: ignore[misc]
    """Prevent any test from writing to the live watcher-run-status.json.

    Under pytest-xdist, parallel workers share the same filesystem.  Without
    this guard, any test that exercises a watcher code-path would corrupt the
    real ``watcher-run-status.json`` in the live fieldkit-data directory.

    ``was_run_today`` is also mocked to return False so that tests are never
    skipped or short-circuited by today's real run state.

    Domain module patches are added incrementally as watch-domain-migration
    (implementation change) slices land.  Each domain patch is guarded so conftest works
    even when only some slices are committed.
    """
    import contextlib
    import importlib

    def _patch_if_importable(target: str) -> "contextlib.AbstractContextManager[object]":
        """Return a patch context manager only when the target module is importable.

        When the module does not exist yet (slice not yet committed), returns a
        no-op context manager so the autouse fixture does not fail.
        """
        parts = target.rsplit(".", 1)
        if len(parts) != 2:
            return contextlib.nullcontext()
        mod_path, _attr = parts
        try:
            importlib.import_module(mod_path)
        except ImportError:
            return contextlib.nullcontext()
        return patch(target, create=True)

    with (
        patch("fieldkit.watch.status.write_run_status"),
        patch("fieldkit.watch.status.was_run_today", return_value=False),
        patch("fieldkit.watch.status.get_fieldkit_home", return_value=Path("/tmp/fieldkit-test")),
        patch("fieldkit.watch.waiting_on_tracker.write_run_status"),
        patch("fieldkit.watch.close_date_countdown.write_run_status"),
        patch("fieldkit.watch.draft_queue.write_run_status"),
        patch("fieldkit.watch.morning_brief.write_run_status", create=True),
        _patch_if_importable("fieldkit.watch.contract_expiry.write_run_status"),
        _patch_if_importable("fieldkit.watch.backstory_health.write_run_status"),
        _patch_if_importable("fieldkit.watch.slack_threads.write_run_status"),
        _patch_if_importable("fieldkit.watch.pursuit_stalls.write_run_status"),
        _patch_if_importable("fieldkit.watch.morning_brief.write_run_status"),
    ):
        yield  # type: ignore[misc]


@pytest.fixture(autouse=True)
def _mock_watcher_logging_dir(tmp_path: Path) -> None:  # type: ignore[misc]
    """Prevent any test from writing log files to the live fieldkit-data/logs/watchers/ dir.

    historic regression: setup_watcher_logging() uses fieldkit.watch.logging.get_fieldkit_home which
    bypasses per-module patches.  This autouse fixture redirects all watcher log
    file writes to a per-test tmp_path so the live data directory is never touched.

    test_watcher_logging.py tests use monkeypatch to set the same target — monkeypatch
    overrides autouse patches within the test scope, so those tests are unaffected.
    """
    with patch("fieldkit.watch.logging.get_fieldkit_home", return_value=tmp_path):
        yield  # type: ignore[misc]


needs_data = pytest.mark.skipif(
    DATA_ROOT is None,
    reason="fieldkit-data not available (no accounts/ directory found)",
)

# Root bypasses filesystem permission bits, so any test that chmod()s a path
# read-only to force a write failure silently succeeds instead — the expected
# error never fires. CLAUDE.md: "Root-environment permission tests skip when
# os.geteuid() == 0." getattr guard keeps collection safe on platforms without
# geteuid (e.g. Windows), where the marker simply never skips.
skip_if_root = pytest.mark.skipif(
    getattr(os, "geteuid", lambda: -1)() == 0,
    reason="permission-bit test is meaningless as root (root bypasses chmod)",
)

# ── Shared pursuit test constants ─────────────────────────────────────────────
# Import these in test files instead of redefining locally.

OPP_GPAY = "006GPAY00000000AAA"
OPP_BANK = "006BANK00000000AAA"

# Richer variant (includes gate-status and meddpicc) — works for all consumers.
# Format with: MINIMAL_PURSUIT_FM.format(opp_id=opp_id)
MINIMAL_PURSUIT_FM = """\
---
sf_opportunity_id: {opp_id}
stage: discover
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


# ── Shared pursuit write fixtures ─────────────────────────────────────────────


@pytest.fixture
def write_pursuit_sf(tmp_path: Path):
    """Return a factory that writes a pursuit under tree/accounts/{account}/pursuits/.

    Signature: factory(tree, account, name, opp_id=None) -> Path
    - When opp_id is provided: writes MINIMAL_PURSUIT_FM.format(opp_id=opp_id).
    - When opp_id is None: writes a plain pursuit without sf_opportunity_id.
    """

    def _factory(tree: Path, account: str, name: str, opp_id: str | None = None) -> Path:
        fp = tree / "accounts" / account / "pursuits" / f"{name}.md"
        fp.parent.mkdir(parents=True, exist_ok=True)
        if opp_id is not None:
            fp.write_text(MINIMAL_PURSUIT_FM.format(opp_id=opp_id), encoding="utf-8")
        else:
            fp.write_text("---\ntitle: Test Pursuit\n---\n\n# Pursuit\n", encoding="utf-8")
        return fp

    return _factory


@pytest.fixture
def write_pursuit_generic(tmp_path: Path):
    """Return a factory that writes a pursuit under directory/pursuits/{name}.md.

    Signature: factory(directory, name, frontmatter) -> Path
    - Writes: ---\\n{frontmatter}---\\n\\n# Body\\n
    Covers test_pipeline_review, test_morning_brief, test_discover_contacts.
    """

    def _factory(directory: Path, name: str, frontmatter: str) -> Path:
        pursuits_dir = directory / "pursuits"
        pursuits_dir.mkdir(parents=True, exist_ok=True)
        path = pursuits_dir / f"{name}.md"
        path.write_text(f"---\n{frontmatter}---\n\n# Body\n", encoding="utf-8")
        return path

    return _factory
