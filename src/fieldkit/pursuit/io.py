"""Shared I/O layer for reading and writing pursuit frontmatter files.

Provides:
  extract_frontmatter_text(text) -> str | None
  search_frontmatter_text(text)  -> str | None
  split_frontmatter_raw(text)    -> tuple[str, str] | None
  load_pursuit(path) -> (PursuitFrontmatter, str, float)
  write_frontmatter(path, model, body, *, expected_mtime=None) -> None

Round-trip invariant: load then write leaves the file semantically identical.
The implementation preserves YAML key ordering and only writes keys that were
present in the original file (no extra sf_ fields added on round-trip).
"""

import fcntl
import hashlib
import json
import logging
import os
import re
import stat
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import yaml

from fieldkit.config import get_fieldkit_data
from fieldkit.errors import FrontmatterStalenessError
from fieldkit.pursuit.models import (
    SF_FIELD_NAMES,
    LegacyMEDDPICC,
    PursuitFrontmatter,
    TransitionEntry,
    canonicalize_legacy_meddpicc,
)

_log = logging.getLogger(__name__)


def _pursuit_lock_path(path: Path) -> Path:
    """Return the stable runtime lock path for a canonical pursuit target."""
    digest = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()
    return get_fieldkit_data() / "locks" / "pursuit" / f"{digest}.lock"


@contextmanager
def _pursuit_lock(path: Path) -> Generator[None, None, None]:
    """Serialize cooperating pursuit writers without adding workspace files."""
    lock_path = _pursuit_lock_path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_fh, fcntl.LOCK_UN)


class _DuplicateKeyLoader(yaml.SafeLoader):  # type: ignore[misc]
    """yaml.SafeLoader subclass that warns on duplicate YAML keys."""


def _construct_mapping_warn_duplicates(loader: yaml.SafeLoader, node: yaml.MappingNode) -> dict[object, object]:
    loader.flatten_mapping(node)
    seen: set[object] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node)
        if key in seen:
            _log.warning("Duplicate YAML key %r — last value wins", key)
        seen.add(key)
    pairs: list[tuple[object, object]] = loader.construct_pairs(node, deep=True)
    return dict(pairs)


# Override default mapping constructor to detect duplicates
_DuplicateKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping_warn_duplicates,
)

# Shared frontmatter regex — matches the YAML block between opening and closing "---" delimiters.
# Group 1 captures the raw YAML text.
# Internal only — use extract_frontmatter_text / search_frontmatter_text / split_frontmatter_raw.
_FM_RE = re.compile(r"^---\n(.*?)\n---", re.DOTALL)


def extract_frontmatter_text(text: str) -> str | None:
    """Return the raw YAML text between the first ``---`` delimiters, or ``None``.

    The match is anchored at the start of the file: the opening ``---`` must
    appear at position 0.  Use this for pursuit files and skill files where
    frontmatter is always the first block.

    Returns the YAML content string (what was between the delimiters), suitable
    for passing to ``yaml.safe_load``.  Returns ``None`` when no anchored
    frontmatter block is found.
    """
    m = _FM_RE.match(text)
    return m.group(1) if m else None


def split_frontmatter_raw(text: str) -> tuple[str, str] | None:
    """Split *text* into ``(frontmatter_yaml, tail)`` or return ``None``.

    *frontmatter_yaml* is the raw YAML text between the opening and closing
    ``---`` delimiters (same as :func:`extract_frontmatter_text`).

    *tail* is the remainder of the file starting immediately after the closing
    ``---`` line (i.e. ``text[m.end():]``).  Use this when you need to
    rewrite the frontmatter block in-place while preserving the body verbatim.

    The opening ``---`` must be at position 0 (anchored match).  Returns
    ``None`` when no frontmatter block is found.
    """
    m = _FM_RE.match(text)
    if not m:
        return None
    return m.group(1), text[m.end() :]


# SF fields recognized by the schema — derived from PursuitFrontmatter.model_fields
# so adding a new sf_ field to the model automatically updates this set.
_SF_FIELDS = SF_FIELD_NAMES

