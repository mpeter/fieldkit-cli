"""fieldkit pursuit archive — move closed/discarded pursuit files to an archive directory.

Usage:
    fieldkit pursuit archive --account <account> --name <pursuit-slug>
    fieldkit pursuit archive --account acme --name closed-deal-slug
    fieldkit pursuit archive --account acme --all-closed   # archive all closed-won/closed-lost
"""

import json
from pathlib import Path
from typing import Literal, NotRequired, TypedDict

import click

from fieldkit.cli_exit import EXIT_DATA, EXIT_PARTIAL
from fieldkit.cli_registry import declare_write
from fieldkit.config import get_fieldkit_home
from fieldkit.pursuit.io import load_pursuit
from fieldkit.pursuit.stages import CLOSED_STAGES as _CLOSED_STAGES

LOG_PREFIX = "[pursuit-archive]"


class ArchiveError(TypedDict):
    code: str
    operation: str


class ArchiveRecord(TypedDict):
    pursuit: str
    source: str
    dest: str | None
    outcome: Literal["error", "already-archived", "would-archive", "archived"]
    ok: bool
    archive_dir_created: bool
    error_code: NotRequired[str]
    operation: NotRequired[str]
    stage: NotRequired[str]


def _archive_record(
    pursuit_path: Path,
    dest: Path | None,
    outcome: Literal["error", "already-archived", "would-archive", "archived"],
    *,
    ok: bool,
    archive_dir_created: bool = False,
    error_code: str | None = None,
    operation: str | None = None,
) -> ArchiveRecord:
    record: ArchiveRecord = {
        "pursuit": pursuit_path.name,
        "source": str(pursuit_path),
        "dest": str(dest) if dest is not None else None,
        "outcome": outcome,
        "ok": ok,
        "archive_dir_created": archive_dir_created,
    }
    if error_code is not None:
        record["error_code"] = error_code
    if operation is not None:
        record["operation"] = operation
    return record


def _data_root() -> Path:
    root = get_fieldkit_home()
    if root is None:
        click.echo(f"{LOG_PREFIX} No data root configured. Run: fieldkit init", err=True)
        raise SystemExit(EXIT_DATA) from None
    return Path(root)


def _archive_one(pursuit_path: Path, dry_run: bool, quiet: bool = False) -> ArchiveRecord:
    """Move a single pursuit file to its account's archive/ directory.

    ``quiet`` suppresses the stdout narration so JSON mode owns stdout.

    Returns a record with ``ok`` False only on error; ``outcome`` is one of
    ``error``, ``already-archived``, ``would-archive``, ``archived``.
    """
    # accounts/<account>/pursuits/<slug>.md  →  accounts/<account>/archive/<slug>.md
    try:
        parts = pursuit_path.parts
        pursuits_idx = next(i for i, p in enumerate(parts) if p == "pursuits")
    except StopIteration:
        click.echo(
            f"{LOG_PREFIX} ERROR: could not determine pursuits dir for {pursuit_path}",
            err=True,
        )
        return _archive_record(pursuit_path, None, "error", ok=False)

    account_dir = Path(*parts[:pursuits_idx])
    archive_dir = account_dir / "archive"
    dest = archive_dir / pursuit_path.name
    if dest.exists():
        if not quiet:
            click.echo(
                f"{LOG_PREFIX} SKIP: {pursuit_path.name} already archived at {dest}",
            )
        return _archive_record(pursuit_path, dest, "already-archived", ok=True)

    if dry_run:
        if not quiet:
            click.echo(f"[dry-run] would move: {pursuit_path} → {dest}")
        return _archive_record(pursuit_path, dest, "would-archive", ok=True)

    archive_dir_existed = archive_dir.exists()
    try:
        archive_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return _archive_record(
            pursuit_path,
            dest,
            "error",
            ok=False,
            error_code="archive-mutation-failed",
            operation="create-destination",
        )
    try:
        pursuit_path.rename(dest)
    except OSError:
        return _archive_record(
            pursuit_path,
            dest,
            "error",
            ok=False,
            archive_dir_created=not archive_dir_existed,
            error_code="archive-mutation-failed",
            operation="move",
        )
    if not quiet:
        click.echo(f"{LOG_PREFIX} archived: {pursuit_path.name} → {dest}")
    return _archive_record(
        pursuit_path,
        dest,
        "archived",
        ok=True,
        archive_dir_created=not archive_dir_existed,
    )


