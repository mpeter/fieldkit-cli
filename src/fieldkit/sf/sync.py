"""Typed local Salesforce matching and publication, independent of CLI output."""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from fieldkit.config import get_accounts_config, get_fieldkit_data, get_fieldkit_home
from fieldkit.errors import FieldkitError, SalesforceSyncPartialError
from fieldkit.pursuit.io import read_pursuit_text_snapshot
from fieldkit.pursuit.paths import PursuitPathError, resolve_pursuit_file
from fieldkit.pursuit.validation import parse_pursuit_content
from fieldkit.sf.frontmatter import (
    SalesforceFrontmatterResult,
    parse_salesforce_frontmatter_payload,
    update_salesforce_frontmatter,
)
from fieldkit.sf.opportunities import is_opportunity_id
from fieldkit.util.atomic import atomic_text_write
from fieldkit.util.workspace_paths import resolve_workspace_output

_ACCOUNT_SLUG_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")


@dataclass(frozen=True)
class SalesforceSyncResult:
    """One validated local update and its canonical cache destination."""

    frontmatter: SalesforceFrontmatterResult
    cache: Path


@dataclass(frozen=True)
class SalesforcePursuit:
    """One confined, strictly parsed pursuit and its Salesforce identity."""

    path: Path
    opportunity_id: str | None
    stage: str


def configured_accounts() -> tuple[str, ...]:
    """Read fresh ownership configuration; never infer accounts from fixtures."""
    accounts = get_accounts_config(strict=True).get("accounts", {})
    if not isinstance(accounts, dict):
        raise FieldkitError("Salesforce accounts configuration must be a mapping")
    if any(not isinstance(name, str) or not _ACCOUNT_SLUG_RE.fullmatch(name) for name in accounts):
        raise FieldkitError("Salesforce accounts configuration contains an invalid account slug")
    return tuple(accounts)


def read_local_pursuit(path: Path, *, workspace: Path) -> SalesforcePursuit:
    """Read one pursuit only from strict frontmatter in the approved namespace."""
    try:
        target = resolve_pursuit_file(workspace, str(path))
    except PursuitPathError:
        raise FieldkitError("Salesforce pursuit is not an approved workspace location") from None
    try:
        document = parse_pursuit_content(read_pursuit_text_snapshot(target).content)
    except ValueError:
        raise FieldkitError("Salesforce pursuit is not a stable UTF-8 file") from None
    except OSError:
        raise SalesforceSyncPartialError("Salesforce local pursuit read failed") from None
    if document is None:
        raise FieldkitError("Salesforce pursuit has no frontmatter block")
    identity = document.frontmatter.get("sf_opportunity_id")
    if identity in (None, ""):
        identity = None
    elif not isinstance(identity, str) or not is_opportunity_id(identity):
        raise FieldkitError("Salesforce pursuit opportunity identity is invalid")
    stage = document.frontmatter.get("stage", "")
    if not isinstance(stage, str):
        raise FieldkitError("Salesforce pursuit stage is invalid")
    return SalesforcePursuit(target, identity, stage)


def match_pursuit(pursuit_dir: Path, opportunity_id: str, *, workspace: Path) -> Path | None:
    """Return a unique frontmatter identity match, rejecting incomplete scans."""
    if not is_opportunity_id(opportunity_id):
        raise FieldkitError("Salesforce opportunity identity is invalid")
    matches = [
        pursuit.path
        for pursuit in local_pursuits(pursuit_dir, workspace=workspace)
        if pursuit.opportunity_id == opportunity_id
    ]
    if len(matches) > 1:
        raise FieldkitError("Salesforce opportunity identity matches multiple pursuits")
    return matches[0] if matches else None