# Hyphenated alias → Python attribute name for PursuitFrontmatter fields
_ALIAS_TO_ATTR = {
    "gate-status": "gate_status",
    "last-transition": "last_transition",
    "last-updated": "last_updated",
    "transition-history": "transition_history",
}

# Python attribute name → YAML key (alias) for PursuitFrontmatter fields
_ATTR_TO_ALIAS = {v: k for k, v in _ALIAS_TO_ATTR.items()}

# Alias names used in YAML for hyphenated fields
_HYPHENATED_YAML_KEYS = set(_ALIAS_TO_ATTR.keys())


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str] | None:
    """Parse YAML frontmatter from a markdown file's text content.

    Returns (frontmatter_dict, body) where body is the text after the closing
    '---' delimiter (starting with the newline immediately after it).
    Returns None if no valid frontmatter block is found, if the YAML is
    syntactically invalid, or if the parsed YAML is not a mapping.

    Duplicate YAML keys are warned (not raised) via _DuplicateKeyLoader.
    The returned dict is the raw parsed YAML — no model validation applied.

    Args:
        text: Full file content as a string.

    Returns:
        A (dict, str) tuple on success, or None if no frontmatter block found.
    """
    result = split_frontmatter_raw(text)
    if result is None:
        return None
    fm_text, tail = result
    try:
        parsed = yaml.load(fm_text, Loader=_DuplicateKeyLoader)
    except yaml.YAMLError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed, tail


def parse_frontmatter_fallback(content: str) -> tuple[dict[str, Any], str] | tuple[None, str]:
    """Parse frontmatter, including an empty delimited YAML block."""
    result = parse_frontmatter(content)
    if result is not None:
        return result
    if not content.startswith("---"):
        return None, content
    parts = content.split("---", 2)
    if len(parts) < 3:
        return None, content
    parsed = _load_fallback_frontmatter(parts[1])
    if parsed is None:
        return None, content
    return parsed, parts[2]


def _load_fallback_frontmatter(frontmatter_text: str) -> dict[str, Any] | None:
    """Load a fallback YAML mapping, treating an empty block as a mapping."""
    try:
        parsed = yaml.safe_load(frontmatter_text)
    except yaml.YAMLError:
        return None
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        return None
    return parsed


def _split_frontmatter(content: str) -> tuple[str, str]:
    """Split file content into (frontmatter_text, body_text).

    Returns (fm_text, body) where fm_text is the raw YAML between the
    first two '---' delimiters, and body is everything after the second '---'
    (starting with the newline immediately after it).

    Raises ValueError if fewer than two '---' delimiters found.
    """
    lines = content.split("\n")
    dashes = [i for i, ln in enumerate(lines) if re.fullmatch(r"-{3,}\s*", ln)]
    if len(dashes) < 2:
        raise ValueError("Fewer than two '---' delimiters found; file has no YAML frontmatter.")
    if dashes[0] != 0:
        raise ValueError(
            f"YAML frontmatter must begin at line 0 (opening --- not found at start of file); "
            f"got first --- at line {dashes[0]}"
        )
    start, end = dashes[0], dashes[1]
    fm_text = "\n".join(lines[start + 1 : end])
    # body: everything from the line after the closing '---' onward
    # We join with \n — the result starts with \n when end+1 lines exist
    body_lines = lines[end + 1 :]
    body = "\n" + "\n".join(body_lines) if body_lines or content.endswith("\n") else ""
    return fm_text, body


def detect_duplicate_yaml_keys(fm_text: str) -> list[str]:
    """Return a list of duplicate YAML mapping key names found in *fm_text*.

    Duplicate keys are returned in the order they are encountered (first
    repetition only). An empty list means no duplicates were found.

    This is the shared detection primitive used by both the write-path guard
    (_validate_no_duplicate_keys) and the audit CLI (check_yaml_duplicates).
    """
    seen: set[str] = set()
    duplicates: list[str] = []

    class _Detector(yaml.SafeLoader):  # type: ignore[misc]
        pass

    def _check(loader: yaml.SafeLoader, node: yaml.MappingNode) -> dict[object, object]:
        loader.flatten_mapping(node)
        for key_node, _ in node.value:
            key = loader.construct_object(key_node)
            key_str = str(key)
            if key_str in seen:
                duplicates.append(key_str)
            seen.add(key_str)
        return {}

    _Detector.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
        _check,
    )
    yaml.load(fm_text, Loader=_Detector)
    return duplicates


