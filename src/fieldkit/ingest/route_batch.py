"""Per-item routing and mutation outcomes for ``fieldkit ingest route``."""

from pathlib import Path
from typing import Any, Literal, Protocol, TypedDict

import click
import yaml


class SplitFrontmatter(Protocol):
    def __call__(self, text: str) -> tuple[str, str] | None: ...


class WriteFrontmatter(Protocol):
    def __call__(self, path: str | Path, fm: dict[str, Any], body: str, *, create: bool) -> None: ...


# ---------------------------------------------------------------------------
# historic regression: Account name aliases for prefix matching
# Maps filename token aliases → canonical account slug prefix.
# Applied before prefix matching so "b-of-a" routes to the account whose
# slug starts with "<account-slug>" (e.g. the Bank of America account).
# Add entries here for any account with a non-obvious filename abbreviation.
# ---------------------------------------------------------------------------
_ACCOUNT_NAME_ALIASES: dict[str, str] = {
    "b-of-a": "acme-corp",  # pii-guard: ignore
    "bank-of-america": "acme-corp",  # pii-guard: ignore
}


class RouteItem(TypedDict):
    item: str
    outcome: Literal["moved", "would-move", "ambiguous", "unmatched", "skipped", "error"]
    ok: bool
    frontmatter_updated: bool
    move_completed: bool


class RouteError(TypedDict):
    code: str
    operation: str


class RouteMutationError(OSError):
    def __init__(self, item: str, operation: str, *, frontmatter_updated: bool) -> None:
        super().__init__(f"{operation} failed for {item}")
        self.item = item
        self.operation = operation
        self.frontmatter_updated = frontmatter_updated


def _route_item(
    fpath: Path,
    outcome: Literal["moved", "would-move", "ambiguous", "unmatched", "skipped", "error"],
    *,
    ok: bool = True,
    frontmatter_updated: bool = False,
    move_completed: bool = False,
) -> RouteItem:
    return {
        "item": fpath.name,
        "outcome": outcome,
        "ok": ok,
        "frontmatter_updated": frontmatter_updated,
        "move_completed": move_completed,
    }


def _apply_account_aliases(stem: str) -> str:
    """Replace known account name aliases in a filename stem.

    Applied before prefix matching so abbreviated account names in filenames
    (e.g. 'b-of-a') are normalised to the canonical account slug prefix.
    """
    lower = stem.lower()
    for alias, canonical in _ACCOUNT_NAME_ALIASES.items():
        if lower.startswith(alias + "-") or lower.startswith(alias + "_"):
            return canonical + stem[len(alias) :]
    return stem


def _load_frontmatter(path: Path, split_frontmatter: SplitFrontmatter) -> tuple[dict[str, Any], str] | None:
    """Parse frontmatter from a file.

    Returns (fm_dict, full_text) or None if no frontmatter found.
    """
    text = path.read_text(encoding="utf-8")
    split = split_frontmatter(text)
    if not split:
        return None
    fm_text, _ = split
    try:
        fm: dict[str, Any] = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError as exc:
        click.echo(f"  YAML error in {path.name}: {exc}", err=True)
        return None
    return fm, text


def _match_by_prefix(stem: str, account_names: list[str]) -> str | None:
    """Return the first account name whose prefix matches the file stem, or None.

    Applies _ACCOUNT_NAME_ALIASES first (historic regression) so abbreviated account names
    in filenames (e.g. 'b-of-a') are normalised before comparison.
    """
    normalised = _apply_account_aliases(stem)
    for acct in account_names:
        if normalised.startswith(f"{acct}-") or normalised.startswith(f"{acct}_"):
            return acct
    return None