def local_pursuits(pursuit_dir: Path, *, workspace: Path) -> tuple[SalesforcePursuit, ...]:
    """Capture parsed local identities, rejecting ambiguous or incomplete scans."""
    root = workspace.resolve(strict=True)
    directory = pursuit_dir if pursuit_dir.is_absolute() else workspace / pursuit_dir
    try:
        absolute = directory.absolute()
        base = workspace.absolute() if absolute.is_relative_to(workspace.absolute()) else root
        relative = absolute.relative_to(base)
        if len(relative.parts) != 3 or relative.parts[0] != "accounts" or relative.parts[2] != "pursuits":
            raise ValueError
        directory = resolve_workspace_output(root, relative.as_posix())
    except (OSError, ValueError):
        raise FieldkitError("Salesforce pursuit directory is not an approved workspace location") from None
    if not directory.exists():
        return ()
    if not directory.is_dir():
        raise FieldkitError("Salesforce pursuit directory is not a directory")
    pursuits: list[SalesforcePursuit] = []
    identities: set[str] = set()
    try:
        paths = sorted(directory.iterdir())
    except OSError:
        raise SalesforceSyncPartialError("Salesforce local pursuit scan failed") from None
    for path in paths:
        if path.suffix != ".md":
            continue
        if path.stem in {"template", "gmail-intel"}:
            continue
        pursuit = read_local_pursuit(path, workspace=root)
        identity = pursuit.opportunity_id
        if identity is not None:
            if identity in identities:
                raise FieldkitError("Salesforce opportunity identity matches multiple pursuits")
            identities.add(identity)
        pursuits.append(pursuit)
    return tuple(pursuits)


def find_pursuit(opportunity_id: str) -> Path | None:
    """Find a unique pursuit across configured accounts under the workspace."""
    workspace = get_fieldkit_home()
    matches = [
        match
        for account in configured_accounts()
        if (match := match_pursuit(workspace / "accounts" / account / "pursuits", opportunity_id, workspace=workspace))
        is not None
    ]
    if len(matches) > 1:
        raise FieldkitError("Salesforce opportunity identity matches multiple pursuits")
    return matches[0] if matches else None


def sync_opportunity(
    opportunity_id: str, pursuit: Path, values: Mapping[str, object], *, dry_run: bool = False
) -> SalesforceSyncResult:
    """Preview or publish frontmatter, table, and cache from one typed payload."""
    if not is_opportunity_id(opportunity_id):
        raise FieldkitError("Salesforce opportunity identity is invalid")
    return _sync(
        pursuit, values, cache_relative=f"salesforce/{opportunity_id}.json", expected_id=opportunity_id, dry_run=dry_run
    )


def sync_account(account: str, values: Mapping[str, object], *, dry_run: bool = False) -> SalesforceSyncResult:
    """Preview or publish one configured account and its runtime cache."""
    if account not in configured_accounts():
        raise FieldkitError("Salesforce account is not configured")
    return _sync(
        get_fieldkit_home() / "accounts" / account / "account.md",
        values,
        cache_relative=f"salesforce/accounts/{account}.json",
        expected_id=None,
        dry_run=dry_run,
    )


def _sync(
    path: Path, values: Mapping[str, object], *, cache_relative: str, expected_id: str | None, dry_run: bool
) -> SalesforceSyncResult:
    envelope = dict(values)
    envelope.setdefault("pulled_at", datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
    payload = parse_salesforce_frontmatter_payload(envelope)
    expected_kind = "opportunity" if expected_id is not None else "account"
    if payload.record_kind != expected_kind:
        raise FieldkitError(f"Salesforce {expected_kind} identity conflicts with payload record kind")
    data = get_fieldkit_data()
    try:
        anchor = data
        while not anchor.exists():
            anchor = anchor.parent
        relative = (data / cache_relative).relative_to(anchor)
        cache = resolve_workspace_output(anchor, relative.as_posix())
        serialized = json.dumps(envelope, indent=2, allow_nan=False) + "\n"
    except (ValueError, TypeError):
        raise FieldkitError("Salesforce cache destination or payload is invalid") from None
    result = update_salesforce_frontmatter(
        path, workspace=get_fieldkit_home(), payload=payload, dry_run=dry_run, expected_opportunity_id=expected_id
    )
    if not dry_run:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            if resolve_workspace_output(data, cache_relative) != cache:
                raise ValueError
            atomic_text_write(cache, serialized)
        except (OSError, ValueError):
            raise SalesforceSyncPartialError(
                "Salesforce frontmatter was updated but cache publication failed"
            ) from None
    return SalesforceSyncResult(result, cache)