def _validate_no_duplicate_keys(fm_text: str, path: "str | Path | None" = None) -> None:
    """Raise ValueError if fm_text contains duplicate YAML mapping keys."""
    duplicates = detect_duplicate_yaml_keys(fm_text)
    if duplicates:
        loc = f" in {path}" if path else ""
        raise ValueError(f"Duplicate YAML keys found{loc}: {duplicates}")


def load_pursuit(path: str | Path) -> tuple[PursuitFrontmatter, str, float]:
    """Read a pursuit .md file and return (PursuitFrontmatter, body_text, mtime).

    body_text is everything after the closing '---' delimiter, starting with
    a newline. Passing body_text directly to write_frontmatter reproduces the
    original file byte-for-byte (modulo YAML whitespace normalization).

    mtime is the file's st_mtime (seconds since epoch) captured immediately
    after reading. Pass it as expected_mtime to write_frontmatter to enable
    staleness detection: if the file was modified between load and write,
    write_frontmatter raises ValueError.
    """
    path = Path(path)
    with path.open(encoding="utf-8") as source:
        content = source.read()
        mtime = os.fstat(source.fileno()).st_mtime
    fm_text, body = _split_frontmatter(content)

    raw: dict[str, Any] = yaml.load(fm_text, Loader=_DuplicateKeyLoader) or {}

    # Coerce None sf_* string fields to empty string to preserve original intent:
    # 'sf_key: ""' in YAML loads as '' (fine), but 'sf_key:' or 'sf_key: null'
    # loads as None — treat those as '' for str | None sf_ fields only.
    # Float and list fields are left as None (handled by their own validators).
    _STR_SF_FIELDS = frozenset(
        {
            "sf_opportunity_id",
            "sf_name",
            "sf_stage",
            "sf_close_date",
            "sf_owner",
            "sf_next_steps",
            "sf_last_pulled",
        }
    )
    _coerce_null_sf_strings(raw, _STR_SF_FIELDS)

    model = PursuitFrontmatter(**raw)
    return model, body, mtime


def _coerce_null_sf_strings(raw: dict[str, Any], string_fields: frozenset[str]) -> None:
    """Normalize explicit nulls for Salesforce string fields in place."""
    for sf_key in _SF_FIELDS & string_fields:
        if sf_key in raw and raw[sf_key] is None:
            raw[sf_key] = ""


_YAML_BOOLEAN_KEYWORDS: frozenset[str] = frozenset(("true", "false", "null", "yes", "no", "on", "off"))
_YAML_SPECIAL_STARTS: tuple[str, ...] = (":", "{", "[", "|", ">", "!", "&", "*", "#", "?", "-", '"', "'")
_YAML_UNSAFE_CHARACTERS: tuple[str, ...] = (
    *(chr(codepoint) for codepoint in range(0x7F, 0xA0)),
    "\u2028",
    "\u2029",
)

# A string that YAML would otherwise load back as an int or float. Digit-only
# string fields (e.g. sf_opportunity_number "71721820") MUST be quoted on write,
# otherwise the bare scalar round-trips through yaml.load as an int and the
# str|None model field fails Pydantic validation on read-back (BUG: every synced
# pursuit file becomes unloadable). Mirrors the special-start guards above.
_YAML_NUMERIC_RE = re.compile(r"[+-]?(\d[\d_]*(\.\d*)?|\.\d+)([eE][+-]?\d+)?")
_YAML_DOLLAR_NUMERIC_RE = re.compile(r"\$[\d,]+(?:\.\d+)?")


