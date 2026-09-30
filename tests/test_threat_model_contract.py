"""Bind public threat-model language to observable local security boundaries."""

import hashlib
import json
import re
import shlex
from pathlib import Path
from typing import Literal

import pytest

from fieldkit.cli_exit import EXIT_DATA, handle_cli_exception
from fieldkit.util.atomic import PathLockTimeoutError, exclusive_file_lock, prepare_runtime_lock_path
from fieldkit.web.server import is_loopback_bind_host
from scripts.documentation_commands import DOCUMENT_COMMANDS, EXAMPLE_COMMANDS
from scripts.markdown_tables import parse_markdown_tables

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "THREAT_MODEL.md"

REPLAY_CLAIMS: tuple[str, ...] = (
    "- Prepared ingest checkpoints retain rendered notes, action items, and task\n  classifications until completion. Treat checkpoints and backups as sensitive\n  content. Ownership comments in notes, pursuits, and tasks contain integrity\n  hashes, not encryption or authentication.",
    "- Ingest revalidates saved checkpoints and existing Markdown before replay.\n  Configured workspace and runtime roots may be symlink aliases resolved to\n  canonical roots; replay rejects descendant redirects. File locks coordinate\n  cooperating fieldkit writers, not arbitrary processes editing the same files.",
    "- T3: user-derived path data can escape an approved root. Resolve and validate\n  paths before writes. Ingest replay accepts canonicalized root aliases but\n  rejects child redirects, with publication-time rechecks. Hashed replay lock\n  names remain beneath the canonical runtime root.",
    "- T6: interruption can leave only some ingest file effects published. Corrupt\n  checkpoints or edited, copied, or malformed ownership comments could suppress,\n  duplicate, or redirect later effects. Replay uses bounded strict checkpoint\n  decoding, rejects duplicate JSON keys, and derives a canonical intent digest.\n  Source and task-position identities bind decision fingerprints; structural\n  Markdown validation rejects conflicting, duplicate, or unmarked ownership.\n  Target-specific bounded locks, stable snapshots, and atomic create or replace\n  protect individual writes. Database completion occurs only after all effects\n  succeed; recovery is replay, not rollback or a cross-filesystem transaction.",
    "- A hostile process running as the same OS user can forge unkeyed ownership\n  comments or race ancestor-directory renames. Protection against that principal\n  is out of scope; replay assumes stable configured directory namespaces.",
    "- Process-interruption recovery does not promise power-loss durability.",
    "- Preserve prepared checkpoints and ownership comments during recovery. Reconcile\n  conflicts using the [pipeline recovery guide](docs/guides/pipeline-workflow.md)\n  instead of deleting markers or lock files. Keep configured directory namespaces\n  stable while writes are active.",
)
REPLAY_SUITES = (
    "tests/test_ingest_paths.py",
    "tests/test_ingest_prepared.py",
    "tests/test_ingest_prepared_store.py",
    "tests/test_pursuit_effects.py",
    "tests/test_task_effects.py",
    "tests/test_owned_markdown.py",
    "tests/test_text_snapshot.py",
    "tests/test_atomic_text_create.py",
)


def assert_replay_claims(document: str) -> tuple[str, ...]:
    """Require the reviewed controls and their explicit recovery limitations."""
    prefixes = (
        "- Prepared ingest",
        "- Ingest revalidates",
        "- T3:",
        "- T6:",
        "- A hostile process",
        "- Process-interruption",
        "- Preserve prepared",
    )
    blocks = tuple(
        block for block in re.findall(r"(?m)^- [^\n]+(?:\n  [^\n]+)*", document) if block.startswith(prefixes)
    )
    assert blocks == REPLAY_CLAIMS, "threat-model replay claim inventory changed"
    return blocks


def test_ingest_replay_claims_include_controls_and_limits() -> None:
    result = assert_replay_claims(PAGE.read_text(encoding="utf-8"))
    assert result == REPLAY_CLAIMS


@pytest.mark.parametrize("index", range(len(REPLAY_CLAIMS)))
@pytest.mark.parametrize("mutation", ("remove", "alter", "negate", "duplicate", "reorder"))
def test_each_replay_claim_mutation_is_rejected(index: int, mutation: str) -> None:
    """Changing a control or its qualification requires renewed ownership review."""
    document = PAGE.read_text(encoding="utf-8")
    claim = REPLAY_CLAIMS[index]
    if mutation == "remove":
        document = document.replace(claim, "", 1)
    elif mutation == "alter":
        document = document.replace(claim, claim + " Recovery always succeeds.", 1)
    elif mutation == "negate":
        document = document.replace(claim, "Not " + claim, 1)
    elif mutation == "duplicate":
        document = document.replace(claim, claim + "\n" + claim, 1)
    else:
        other = REPLAY_CLAIMS[(index + 1) % len(REPLAY_CLAIMS)]
        document = document.replace(claim, "REPLAY_CLAIM_SWAP", 1).replace(other, claim, 1)
        document = document.replace("REPLAY_CLAIM_SWAP", other, 1)
    with pytest.raises(AssertionError, match="replay claim inventory"):
        assert_replay_claims(document)


