"""Resolve and freeze driver prompt sources at the revision they will execute."""

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from fieldkit.config._timeouts import TIMEOUT_PROCESS_KILL_GRACE
from fieldkit.driver.done_checks import DoneCheckError, parse_done_checks
from fieldkit.driver.github import AgentIssue
from fieldkit.driver.prompt_contract import (
    PromptContract,
    PromptContractError,
    PromptKind,
    parse_prompt_contract,
    validate_edit_sites,
)
from fieldkit.errors import FieldkitError
from fieldkit.util.bounded_process import (
    BoundedProcessBytesResult,
    BoundedProcessError,
    run_bounded_process_bytes,
)
from fieldkit.util.text_snapshot import read_text_snapshot

_GIT_TIMEOUT = 30
_MAX_PROMPT_BYTES = 1 * 1024 * 1024
_MAX_TARGET_BYTES = 4 * 1024 * 1024
_MAX_GIT_METADATA_BYTES = 64 * 1024
_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
_WORK_ORDER_RE = re.compile(r"work\s*order\s*[:\-]\s*(docs/work-orders/[\w\-]+\.md)", re.IGNORECASE)
_OPENSPEC_RE = re.compile(r"(?:openspec\s*[:\-]?\s*)(openspec/changes/[\w\-]+/)", re.IGNORECASE)
_SPECKIT_RE = re.compile(r"(?:speckit\s*[:\-]?\s*)(specs/[\w\-]+/)", re.IGNORECASE)


class PromptSourceError(FieldkitError):
    """A prompt source or its revision-bound contract is unavailable or unsafe."""


@dataclass(frozen=True)
class PromptSource:
    """One resolved candidate-relative prompt path and its format."""

    path: Path
    kind: PromptKind


@dataclass(frozen=True)
class FrozenPrompt:
    """Prompt execution authority frozen to one source revision."""

    source: PromptSource
    revision: str | None
    contract: PromptContract

    @property
    def contract_path(self) -> Path:
        """Return the file containing all execution authority for this prompt."""
        if self.source.kind == "work-order":
            return self.source.path
        return self.source.path.parent / "driver.yaml"


def _bounded_git(repo_root: Path, args: list[str], *, stdout_limit: int) -> BoundedProcessBytesResult:
    try:
        return run_bounded_process_bytes(
            ["git", *args],
            cwd=repo_root,
            timeout=_GIT_TIMEOUT,
            stdout_limit=stdout_limit,
            stderr_limit=_MAX_GIT_METADATA_BYTES,
            cleanup_timeout=TIMEOUT_PROCESS_KILL_GRACE,
        )
    except BoundedProcessError as exc:
        raise PromptSourceError("revision-bound Git input did not complete") from exc


def has_prompt_reference(body: str) -> bool:
    """Return whether an issue body contains a supported prompt reference."""
    return bool(_WORK_ORDER_RE.search(body) or _OPENSPEC_RE.search(body) or _SPECKIT_RE.search(body))