def _yaml_needs_quote(val: str) -> bool:
    """Return True when a YAML string value requires double-quoting.

    Extracted from ``_yaml_scalar`` to reduce its cyclomatic complexity (CRAP gate).
    Called only for non-empty string values.
    """
    if _yaml_syntax_requires_quote(val):
        return True
    return not _plain_yaml_scalar_round_trips(val)


def _yaml_syntax_requires_quote(val: str) -> bool:
    """Return whether YAML syntax alone requires a quoted scalar."""
    return any(
        (
            ": " in val,
            "\n" in val,
            "\r" in val,
            "\x00" in val,
            "---" in val,
            val.startswith(_YAML_SPECIAL_STARTS),
            _has_yaml_unsafe_unicode(val),
            val.lower() in _YAML_BOOLEAN_KEYWORDS,
            _YAML_NUMERIC_RE.fullmatch(val) is not None,
            _YAML_DOLLAR_NUMERIC_RE.fullmatch(val) is not None,
        )
    )


def _has_yaml_unsafe_unicode(val: str) -> bool:
    """Return whether a scalar contains a character YAML requires escaped."""
    return any(character in val for character in _YAML_UNSAFE_CHARACTERS)


def _plain_yaml_scalar_round_trips(val: str) -> bool:
    """Return whether YAML loads an unquoted scalar as the same string."""
    try:
        loaded = yaml.safe_load(val)
    except (ValueError, yaml.YAMLError):
        return False
    return isinstance(loaded, str) and loaded == val


def _yaml_scalar(val: Any) -> str:
    """Render a scalar value as a YAML string fragment (no trailing newline)."""
    if val is None:
        return "null"
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, int):
        return str(val)
    if isinstance(val, float):
        return str(val)
    if isinstance(val, str):
        if val == "":
            return '""'
        if _yaml_needs_quote(val):
            return _yaml_quoted_scalar(val)
        return val
    if hasattr(val, "isoformat"):
        return str(val.isoformat())
    return str(val)


def _yaml_quoted_scalar(val: str) -> str:
    """Quote a string without losing non-BMP Unicode or YAML-unsafe characters."""
    rendered = json.dumps(val, ensure_ascii=False)
    for character in _YAML_UNSAFE_CHARACTERS:
        rendered = rendered.replace(character, character.encode("unicode_escape").decode("ascii"))
    return rendered


def _render_legacy_meddpicc_block(raw_value: dict[str, Any], model_value: LegacyMEDDPICC) -> list[str]:
    """Render historical MEDDPICC data without interpreting any stored value."""
    if "schema_version" in raw_value:
        canonical = raw_value
    else:
        canonical = {"schema_version": 1, "status": "historical", **raw_value}
    # Validation pins metadata while the original mapping remains the
    # serialization source, preserving key order and exact archival values.
    LegacyMEDDPICC.model_validate(canonical)
    return render_raw_key_value("legacy_meddpicc", canonical)


def _render_transition_history(entries: list[TransitionEntry]) -> list[str]:
    """Render transition-history as a YAML block sequence.

    Each TransitionEntry is serialised using ``model_dump(by_alias=True,
    exclude_none=True)`` so that:
    - Hyphenated YAML aliases (``from``, ``gate-result``, ``override-reason``)
      are used instead of the Python attribute names.
    - Fields absent from the original entry are not written (no spurious nulls).
    """
    lines = ["transition-history:"]
    for entry in entries:
        # Use model_dump to get aliased keys and skip None fields cleanly.
        entry_dict = entry.model_dump(by_alias=True, exclude_none=True)
        items = list(entry_dict.items())
        if not items:
            continue
        first = True
        for ek, ev in items:
            ev_str = _yaml_scalar(ev)
            if first:
                lines.append(f"  - {ek}: {ev_str}")
                first = False
            else:
                lines.append(f"    {ek}: {ev_str}")
    return lines