def _match_by_title(
    fpath: Path,
    fm: dict[str, Any],
    text: str,
    data_root: Path,
    *,
    dry_run: bool,
    counters: dict[str, int],
    split_frontmatter: SplitFrontmatter,
    write_frontmatter: WriteFrontmatter,
    quiet: bool = False,
) -> str | None:
    """Attempt title-keyword routing. Updates counters in-place for ambiguous matches.

    Returns matched account name, or None if no match or ambiguous.
    """
    from fieldkit.ingest.router import route_by_title

    title = str(fm.get("title") or fm.get("meeting_title") or "")
    if not title:
        return None

    try:
        route_result = route_by_title(title, data_root=data_root)
    except (OSError, ValueError, KeyError) as exc:
        click.echo(f"  WARN: routing error for {fpath.name}: {exc}", err=True)
        return None

    if route_result.accounts and len(route_result.accounts) == 1:
        return route_result.accounts[0]

    if route_result.accounts and len(route_result.accounts) > 1:
        if dry_run:
            if not quiet:
                click.echo(f"  [DRY-RUN] AMBIGUOUS {fpath.name} → {route_result.accounts}")
        else:
            fm["ambiguous-match"] = route_result.accounts
            _, tail = split_frontmatter(text) or ("", "")
            # Note: expected_mtime not captured — single-user tool, concurrent write risk accepted
            try:
                write_frontmatter(fpath, fm, "\n" + tail, create=False)
            except OSError:
                raise RouteMutationError(fpath.name, "update-frontmatter", frontmatter_updated=False) from None
            if not quiet:
                click.echo(f"  AMBIGUOUS {fpath.name} → tagged {route_result.accounts}")
        counters["ambiguous"] += 1

    return None


def _handle_matched(
    fpath: Path,
    fm: dict[str, Any],
    text: str,
    matched_account: str,
    data_root: Path,
    *,
    dry_run: bool,
    counters: dict[str, int],
    split_frontmatter: SplitFrontmatter,
    write_frontmatter: WriteFrontmatter,
    quiet: bool = False,
) -> None:
    """Move file to matched account directory, updating frontmatter."""
    dest_dir = data_root / "accounts" / matched_account / "meetings"
    dest = dest_dir / fpath.name
    if dry_run:
        if not quiet:
            click.echo(f"  [DRY-RUN] MOVE {fpath.name} → accounts/{matched_account}/meetings/")
    else:
        fm["account"] = matched_account
        _, tail = split_frontmatter(text) or ("", "")
        # Note: expected_mtime not captured — single-user tool, concurrent write risk accepted
        try:
            write_frontmatter(fpath, fm, "\n" + tail, create=False)
        except OSError:
            raise RouteMutationError(fpath.name, "update-frontmatter", frontmatter_updated=False) from None
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise RouteMutationError(fpath.name, "create-destination", frontmatter_updated=True) from None
        try:
            fpath.rename(dest)
        except OSError:
            raise RouteMutationError(fpath.name, "move", frontmatter_updated=True) from None
        if not quiet:
            click.echo(f"  MOVED {fpath.name} → accounts/{matched_account}/meetings/")
    counters["moved"] += 1


def _handle_unmatched(
    fpath: Path,
    fm: dict[str, Any],
    text: str,
    *,
    dry_run: bool,
    counters: dict[str, int],
    split_frontmatter: SplitFrontmatter,
    write_frontmatter: WriteFrontmatter,
    quiet: bool = False,
) -> None:
    """Mark file as reviewed-unmatched."""
    if dry_run:
        if not quiet:
            click.echo(f"  [DRY-RUN] UNMATCHED {fpath.name}")
    else:
        fm["reviewed-unmatched"] = True
        _, tail = split_frontmatter(text) or ("", "")
        # Note: expected_mtime not captured — single-user tool, concurrent write risk accepted
        try:
            write_frontmatter(fpath, fm, "\n" + tail, create=False)
        except OSError:
            raise RouteMutationError(fpath.name, "update-frontmatter", frontmatter_updated=False) from None
    counters["unmatched"] += 1


