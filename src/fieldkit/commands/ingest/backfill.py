"""backfill — scan vault meeting notes for files without provenance stamps.

Identifies files under accounts/*/meetings/*.md that lack a ``source_id``
YAML frontmatter key.  These are candidates for manual provenance assignment
before the full ingest pipeline can re-ingest them.

Usage::

    python -m fieldkit ingest backfill [--dry-run]

``--dry-run`` is accepted for interface consistency but has no effect: the
command never writes anything (this is a read-only audit tool in its current
scope).
"""

import json
from pathlib import Path
from typing import NamedTuple

import click
import yaml

from fieldkit.cli_registry import declare_write
from fieldkit.commands._account_guard import validate_account_slug
from fieldkit.config import get_fieldkit_home

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


class CandidateFile(NamedTuple):
    path: Path
    account: str
    reason: str  # human-readable explanation of why it is a candidate


# ---------------------------------------------------------------------------
# Frontmatter helpers
# ---------------------------------------------------------------------------


def _parse_frontmatter(text: str) -> dict[str, object] | None:
    """Return parsed YAML frontmatter dict, or None if absent / unparseable.

    Frontmatter is the block between the first and second ``---`` lines.
    An empty frontmatter block (``---\\n---``) returns an empty dict.
    """
    lines = text.splitlines()
    if not lines or lines[0].rstrip() != "---":
        return None

    end_idx: int | None = None
    for i, line in enumerate(lines[1:], start=1):
        if line.rstrip() == "---":
            end_idx = i
            break

    if end_idx is None:
        return None  # no closing delimiter

    frontmatter_text = "\n".join(lines[1:end_idx])
    try:
        parsed = yaml.safe_load(frontmatter_text)
    except yaml.YAMLError:
        return None

    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        return None
    return parsed


def _is_candidate(path: Path) -> str | None:
    """Return a reason string if *path* is a backfill candidate, else None."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return f"unreadable: {exc}"

    fm = _parse_frontmatter(text)
    if fm is None:
        return "no YAML frontmatter"
    if "source_id" not in fm:
        return "frontmatter present but missing source_id"
    return None  # has source_id — not a candidate


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------


def _meeting_glob(account: str | None) -> str:
    """Glob for meeting notes — every account, or one.

    Single definition so the scan and the scanned-file count cannot diverge;
    they are globbed separately and the summary compares the two.
    """
    return "*/meetings/*.md" if account is None else f"{account}/meetings/*.md"


def scan_vault(accounts_root: Path, account: str | None = None) -> list[CandidateFile]:
    """Scan accounts_root for meeting notes without provenance stamps.

    Args:
        accounts_root: The ``accounts/`` directory.
        account: Restrict the scan to one account slug. Validated at the CLI
            boundary, so an unknown slug cannot reach here.
    """
    candidates: list[CandidateFile] = []

    for meeting_file in sorted(accounts_root.glob(_meeting_glob(account))):
        # Skip .template and any other dot-directories
        parts = meeting_file.relative_to(accounts_root).parts
        if any(p.startswith(".") for p in parts):
            continue

        account = parts[0]
        reason = _is_candidate(meeting_file)
        if reason is not None:
            candidates.append(CandidateFile(path=meeting_file, account=account, reason=reason))

    return candidates


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def _run_backfill(*, dry_run: bool, account: str | None = None, as_json: bool = False) -> int:
    """Scan vault for meeting notes without provenance stamps. Returns exit code."""
    data_root = get_fieldkit_home()
    accounts_root = data_root / "accounts"

    if not accounts_root.is_dir():
        click.echo(f"ERROR: accounts directory not found: {accounts_root}", err=True)
        return 1

    if dry_run and not as_json:
        click.echo("[dry-run] scanning vault for pre-pipeline meeting notes …")

    # Collect all meeting files for the scan count
    all_meeting_files: list[Path] = []
    for f in sorted(accounts_root.glob(_meeting_glob(account))):
        parts = f.relative_to(accounts_root).parts
        if not any(p.startswith(".") for p in parts):
            all_meeting_files.append(f)

    candidates = scan_vault(accounts_root, account)

    if as_json:
        items = [{"path": str(c.path), "account": c.account, "reason": c.reason} for c in candidates]
        click.echo(
            json.dumps(
                {
                    "items": items,
                    "count": len(items),
                    "scanned": len(all_meeting_files),
                    "next_step": "fieldkit ingest run --pipeline transcript-ingest",
                    "filters": {"account": account, "dry_run": dry_run},
                },
                indent=2,
                default=str,
            )
        )
        return 0

    # Output one block per candidate: path/reason line + actionable next-step hint.
    # implementation change: without the hint the report is a dead end — operators must already
    # know the separate command to add provenance stamps.  The hint is assembled
    # from the pipeline ID so it stays in sync if the registry changes.
    for c in candidates:
        click.echo(f"{c.path}  [{c.account}]  reason: {c.reason}")
        click.echo("  → fieldkit ingest run --pipeline transcript-ingest")

    # Summary — include next-step guidance when there are candidates to fix.
    n_scanned = len(all_meeting_files)
    n_candidates = len(candidates)
    if n_candidates > 0:
        click.echo(
            f"\n{n_scanned} files scanned, {n_candidates} candidate(s) found"
            f" — run the command shown above each file to assign provenance"
        )
        # historic regression: explain the pipeline.db vs filesystem gap so operators
        # understand why 'ingest run --dry-run' may show 0 pending sources.
        click.echo(
            "\nNOTE: 'ingest run' only processes sources registered in pipeline.db.\n"
            "      Run 'fieldkit ingest route' to assign provenance, then\n"
            "      'fieldkit ingest discover' to register sources before running 'fieldkit ingest run'."
        )
    else:
        click.echo(f"\n{n_scanned} files scanned, 0 candidates found")

    return 0


# ---------------------------------------------------------------------------
# Click command
# ---------------------------------------------------------------------------


@declare_write("read-only")
@click.command("backfill")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Accepted for interface consistency; has no effect (command is always read-only).",
)
@click.option(
    "--account",
    "-a",
    default=None,
    metavar="SLUG",
    help="Limit the scan to a single account slug. Default: all accounts.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the candidate list as JSON.")
def cli(dry_run: bool, account: str | None, as_json: bool) -> None:
    """Scan vault meeting notes for files without provenance stamps (source_id).

    This command is read-only — it never writes to any file.

    Exit codes: 0 success; 1 accounts directory missing; 3 unknown account slug.
    """
    validate_account_slug(account)
    rc = _run_backfill(dry_run=dry_run, account=account, as_json=as_json)
    if rc:
        raise SystemExit(rc)


def main(argv: list[str] | None = None) -> int:
    """Invoke the CLI command in-process; writes to real stdout. Returns exit code."""
    try:
        cli.main(argv or [], standalone_mode=False)
        return 0
    except click.exceptions.Exit as exc:
        return int(exc.exit_code) if exc.exit_code is not None else 0
    except click.exceptions.UsageError as exc:
        click.echo(f"Error: {exc.format_message()}", err=True)
        return 2
    except SystemExit as exc:
        return int(exc.code) if exc.code is not None else 0