def _render_key_value(key: str, model: PursuitFrontmatter, raw: dict[str, Any]) -> list[str]:
    """Return the formatted YAML line(s) for a single frontmatter key.

    Returns a list of strings (multiple lines for block scalars like legacy_meddpicc
    and transition-history, single item otherwise).
    """
    if key in {"meddpicc", "legacy_meddpicc"}:
        return _render_historical_qualification(key, model, raw)

    if key == "transition-history":
        val = model.transition_history
        if val is None:
            return ["transition-history: null"]
        return _render_transition_history(val)

    if key in _SF_FIELDS:
        sf_val: Any = getattr(model, key, None)
        if sf_val is None:
            sf_val = ""
        if isinstance(sf_val, (dict, list)):
            return render_raw_key_value(key, sf_val)
        return [f"{key}: {_yaml_scalar(sf_val)}"]

    if key in _HYPHENATED_YAML_KEYS:
        attr = _ALIAS_TO_ATTR[key]
        val = getattr(model, attr, None)
        if val is None:
            orig_val = raw.get(key)
            if orig_val is None:
                return []  # Skip — field was null/absent originally
            return [f"{key}: null"]
        return [f"{key}: {_yaml_scalar(val)}"]

    if key == "stage":
        return [f"stage: {model.stage}"]

    # Unknown/extra key: preserve from original raw dict
    return render_raw_key_value(key, raw[key])


def _render_historical_qualification(key: str, model: PursuitFrontmatter, raw: dict[str, Any]) -> list[str]:
    """Render a stored historical qualification mapping without scoring it."""
    raw_value = raw.get(key)
    if raw_value is None:
        return []
    if model.legacy_meddpicc is None:
        raise ValueError(f"{key} is present on disk but missing from the validated model")
    if not isinstance(raw_value, dict):
        raise ValueError(f"{key} must be a mapping")
    return _render_legacy_meddpicc_block(raw_value, model.legacy_meddpicc)


def write_frontmatter(
    path: str | Path,
    model: PursuitFrontmatter,
    body: str,
    *,
    expected_mtime: float | None = None,
) -> None:
    """Write a pursuit file atomically: '---\\n<yaml>\\n---<body>'.

    Preserves YAML key ordering by re-reading the current file to get the
    original key order, then serializing only those keys. Does not add new
    sf_* fields that weren't in the original file. Writes atomically via
    tempfile + os.replace.

    body is the string returned by load_pursuit — starts with '\\n' and
    contains everything after the closing '---' delimiter.

    expected_mtime: when provided (the mtime float from load_pursuit), the
    file's current mtime is compared before writing. If they differ, the file
    was modified between load and write and this call raises ValueError:
      "write_frontmatter: file modified since load — stale model detected: {path}"
    Pass None (the default) to skip the check.

    Concurrency: cooperating pursuit writers are serialized with a persistent
    lock under the runtime data directory. Pass expected_mtime for every
    load-modify-write cycle so a writer that waited for a newer replacement
    refuses its stale model.
    """
    path = Path(path)
    with _pursuit_lock(path):
        if path.is_symlink():
            raise OSError(f"Refusing to replace symlinked pursuit file: {path}")
        _write_frontmatter_locked(path, model, body, expected_mtime=expected_mtime)


def _write_frontmatter_locked(
    path: Path,
    model: PursuitFrontmatter,
    body: str,
    *,
    expected_mtime: float | None,
) -> None:
    """Render and replace typed pursuit frontmatter while its runtime lock is held."""
    if expected_mtime is not None and path.stat().st_mtime != expected_mtime:
        raise ValueError(f"write_frontmatter: file modified since load — stale model detected: {path}")
    current = path.read_text(encoding="utf-8")
    fm_text, _ = _split_frontmatter(current)

    # Parse original to get key order; raises ValueError if duplicate keys found
    _validate_no_duplicate_keys(fm_text, path)
    raw: dict[str, Any] = yaml.load(fm_text, Loader=_DuplicateKeyLoader) or {}

    # Build frontmatter lines preserving original key order
    fm_lines: list[str] = []
    for key in raw:
        fm_lines.extend(_render_key_value(key, model, raw))

    yaml_str = "\n".join(fm_lines)
    new_content = f"---\n{yaml_str}\n---{body}"

    _validate_no_duplicate_keys(yaml_str, path)
    _atomic_text_write(path, new_content)


