import os
import shutil
import socket
import sys
import tempfile
from collections.abc import Callable, Iterator
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
import fieldkit.watch.backstory_health as _w_backstory_domain
import fieldkit.watch.close_date_countdown as _w_close_date
import fieldkit.watch.contract_expiry as _w_contract
import fieldkit.watch.draft_queue as _w_draft
import fieldkit.watch.morning_brief as _w_morning_brief
import fieldkit.watch.morning_brief_collect as _w_morning_brief_collect_domain
import fieldkit.watch.pursuit_stalls as _w_pursuit_stalls_domain
import fieldkit.watch.repair as _w_repair
import fieldkit.watch.slack_threads as _w_slack_domain
import fieldkit.watch.status as _w_status
import fieldkit.watch.waiting_on_tracker as _w_waiting
from fieldkit.config import clear_config_caches
from fieldkit.gmail.discover import clear_gmail_caches
from fieldkit.pursuit import clear_pursuit_caches
from fieldkit.watch.status import RunStatusWriteResult

ROOT = Path(__file__).resolve().parents[1]

pytest_plugins = ("tests.documentation_workflow_support",)

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
def _block_outbound_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
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
def _clear_config_cache() -> Iterator[None]:
    """Clear lib.config, pursuit, and watcher-dir caches between tests for isolation."""
    clear_config_caches()
    clear_gmail_caches()
    clear_pursuit_caches()
    _clear_watcher_dir_caches()
    yield
    clear_config_caches()
    clear_gmail_caches()
    clear_pursuit_caches()
    _clear_watcher_dir_caches()


_STATUS_WRITER_MODULES = (
    _w_status,
    _w_waiting,
    _w_close_date,
    _w_draft,
    _w_morning_brief,
    _w_contract,
    _w_backstory_domain,
    _w_slack_domain,
    _w_pursuit_stalls_domain,
)


def _discard_run_status(*args: object, **kwargs: object) -> RunStatusWriteResult:
    """Simulate a writer response without I/O; persistence tests restore the real writer."""
    return "skipped" if kwargs.get("dry_run") is True else "written"


def _never_run_today(*args: object, **kwargs: object) -> bool:
    """Keep real run history from short-circuiting test behavior."""
    return False


@pytest.fixture(autouse=True)
def _mock_write_run_status(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Isolate every watcher status alias without per-test recording mocks.

    Tests needing call assertions may override these guards locally. Missing
    modules or attributes fail collection/setup rather than bypassing isolation.
    """
    for module in _STATUS_WRITER_MODULES:
        monkeypatch.setattr(module, "write_run_status", _discard_run_status)
    monkeypatch.setattr(_w_status, "was_run_today", _never_run_today)
    monkeypatch.setattr(_w_status, "get_fieldkit_home", lambda: tmp_path)


@pytest.fixture(autouse=True)
def _mock_watcher_logging_dir(tmp_path: Path) -> Iterator[None]:
    """Prevent any test from writing log files to the live fieldkit-data/logs/watchers/ dir.

    historic regression: setup_watcher_logging() uses fieldkit.watch.logging.get_fieldkit_home which
    bypasses per-module patches.  This autouse fixture redirects all watcher log
    file writes to a per-test tmp_path so the live data directory is never touched.

    test_watcher_logging.py tests use monkeypatch to set the same target — monkeypatch
    overrides autouse patches within the test scope, so those tests are unaffected.
    """
    with patch("fieldkit.watch.logging.get_fieldkit_home", return_value=tmp_path):
        yield


@pytest.fixture
def fictional_account(tmp_path: Path) -> Path:
    """An operator-authored account record exercising the stakeholder parser."""
    account = tmp_path / "workspace" / "accounts" / "acme-corp"
    account.mkdir(parents=True)
    (account / "account.md").write_text(
        "---\nname: Acme Corp\ndomains: [example.com]\n---\n\n"
        "# Acme Corp\n\n## Stakeholder Map\n\n"
        "| Name | Title | SF Contact Role | Supplemental |\n"
        "|------|-------|-----------------|--------------|\n"
        "| Jane Example | CFO | Economic Buyer | jane@example.com |\n"
        "| Alex Example | Architect | Technical Buyer | alex@example.com |\n\n"
        "### Coverage Gaps\n\n"
        "| Role | Status | Action Needed |\n"
        "|------|--------|---------------|\n"
        "| Champion | Missing | Identify a sponsor |\n\n"
        "## Notes\n\nFictional account for contributor tests.\n",
        encoding="utf-8",
    )
    return account


@pytest.fixture
def sf_pursuit(fictional_account: Path) -> Path:
    """Hand-authored fictional Salesforce fields, including YAML-sensitive text."""
    path = fictional_account / "pursuits" / "service-expansion.md"
    path.parent.mkdir()
    path.write_text(
        "---\nstage: propose\ngate-status: pending\n"
        'sf_opportunity_id: "006EXAMPLE000001AAA"\n'
        'sf_stage: "Proposal"\n'
        'sf_close_date: "2026-12-15"\n'
        'sf_arr: "250000"\n'
        'sf_owner: "Taylor Example"\n'
        'sf_next_steps: "Review: scope #1 with sponsor"\n'
        'sf_last_pulled: "2026-09-01T10:00:00+00:00"\n'
        "---\n\n# Service Expansion\n\n"
        "Fictional scope: consulting services for Acme Corp.\n",
        encoding="utf-8",
    )
    return path


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
def write_pursuit_sf(tmp_path: Path) -> Callable[[Path, str, str, str | None], Path]:
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
def write_pursuit_generic(tmp_path: Path) -> Callable[[Path, str, str], Path]:
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