@declare_write("workspace")
@click.command(name="archive")
@click.option("--account", "-a", required=True, help="Account slug (e.g. acme-corp).")
@click.option("--name", "-n", default=None, help="Pursuit slug to archive (without .md).")
@click.option(
    "--all-closed",
    "all_closed",
    is_flag=True,
    default=False,
    help="Archive all pursuits with stage closed-won, closed-lost, or won-lost.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Show what would be archived without moving files.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the archive outcome as JSON.")
def cli(account: str, name: str | None, all_closed: bool, dry_run: bool, as_json: bool) -> None:
    """Move closed or discarded pursuit files to accounts/<account>/archive/.

    Either pass --name to archive a single pursuit, or --all-closed to
    batch-archive every pursuit in the closed-won / closed-lost / won-lost stage.

    Exit codes: 0 success; 1 partial when an archive mutation fails; 3 missing
    account data or pursuit file.
    """
    if not name and not all_closed:
        click.echo(
            f"{LOG_PREFIX} ERROR: pass --name <slug> or --all-closed.",
            err=True,
        )
        raise SystemExit(EXIT_PARTIAL)

    data_root = _data_root()
    pursuits_dir = data_root / "accounts" / account / "pursuits"

    if not pursuits_dir.is_dir():
        click.echo(
            f"{LOG_PREFIX} ERROR: pursuits directory not found: {pursuits_dir}",
            err=True,
        )
        raise SystemExit(EXIT_DATA) from None

    filters = {"name": name, "all_closed": all_closed, "dry_run": dry_run}

    def _emit(
        items: list[ArchiveRecord],
        archived: int,
        skipped: int,
        errors: int,
        *,
        aborted_at: str | None = None,
        error: ArchiveError | None = None,
    ) -> None:
        click.echo(
            json.dumps(
                {
                    "account": account,
                    "items": items,
                    "count": len(items),
                    "archived": archived,
                    "skipped": skipped,
                    "errors": errors,
                    "filters": filters,
                    "outcome": "partial" if error else "ok",
                    "aborted_at": aborted_at,
                    "error": error,
                },
                indent=2,
                default=str,
            )
        )

    if name:
        # Single-pursuit mode
        slug = name.removesuffix(".md")
        target = pursuits_dir / f"{slug}.md"
        if not target.exists():
            click.echo(f"{LOG_PREFIX} ERROR: pursuit file not found: {target}", err=True)
            raise SystemExit(EXIT_DATA) from None
        record = _archive_one(target, dry_run=dry_run, quiet=as_json)
        ok = bool(record["ok"])
        if as_json:
            # The run happened — emit the record even when it failed.
            single_error: ArchiveError | None = None
            if not ok:
                single_error = {
                    "code": record.get("error_code", "archive-mutation-failed"),
                    "operation": record.get("operation", "archive"),
                }
            _emit(
                [record],
                archived=1 if ok else 0,
                skipped=0,
                errors=0 if ok else 1,
                aborted_at=record["pursuit"] if not ok else None,
                error=single_error,
            )
        raise SystemExit(0 if ok else 1)

    # --all-closed: scan and archive all closed-stage pursuits
    items: list[ArchiveRecord] = []
    archived = 0
    skipped = 0
    errors = 0
    for md_file in sorted(pursuits_dir.glob("*.md")):
        if md_file.stem == "template":
            continue
        try:
            fm, _body, _mtime = load_pursuit(md_file)
            stage = str(fm.stage or "").lower()
        except Exception:  # noqa: BLE001
            stage = ""

        if stage not in _CLOSED_STAGES:
            skipped += 1
            continue

        record = _archive_one(md_file, dry_run=dry_run, quiet=as_json)
        record["stage"] = stage
        items.append(record)
        if record["ok"]:
            archived += 1
        else:
            errors += 1
            operation = record.get("operation", "archive")
            if not as_json:
                click.echo(
                    f"{LOG_PREFIX} ERROR: {operation} failed for {record['pursuit']}; archival stopped.", err=True
                )
            break

    if as_json:
        failed = next((item for item in items if not item["ok"]), None)
        batch_error: ArchiveError | None = None
        aborted_at: str | None = None
        if failed is not None:
            batch_error = {
                "code": failed.get("error_code", "archive-mutation-failed"),
                "operation": failed.get("operation", "archive"),
            }
            aborted_at = failed["pursuit"]
        _emit(items, archived=archived, skipped=skipped, errors=errors, aborted_at=aborted_at, error=batch_error)
        raise SystemExit(0 if errors == 0 else 1)

    action = "[dry-run] would archive" if dry_run else "archived"
    status = "PARTIAL " if errors else ""
    click.echo(
        f"{LOG_PREFIX} {status}{action} {archived} pursuit(s), skipped {skipped} (not closed), {errors} error(s)."
    )
    raise SystemExit(0 if errors == 0 else 1)