def _publish_text_temp(tmp_path: str, path: Path, *, exclusive_create: bool) -> None:
    """Publish a completed temporary file, optionally without replacing a peer."""
    if not exclusive_create:
        Path(tmp_path).replace(path)
        return
    try:
        os.link(tmp_path, path)
    except FileExistsError as exc:
        raise FileExistsError(f"Pursuit file already exists: {path}") from exc
    Path(tmp_path).unlink()


def _existing_mode(path: Path) -> int | None:
    """Return the current file mode when a replacement should preserve it."""
    return stat.S_IMODE(path.stat().st_mode) if path.exists() else None


def _atomic_text_write(path: Path, content: str, *, exclusive_create: bool = False) -> None:
    """Publish text atomically and clean up its same-directory temporary file."""
    existing_mode = None if exclusive_create else _existing_mode(path)
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
            tmp_path = tmp.name
            if existing_mode is not None:
                os.fchmod(tmp.fileno(), existing_mode)
            tmp.write(content)
            tmp.flush()
            os.fsync(tmp.fileno())
        _publish_text_temp(tmp_path, path, exclusive_create=exclusive_create)
        tmp_path = None
    except BaseException:
        # historic regression: clean up orphaned temp file on any failure (disk full, cross-device, Ctrl-C)
        if tmp_path is not None:
            Path(tmp_path).unlink(missing_ok=True)
        raise


def render_raw_key_value(key: str, value: Any) -> list[str]:
    """Render a single frontmatter key-value pair as YAML line(s).

    Uses _yaml_scalar() for correct escaping (same as write_frontmatter).
    Returns a list of strings (one per line, no trailing newlines). Simple
    scalars return a single-element list; nested structures (dicts, lists)
    use yaml.dump with sort_keys=False for the value portion only.

    Args:
        key: YAML key name.
        value: Python value to serialize.

    Returns:
        List of YAML line strings (no trailing newlines).
    """
    if isinstance(value, (dict, list)):
        # Use yaml.dump for nested structures; sort_keys=False preserves insertion order.
        # yaml.dump() returns Any per stubs, so cast to str before splitlines().
        dumped: str = yaml.dump(
            {key: value},
            default_flow_style=False,
            allow_unicode=False,
            sort_keys=False,
        ).strip()
        return dumped.splitlines()
    return [f"{_yaml_scalar(key)}: {_yaml_scalar(value)}"]