def _safe_source_path(repo_root: Path, relative: str, allowed: PurePosixPath) -> Path | None:
    relative_path = PurePosixPath(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts or not relative_path.is_relative_to(allowed):
        return None
    return repo_root / Path(relative_path)


def _exists_on_main_or_disk(repo_root: Path, path: Path) -> bool:
    if path.is_file():
        return True
    try:
        relative = path.relative_to(repo_root).as_posix()
        result = _bounded_git(
            repo_root,
            ["cat-file", "-t", f"origin/main:{relative}"],
            stdout_limit=_MAX_GIT_METADATA_BYTES,
        )
    except (ValueError, PromptSourceError):
        return False
    return result.returncode == 0 and result.stdout.decode("utf-8", errors="replace").strip() == "blob"


def _resolve_dir_source(
    repo_root: Path,
    relative_dir: str,
    *,
    allowed: PurePosixPath,
    kind: PromptKind,
    primary: str,
    fallback: str,
) -> PromptSource | None:
    directory = _safe_source_path(repo_root, relative_dir, allowed)
    if directory is None:
        return None
    for filename in (primary, fallback):
        candidate = directory / filename
        if _exists_on_main_or_disk(repo_root, candidate):
            return PromptSource(candidate, kind)
    return None


def resolve_prompt_source(issue: AgentIssue, repo_root: Path) -> PromptSource | None:
    """Resolve one safe prompt reference from an issue body.

    Work orders take precedence, followed by OpenSpec and Speckit. The retired
    ``Brief: docs/briefs/...`` compatibility form is intentionally unsupported.
    """
    work_order = _WORK_ORDER_RE.search(issue.body)
    if work_order is not None:
        path = _safe_source_path(repo_root, work_order.group(1), PurePosixPath("docs/work-orders"))
        if path is not None and _exists_on_main_or_disk(repo_root, path):
            return PromptSource(path, "work-order")
        return None
    openspec = _OPENSPEC_RE.search(issue.body)
    if openspec is not None:
        return _resolve_dir_source(
            repo_root,
            openspec.group(1),
            allowed=PurePosixPath("openspec/changes"),
            kind="openspec",
            primary="tasks.md",
            fallback="proposal.md",
        )
    speckit = _SPECKIT_RE.search(issue.body)
    if speckit is not None:
        return _resolve_dir_source(
            repo_root,
            speckit.group(1),
            allowed=PurePosixPath("specs"),
            kind="speckit",
            primary="tasks.md",
            fallback="spec.md",
        )
    return None


def freeze_origin_main(repo_root: Path, *, refresh: bool) -> str | None:
    """Return the exact ``origin/main`` revision used by a real checkout.

    A path without ``.git`` is treated as an isolated local fixture. A real Git
    checkout fails closed when refresh or revision resolution fails. Callers
    omit the refresh for side-effect-free dry runs.
    """
    if not (repo_root / ".git").exists():
        return None
    if refresh:
        fetched = _bounded_git(repo_root, ["fetch", "origin", "main"], stdout_limit=_MAX_GIT_METADATA_BYTES)
        if fetched.returncode != 0:
            raise PromptSourceError("cannot refresh origin/main for driver execution")
    result = _bounded_git(
        repo_root,
        ["rev-parse", "--verify", "origin/main^{commit}"],
        stdout_limit=_MAX_GIT_METADATA_BYTES,
    )
    if result.returncode != 0:
        raise PromptSourceError("cannot freeze origin/main for driver execution")
    revision = result.stdout.decode("utf-8", errors="replace").strip()
    if not _REVISION_RE.fullmatch(revision):
        raise PromptSourceError("origin/main did not resolve to a commit")
    return revision


def _git_blob(repo_root: Path, revision: str, relative_path: str, *, max_bytes: int) -> str:
    object_name = f"{revision}:{relative_path}"
    try:
        tree_result = _bounded_git(
            repo_root,
            ["ls-tree", "-z", revision, "--", relative_path],
            stdout_limit=_MAX_GIT_METADATA_BYTES,
        )
        if tree_result.returncode != 0:
            raise PromptSourceError("revision-bound driver input is unavailable")
        records = [record for record in tree_result.stdout.split(b"\0") if record]
        if len(records) != 1:
            raise PromptSourceError("revision-bound driver input is unavailable")
        metadata, separator, recorded_path = records[0].partition(b"\t")
        fields = metadata.split()
        if (
            not separator
            or recorded_path != relative_path.encode("utf-8")
            or len(fields) != 3
            or fields[0] not in {b"100644", b"100755"}
            or fields[1] != b"blob"
        ):
            raise PromptSourceError("revision-bound driver input is not a regular file")
        size_result = _bounded_git(
            repo_root,
            ["cat-file", "-s", object_name],
            stdout_limit=_MAX_GIT_METADATA_BYTES,
        )
        if size_result.returncode != 0:
            raise PromptSourceError("revision-bound driver input is unavailable")
        size = int(size_result.stdout.strip())
        if size < 0 or size > max_bytes:
            raise PromptSourceError("revision-bound driver input exceeds its byte limit")
        blob_result = _bounded_git(repo_root, ["cat-file", "blob", object_name], stdout_limit=max_bytes)
        if blob_result.returncode != 0:
            raise PromptSourceError("revision-bound driver input is unavailable")
        blob = blob_result.stdout
    except ValueError as exc:
        raise PromptSourceError("revision-bound driver input has an invalid size") from exc
    if len(blob) != size or len(blob) > max_bytes:
        raise PromptSourceError("revision-bound driver input changed during its bounded read")
    try:
        return blob.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PromptSourceError("revision-bound driver input is not valid UTF-8") from exc


def _local_blob(repo_root: Path, relative_path: str, *, max_bytes: int) -> str:
    path = repo_root / relative_path
    try:
        return read_text_snapshot(path, max_bytes=max_bytes).content
    except (FileNotFoundError, ValueError) as exc:
        raise PromptSourceError("local driver input is unavailable or unsafe") from exc


def _read_input(repo_root: Path, revision: str | None, relative_path: str, *, max_bytes: int) -> str:
    if revision is not None:
        return _git_blob(repo_root, revision, relative_path, max_bytes=max_bytes)
    return _local_blob(repo_root, relative_path, max_bytes=max_bytes)


def freeze_prompt(repo_root: Path, source: PromptSource, revision: str | None) -> FrozenPrompt:
    """Load and validate a prompt contract and all anchors from one revision."""
    try:
        relative = source.path.relative_to(repo_root).as_posix()
    except ValueError as exc:
        raise PromptSourceError("driver prompt lies outside the repository") from exc
    if source.kind != "work-order":
        _read_input(repo_root, revision, relative, max_bytes=_MAX_PROMPT_BYTES)
    contract_path = relative if source.kind == "work-order" else f"{PurePosixPath(relative).parent}/driver.yaml"
    contract_text = _read_input(repo_root, revision, contract_path, max_bytes=_MAX_PROMPT_BYTES)
    try:
        contract = parse_prompt_contract(contract_text, kind=source.kind)
        done_check_text = contract_text if source.kind == "work-order" else f"---\n{contract_text}\n---\n"
        parse_done_checks(done_check_text)
        validate_edit_sites(
            contract,
            lambda target: _read_input(repo_root, revision, target, max_bytes=_MAX_TARGET_BYTES),
        )
    except (DoneCheckError, PromptContractError) as exc:
        raise PromptSourceError(str(exc)) from exc
    return FrozenPrompt(source, revision, contract)
