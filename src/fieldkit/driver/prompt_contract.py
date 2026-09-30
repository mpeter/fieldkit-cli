"""Validated, versioned edit-site contracts for unattended driver prompts."""

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal

from fieldkit.errors import FieldkitError
from fieldkit.util.strict_yaml import StrictYAMLError, load_strict_yaml

PromptKind = Literal["work-order", "openspec", "speckit"]

_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---(?:[ \t]*\r?\n|\Z)", re.DOTALL)
_MAX_EDIT_SITES = 128
_MAX_ANCHOR_BYTES = 64 * 1024


class PromptContractError(FieldkitError):
    """A driver prompt cannot be executed safely as written."""


@dataclass(frozen=True)
class EditSite:
    """One exact, candidate-relative edit location."""

    path: str
    anchor: str


@dataclass(frozen=True)
class PromptContract:
    """Scheduling and edit-site authority bound to one prompt revision."""

    covers: frozenset[str]
    depends_on: tuple[int, ...]
    edit_sites: tuple[EditSite, ...]


def _mapping(value: object, location: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise PromptContractError(f"{location} must be an object")
    return value


def _load_mapping(text: str, *, kind: PromptKind) -> dict[str, object]:
    yaml_text = text
    if kind == "work-order":
        match = _FRONTMATTER_RE.match(text)
        if match is None:
            raise PromptContractError("work order must begin with YAML frontmatter")
        yaml_text = match.group(1)
    try:
        loaded = load_strict_yaml(yaml_text)
    except StrictYAMLError as exc:
        if exc.reason == "duplicate_key":
            raise PromptContractError("duplicate prompt contract key") from exc
        if exc.reason == "non_scalar_key":
            raise PromptContractError("prompt contract keys must be scalar values") from exc
        raise PromptContractError("prompt contract is not valid YAML") from exc
    return _mapping(loaded, "prompt contract")


def _safe_path(value: object, location: str, *, allow_directory: bool = False) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise PromptContractError(f"{location} must be a nonempty POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or (value.endswith("/") and not allow_directory):
        raise PromptContractError(f"{location} must stay within the candidate repository")
    normalized = path.as_posix()
    return f"{normalized}/" if allow_directory and value.endswith("/") else normalized


def _covers(mapping: dict[str, object]) -> frozenset[str] | None:
    value = mapping.get("covers")
    if value is None:
        return None
    if not isinstance(value, list) or not value:
        raise PromptContractError("covers must be a nonempty list of candidate-relative paths")
    paths = frozenset(_safe_path(item, "covers entry", allow_directory=True) for item in value)
    return paths or None


def _depends_on(mapping: dict[str, object]) -> tuple[int, ...]:
    value = mapping.get("depends_on", [])
    if not isinstance(value, list):
        raise PromptContractError("depends_on must be a list of positive issue numbers")
    numbers: list[int] = []
    for item in value:
        parsed_item = int(item[1:]) if isinstance(item, str) and re.fullmatch(r"#[1-9][0-9]*", item) else item
        if isinstance(parsed_item, bool) or not isinstance(parsed_item, int) or parsed_item < 1:
            raise PromptContractError("depends_on must contain only positive issue numbers")
        numbers.append(parsed_item)
    return tuple(dict.fromkeys(numbers))


def parse_prompt_contract(text: str, *, kind: PromptKind) -> PromptContract:
    """Parse a complete fail-closed prompt contract.

    Work orders carry the mapping in their frontmatter. OpenSpec and Speckit
    prompts use an adjacent ``driver.yaml`` sidecar and pass that sidecar text.
    """
    mapping = _load_mapping(text, kind=kind)
    covers = _covers(mapping)
    depends_on = _depends_on(mapping)
    edit_sites = mapping.get("edit_sites")
    if not isinstance(edit_sites, dict):
        raise PromptContractError("edit_sites must be a versioned object")
    edit_mapping = _mapping(edit_sites, "edit_sites")
    if set(edit_mapping) != {"version", "sites"} or edit_mapping.get("version") != 1:
        raise PromptContractError("edit_sites must contain only version: 1 and sites")
    sites = edit_mapping.get("sites")
    if not isinstance(sites, list) or not sites or len(sites) > _MAX_EDIT_SITES:
        raise PromptContractError("edit_sites.sites must contain 1-128 entries")
    parsed: list[EditSite] = []
    for index, raw_site in enumerate(sites):
        site = _mapping(raw_site, f"edit_sites.sites[{index}]")
        if set(site) != {"path", "anchor"}:
            raise PromptContractError(f"edit_sites.sites[{index}] must contain only path and anchor")
        path = _safe_path(site.get("path"), f"edit_sites.sites[{index}].path")
        anchor = site.get("anchor")
        if (
            not isinstance(anchor, str)
            or not anchor
            or "\x00" in anchor
            or len(anchor.encode("utf-8")) > _MAX_ANCHOR_BYTES
        ):
            raise PromptContractError(f"edit_sites.sites[{index}].anchor must be 1-{_MAX_ANCHOR_BYTES} UTF-8 bytes")
        parsed.append(EditSite(path, anchor))
    if len(set(parsed)) != len(parsed):
        raise PromptContractError("edit_sites contains a duplicate path and anchor")
    if covers is None:
        raise PromptContractError("covers must declare authority for every edit site")
    for index, parsed_site in enumerate(parsed):
        if not any(
            parsed_site.path == cover or (cover.endswith("/") and parsed_site.path.startswith(cover))
            for cover in covers
        ):
            raise PromptContractError(f"edit site {index} is outside covers")
    return PromptContract(covers, depends_on, tuple(parsed))


def validate_edit_sites(contract: PromptContract, read_target: Callable[[str], str]) -> None:
    """Prove every declared anchor occurs exactly once in its revision-bound target."""
    for index, site in enumerate(contract.edit_sites):
        target = read_target(site.path)
        matches = target.count(site.anchor)
        if matches != 1:
            disposition = "missing" if matches == 0 else f"ambiguous ({matches} matches)"
            raise PromptContractError(f"edit site {index} is {disposition}")