def _canonicalize_raw_frontmatter(
    fm: dict[str, Any], raw_on_disk: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Canonicalize historical qualification without mixing its two shapes."""
    fm_had_old = "meddpicc" in fm
    fm_had_canonical = "legacy_meddpicc" in fm
    canonical_fm = canonicalize_legacy_meddpicc(fm)
    if (fm_had_old and "legacy_meddpicc" in raw_on_disk) or (fm_had_canonical and "meddpicc" in raw_on_disk):
        raise ValueError("frontmatter write mixes meddpicc and legacy_meddpicc representations")
    return canonical_fm, canonicalize_legacy_meddpicc(raw_on_disk)


def _render_new_raw_frontmatter(fm: dict[str, Any]) -> str:
    """Render a new frontmatter mapping in its supplied key order."""
    if "meddpicc" in fm:
        raise ValueError("cannot create active local MEDDPICC scores; use the native ClosePlan workflow")
    canonical_fm = canonicalize_legacy_meddpicc(fm)
    fm_lines: list[str] = []
    for key, val in canonical_fm.items():
        fm_lines.extend(render_raw_key_value(key, val))
    return "\n".join(fm_lines)


def render_frontmatter_raw(
    path: str | Path,
    fm: dict[str, Any],
    body: str,
    *,
    create: bool = False,
    expected_mtime: float | None = None,
    remove_keys: frozenset[str] = frozenset(),
) -> str:
    """Render and validate the exact frontmatter replacement without writing it.

    Body convention: body starts with '\\n' (the newline immediately after the
    closing '---' delimiter), matching the convention of write_frontmatter() and
    load_pursuit().

    Args:
        path: Path to the target file.
        fm: Dict of frontmatter key-value pairs to render.
        body: Everything after the closing '---' delimiter, starting with '\\n'.
        create: When True, render keys in fm dict iteration order; safe to call
            on non-existent paths. When False (default), preserve on-disk key
            order by re-reading the current file.
        expected_mtime: When provided, the file's current mtime is compared
            before rendering. Raises FrontmatterStalenessError on mismatch.
        remove_keys: Existing top-level keys to omit from the replacement.

    Raises:
        FileNotFoundError: create=False and file does not exist.
        ValueError: create=False and no frontmatter block found in file.
        FrontmatterStalenessError: expected_mtime was provided but the target
            is missing or its mtime differs. Subclasses FieldkitError
            (canonical home: fieldkit.errors).
    """
    path = Path(path)

    if expected_mtime is not None and (not path.exists() or path.stat().st_mtime != expected_mtime):
        raise FrontmatterStalenessError(f"File modified since last read: {path}")

    if create:
        yaml_str = _render_new_raw_frontmatter(fm)
    else:
        # Existing-file path: preserve on-disk key order.
        if not path.exists():
            raise FileNotFoundError(f"write_frontmatter_raw: file not found: {path}")
        current = path.read_text(encoding="utf-8")
        try:
            fm_text, _ = _split_frontmatter(current)
        except ValueError as exc:
            raise ValueError(f"write_frontmatter_raw: no frontmatter block in {path}") from exc
        _validate_no_duplicate_keys(fm_text, path)
        raw_on_disk: dict[str, Any] = yaml.load(fm_text, Loader=_DuplicateKeyLoader) or {}

        fm, raw_on_disk = _canonicalize_raw_frontmatter(fm, raw_on_disk)

        fm_lines = []
        # Preserve on-disk key order; use new value from fm if provided, else keep original.
        for key in raw_on_disk:
            if key in remove_keys:
                continue
            fm_lines.extend(render_raw_key_value(key, fm.get(key, raw_on_disk[key])))
        # Append new keys from fm that were not on disk.
        for key in fm:
            if key not in raw_on_disk:
                fm_lines.extend(render_raw_key_value(key, fm[key]))
        yaml_str = "\n".join(fm_lines)

    new_content = f"---\n{yaml_str}\n---{body}"
    _validate_no_duplicate_keys(yaml_str, path)
    return new_content


def write_frontmatter_raw(
    path: str | Path,
    fm: dict[str, Any],
    body: str,
    *,
    create: bool = False,
    exclusive_create: bool = False,
    expected_mtime: float | None = None,
    remove_keys: frozenset[str] = frozenset(),
) -> None:
    """Write validated raw frontmatter under the pursuit's runtime lock."""
    path = Path(path)
    if exclusive_create and not create:
        raise ValueError("exclusive_create requires create=True")
    path.parent.mkdir(parents=True, exist_ok=True)
    with _pursuit_lock(path):
        _write_frontmatter_raw_locked(
            path,
            fm=fm,
            body=body,
            create=create,
            exclusive_create=exclusive_create,
            expected_mtime=expected_mtime,
            remove_keys=remove_keys,
        )


def _write_frontmatter_raw_locked(
    path: Path,
    *,
    fm: dict[str, Any],
    body: str,
    create: bool,
    exclusive_create: bool,
    expected_mtime: float | None,
    remove_keys: frozenset[str],
) -> None:
    """Validate and replace one pursuit while its runtime lock is held."""
    if path.is_symlink():
        raise OSError(f"Refusing to replace symlinked pursuit file: {path}")
    if exclusive_create and path.exists():
        raise FileExistsError(f"Pursuit file already exists: {path}")
    new_content = render_frontmatter_raw(
        path,
        fm,
        body,
        create=create,
        expected_mtime=expected_mtime,
        remove_keys=remove_keys,
    )
    _atomic_text_write(path, new_content, exclusive_create=exclusive_create)