@pytest.mark.parametrize("namespace", ("meeting-note", "pursuit", "tasks"))
def test_replay_lock_alias_has_one_contained_identity(
    tmp_path: Path, namespace: Literal["meeting-note", "pursuit", "tasks"]
) -> None:
    """Canonical runtime aliases share the same target-specific bounded lock."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(runtime, target_is_directory=True)
    target = tmp_path / "workspace" / "fictional.md"
    lock = prepare_runtime_lock_path(target, alias, namespace)
    digest = hashlib.sha256(str(target.resolve()).encode("utf-8")).hexdigest()
    assert lock == runtime / "locks" / namespace / f"{digest}.lock"
    assert prepare_runtime_lock_path(target, runtime, namespace) == lock
    with (
        exclusive_file_lock(lock, timeout_seconds=0),
        pytest.raises(PathLockTimeoutError, match="Timed out acquiring path lock"),
        exclusive_file_lock(lock, timeout_seconds=0),
    ):
        pytest.fail("A cooperating replay writer bypassed the held lock")
    with exclusive_file_lock(lock, timeout_seconds=0):
        assert lock.is_file()
    assert not target.exists()


@pytest.mark.parametrize("redirect", ("locks", "locks/meeting-note"))
def test_replay_runtime_lock_rejects_descendant_redirect(tmp_path: Path, redirect: str) -> None:
    """A child redirect must fail before a lock can be published outside runtime."""
    runtime = tmp_path / "runtime"
    outside = tmp_path / "outside"
    outside.mkdir()
    redirected = runtime / redirect
    redirected.parent.mkdir(parents=True)
    redirected.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="Runtime lock directory redirects"):
        prepare_runtime_lock_path(tmp_path / "fictional.md", runtime, "meeting-note")
    assert list(outside.iterdir()) == []


def test_dashboard_boundary_names_local_clients_and_auth_modes() -> None:
    document = PAGE.read_text(encoding="utf-8")

    assert "any local HTTP client, not only a browser" in document
    assert "Without a token, it rejects foreign Host\n  headers and write requests" in document
    assert "with a token, API and event requests require the\n  bearer credential" in document
    assert "not a multi-user authorization\n  boundary" in document
    assert is_loopback_bind_host("127.0.0.1")
    assert is_loopback_bind_host("::1")
    assert not is_loopback_bind_host("0.0.0.0")


def test_diagnostic_warning_matches_unexpected_error_boundary(capsys: pytest.CaptureFixture[str]) -> None:
    document = PAGE.read_text(encoding="utf-8")
    marker = "fictional diagnostic marker"

    assert "do not deliberately include them in diagnostics" in document
    assert "exception text and tracebacks can still disclose sensitive values" in " ".join(document.split())
    assert handle_cli_exception(RuntimeError(marker)) == EXIT_DATA
    assert marker in capsys.readouterr().err


def test_threat_table_has_one_row_for_all_named_threats() -> None:
    document = PAGE.read_text(encoding="utf-8")
    tables = parse_markdown_tables(document)

    assert len(tables) == 1
    assert tables[0].header == ("Threat group", "Required control")
    assert tables[0].rows == (
        ("T1\u2013T6", "Apply the matching control above before enabling or releasing the affected capability."),
    )
    for number in range(1, 7):
        assert f"- T{number}:" in document


def test_threat_page_and_table_have_fixed_owners() -> None:
    contract = json.loads((ROOT / "docs/documentation-contract.json").read_text(encoding="utf-8"))
    command = (
        "uv",
        "run",
        "pytest",
        "tests/test_threat_model_contract.py",
        *REPLAY_SUITES,
        "tests/test_web_server.py::test_token_guard_rejects_missing_bearer",
        "tests/test_web_server.py::test_tokenless_mode_rejects_foreign_host_header",
        "tests/test_web_server.py::test_tokenless_create_rejects_before_body_validation",
        "tests/test_web_server.py::test_serve_refuses_non_loopback_bind_even_with_token",
        "tests/test_cli_exit.py::test_cli_main_generic_exception_is_exit_3",
        "-q",
        "-n",
        "0",
    )

    assert contract["verification"]["threat_model_contract"]["paths"] == ["THREAT_MODEL.md"]
    assert DOCUMENT_COMMANDS["threat_model_contract"] == (command,)
    assert contract["verification"]["threat_model_contract"]["evidence"] == shlex.join(command)
    assert set(REPLAY_SUITES).issubset(contract["documents"]["THREAT_MODEL.md"]["sources"])
    table = contract["documents"]["THREAT_MODEL.md"]["tables"]
    assert len(table) == 1
    assert table[0]["verification_id"] == "automated.threat-model-contract"
    assert EXAMPLE_COMMANDS["automated.threat-model-contract"] == (command,)
    assert contract["example_verifications"]["automated.threat-model-contract"]["evidence"] == shlex.join(command)