def _route_one_file(
    fpath: Path,
    account_names: list[str],
    data_root: Path,
    *,
    dry_run: bool,
    counters: dict[str, int],
    split_frontmatter: SplitFrontmatter,
    write_frontmatter: WriteFrontmatter,
    quiet: bool = False,
) -> RouteItem:
    """Route a single meeting file to the correct account directory."""
    result = _load_frontmatter(fpath, split_frontmatter)
    if result is None:
        click.echo(f"  WARN: no frontmatter in {fpath.name}, skipping.", err=True)
        counters["skipped"] += 1
        return _route_item(fpath, "skipped")

    fm, text = result

    # Idempotency guards
    if fm.get("reviewed-unmatched") is True or fm.get("ambiguous-match"):
        counters["skipped"] += 1
        return _route_item(fpath, "skipped")

    # 1. Filename-prefix match
    matched_account = _match_by_prefix(fpath.stem, account_names)

    # 2. Title-keyword fallback (may set ambiguous and return None)
    if matched_account is None:
        ambiguous_before = counters["ambiguous"]
        matched_account = _match_by_title(
            fpath,
            fm,
            text,
            data_root,
            dry_run=dry_run,
            counters=counters,
            split_frontmatter=split_frontmatter,
            write_frontmatter=write_frontmatter,
            quiet=quiet,
        )
        if counters["ambiguous"] > ambiguous_before:
            return _route_item(fpath, "ambiguous", frontmatter_updated=not dry_run)

    if matched_account is not None and matched_account != "unknown":
        _handle_matched(
            fpath,
            fm,
            text,
            matched_account,
            data_root,
            dry_run=dry_run,
            counters=counters,
            split_frontmatter=split_frontmatter,
            write_frontmatter=write_frontmatter,
            quiet=quiet,
        )
        return _route_item(
            fpath,
            "would-move" if dry_run else "moved",
            frontmatter_updated=not dry_run,
            move_completed=not dry_run,
        )
    else:
        _handle_unmatched(
            fpath,
            fm,
            text,
            dry_run=dry_run,
            counters=counters,
            split_frontmatter=split_frontmatter,
            write_frontmatter=write_frontmatter,
            quiet=quiet,
        )
        return _route_item(fpath, "unmatched", frontmatter_updated=not dry_run)


def _force_one_file(
    fpath: Path,
    account: str,
    data_root: Path,
    *,
    dry_run: bool,
    counters: dict[str, int],
    split_frontmatter: SplitFrontmatter,
    write_frontmatter: WriteFrontmatter,
    quiet: bool = False,
) -> int:
    """Assign *fpath* to *account* by operator decision, bypassing the matchers.

    The idempotency guards (``reviewed-unmatched`` / ``ambiguous-match``) are
    deliberately ignored here. They exist to stop the automatic matchers
    re-examining files they have already failed on — which is exactly the set a
    manual force needs to reach. Honouring them would make this command a no-op
    on the only files anyone would ever run it against.

    Both guard keys are cleared on the way out: leaving ``ambiguous-match`` on a
    file that now has a decided account would keep reporting an open question
    that has been answered.

    Returns an exit code: 0 on success, 3 when the file has no frontmatter.
    """
    result = _load_frontmatter(fpath, split_frontmatter)
    if result is None:
        click.echo(f"Error: no frontmatter in {fpath.name}; cannot set an account.", err=True)
        return 3

    fm, text = result
    was_guarded = fm.get("reviewed-unmatched") is True or bool(fm.get("ambiguous-match"))

    if dry_run:
        if not quiet:
            note = " (clearing prior unmatched/ambiguous marker)" if was_guarded else ""
            click.echo(f"  [DRY-RUN] FORCE {fpath.name} → accounts/{account}/meetings/{note}")
        counters["moved"] += 1
        return 0

    fm.pop("reviewed-unmatched", None)
    fm.pop("ambiguous-match", None)
    fm["account"] = account
    _, tail = split_frontmatter(text) or ("", "")
    # create=True, deliberately. The create=False path rebuilds frontmatter from
    # the keys found on disk (`fm.get(key, raw_on_disk[key])`), so a key removed
    # from `fm` is restored from the file and the pops above become silent
    # no-ops — deletion is impossible through that path. create=True writes
    # exactly `fm`, in dict order, and `fm` was parsed from this file, so the
    # surviving keys keep their on-disk order.
    write_frontmatter(fpath, fm, "\n" + tail, create=True)

    dest_dir = data_root / "accounts" / account / "meetings"
    dest_dir.mkdir(parents=True, exist_ok=True)
    fpath.rename(dest_dir / fpath.name)
    if not quiet:
        click.echo(f"  FORCED {fpath.name} → accounts/{account}/meetings/")
    counters["moved"] += 1
    return 0
