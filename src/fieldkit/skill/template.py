"""Skill template rendering and installation.

Provides the shared context builder and renderer used by both the install
command and the eval runner.  This module is the sole location for
``{{key}}`` template logic — no private copies should exist elsewhere.

Configuration comes from fieldkit.config; architecture dependencies are enforced
by tach.toml.
"""

import logging
import os
import re
import shutil
import stat
import sys
from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple, TypeAlias

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

#: Alias for the template context dict — maps ``{{key}}`` names to values.
TemplateContext: TypeAlias = dict[str, str]


@dataclass
class SkillInstallResult:
    """Counts from a single ``install_skills()`` run.

    Attributes:
        rendered: Number of ``.md`` files rendered with template substitution.
        copied:   Number of non-``.md`` files copied verbatim.
        warned:   Number of unresolved ``{{key}}`` occurrences emitted to stderr.
        errors:   Number of ``OSError`` failures during file writes.
    """

    rendered: int = field(default=0)
    copied: int = field(default=0)
    warned: int = field(default=0)
    errors: int = field(default=0)


class UnresolvedVariable(NamedTuple):
    """A single unresolved template variable occurrence.

    Attributes:
        key:       The variable name (e.g. ``"email"``).
        file_path: Relative path of the file containing the unresolved token.
    """

    key: str
    file_path: str


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_MAX_ACCOUNT_INDEX = 4  # accounts.0 through accounts.3
_MAX_PRIMARY_PURSUIT_ENTRIES = 1024
_LOCAL_MARKDOWN_LINK = re.compile(r"\]\((?P<path>[^()\s#]+\.md)(?P<fragment>#[^()\s]+)?\)")


def _template_scalar(data: Mapping[str, object], key: str) -> str:
    from fieldkit.config import ConfigError

    value = data.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ConfigError(f"Template configuration field '{key}' must be a string")
    return value


def _load_config_fields() -> dict[str, str]:
    """Read personal fields from config.yaml.

    Returns a dict with keys: name, email, role, company, fieldkit_home.
    Returns {} if config.yaml is absent. Invalid configuration raises ConfigError.
    """
    # Import here to avoid circular imports and to keep the dependency explicit.
    import fieldkit.config._loader as _cfg_impl

    data = _cfg_impl._load_raw_config_uncached(strict=True)
    if data is None:
        return {}

    from fieldkit.config import ConfigError, get_config_path

    fieldkit_home = _template_scalar(data, "fieldkit_home")
    if fieldkit_home:
        workspace = Path(fieldkit_home.strip()).expanduser()
        if not fieldkit_home.strip() or not workspace.is_absolute():
            raise ConfigError("Template fieldkit_home must be an absolute path")
        fieldkit_home = str(get_config_path("accounts.yaml", workspace_root=workspace).parent.parent)

    return {
        "name": _template_scalar(data, "name"),
        "email": _template_scalar(data, "email"),
        "role": _template_scalar(data, "role"),
        "company": _template_scalar(data, "company"),
        "fieldkit_home": fieldkit_home,
    }


def _read_identity_yaml(identity_path: Path) -> dict[str, str]:
    """Parse identity.yaml and return {territory, salesforce_user_id}.

    Handles both flat and nested (``identity:`` key) layouts.
    Returns empty strings for missing keys. Invalid configuration raises ConfigError.
    """
    from fieldkit.config._loader import ConfigError, read_config_mapping_for_update

    identity_data = read_config_mapping_for_update(identity_path)

    # historic regression: support both flat and nested (``identity:`` key) layouts.
    nested = identity_data.get("identity")
    if "identity" in identity_data and not isinstance(nested, dict):
        raise ConfigError("Template identity configuration must contain an identity mapping")
    lookup: dict[str, object] = nested if isinstance(nested, dict) else identity_data

    return {
        "territory": _template_scalar(lookup, "territory"),
        "salesforce_user_id": _template_scalar(lookup, "salesforce_user_id"),
    }


def _territory_from_accounts(configuration: Mapping[str, object]) -> str:
    """Project territory from the same validated account snapshot as account fields."""
    from fieldkit.config import ConfigError

    accounts = configuration.get("accounts", {})
    if not isinstance(accounts, dict):
        raise ConfigError("Template account configuration must contain an accounts mapping")
    for info in accounts.values():
        if not isinstance(info, dict):
            raise ConfigError("Template account configuration must contain account mappings")
        if info.get("internal"):
            continue
        territory = _template_scalar(info, "sf_territory")
        if territory:
            return territory
    return ""


def _load_identity_fields(workspace_root: Path | None, account_territory: str) -> dict[str, str]:
    """Read territory and salesforce_user_id from <fieldkit_home>/config/identity.yaml.

    Returns a dict with keys: territory, salesforce_user_id.
    Returns empty strings for both if identity.yaml is absent or keys are missing.
    Invalid configuration raises ConfigError, including account configuration
    when territory fallback consults it.

    identity.yaml may use either a flat structure (keys at top level) or a
    nested structure (keys under an ``identity:`` mapping).  Both are handled.

    territory fallback: if identity.yaml does not contain a ``territory`` key,
    falls back to the ``sf_territory`` field of the primary account in
    accounts.yaml (the first non-internal account in the accounts list).
    """
    defaults: dict[str, str] = {"territory": "", "salesforce_user_id": ""}
    if workspace_root is None:
        return defaults

    from fieldkit.config import get_config_path

    if not workspace_root.exists() and not workspace_root.is_symlink():
        return defaults
    identity_path = get_config_path("identity.yaml", workspace_root=workspace_root)
    if not identity_path.exists():
        return defaults

    fields = _read_identity_yaml(identity_path)

    # historic regression: territory fallback — if identity.yaml does not have the field,
    # read sf_territory from the primary (first non-internal) account.
    if not fields["territory"]:
        fields["territory"] = account_territory

    return fields


def _primary_pursuit(workspace_root: Path, primary: str) -> str:
    """Inventory a flat account directory without following child redirects.

    Root authority comes from the canonical configuration path selector. The
    directory namespace must remain stable; this is not a filesystem sandbox
    or protection against hostile same-user replacement.
    """
    from fieldkit.config import ConfigError, get_config_path

    try:
        if (
            not primary.strip()
            or primary in {".", ".."}
            or any(character in primary for character in ("/", "\\", ":", "\x00"))
            or any(not character.isprintable() for character in primary)
        ):
            raise ValueError
        root = get_config_path("accounts.yaml", workspace_root=workspace_root).parent.parent
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        with ExitStack() as stack:
            directory = os.open(root, flags)
            stack.callback(os.close, directory)
            for component in ("accounts", primary, "pursuits"):
                try:
                    directory = os.open(component, flags, dir_fd=directory)
                except FileNotFoundError:
                    return ""
                stack.callback(os.close, directory)
            selected: str | None = None
            with os.scandir(directory) as entries:
                for count, entry in enumerate(entries, start=1):
                    if count > _MAX_PRIMARY_PURSUIT_ENTRIES:
                        raise ValueError
                    if not entry.name.endswith(".md"):
                        continue
                    if not stat.S_ISREG(entry.stat(follow_symlinks=False).st_mode):
                        raise ValueError
                    if selected is None or entry.name < selected:
                        selected = entry.name
            return Path(selected).stem if selected is not None else ""
    except (OSError, RuntimeError, ValueError, ConfigError):
        raise ConfigError("Cannot read safe primary pursuit inventory") from None


def _load_account_fields(workspace_root: Path | None) -> dict[str, str]:
    """Read account fields from one strict selected-workspace snapshot.

    Returns a dict with keys: primary_account, accounts.0-3, accounts.all,
    internal_domain, primary_pursuit, example_sf_id, and derived territory.
    Identity projection overrides territory, preserving empty territory when
    identity.yaml is absent.
    Absent configuration has empty defaults; invalid configuration propagates.
    """
    from fieldkit.config import get_accounts_config

    configuration = (
        get_accounts_config(strict=True, workspace_root=workspace_root) if workspace_root is not None else {}
    )
    slugs = list(configuration.get("accounts", {}))
    domains = configuration.get("internal_domains", [])

    primary = slugs[0] if slugs else ""

    ctx: dict[str, str] = {}

    # Populate real indices
    for i, slug in enumerate(slugs):
        ctx[f"accounts.{i}"] = slug

    # Fill gaps up to _MAX_ACCOUNT_INDEX so skills using accounts.1 or .2
    # don't produce unresolved warnings for a 1-account config.
    for i in range(len(slugs), _MAX_ACCOUNT_INDEX):
        ctx[f"accounts.{i}"] = primary

    ctx["primary_account"] = primary
    ctx["internal_domain"] = domains[0] if domains else ""
    ctx["accounts.all"] = ", ".join(slugs)
    ctx["example_sf_id"] = "006Pe000000ExampleId"
    ctx["territory"] = _territory_from_accounts(configuration)

    # primary_pursuit: stem of the first pursuit file for the primary account
    ctx["primary_pursuit"] = ""
    if slugs and workspace_root is not None:
        ctx["primary_pursuit"] = _primary_pursuit(workspace_root, primary)

    return ctx


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_template_ctx() -> dict[str, str]:
    """Build template variable context from config.yaml, accounts.yaml, and identity.yaml.

    Returns a dict mapping ``{{key}}`` names to their resolved string values.
    Missing config keys produce empty strings. Invalid account configuration
    consulted for territory fallback raises ConfigError before installation.

    Available variables:
        name, email, role, company, fieldkit_home   (from config.yaml)
        primary_account, accounts.0-3,
        accounts.all, internal_domain,
        primary_pursuit, example_sf_id              (from accounts.yaml)
        territory, salesforce_user_id               (from <fieldkit_home>/config/identity.yaml)

    Returns:
        Empty dict if config.yaml is absent. Invalid configuration raises ConfigError.
    """
    config_fields = _load_config_fields()
    if not config_fields:
        # config.yaml absent — return empty dict per spec FR-3 / contract
        return {}

    workspace = config_fields.get("fieldkit_home", "")
    selected_workspace = Path(workspace) if workspace else None
    account_fields = _load_account_fields(selected_workspace)
    identity_fields = _load_identity_fields(selected_workspace, account_fields.get("territory", ""))

    ctx: dict[str, str] = {}
    ctx.update(config_fields)
    ctx.update(account_fields)
    ctx.update(identity_fields)
    return ctx


def render_skill_text(text: str, ctx: dict[str, str]) -> tuple[str, list[str]]:
    """Replace ``{{key}}`` placeholders with values from ctx.

    Args:
        text: Raw skill file content (UTF-8 decoded string).
        ctx:  Template context from ``build_template_ctx()``.

    Returns:
        Tuple of ``(rendered_text, unresolved_keys)`` where:
        - ``rendered_text``: text with all known ``{{key}}`` tokens replaced.
        - ``unresolved_keys``: list of key names (str) for tokens not in ctx.
          Empty list if all tokens resolved.

    Behavior:
        - Unresolved keys are preserved verbatim in rendered_text.
        - Key matching is exact (no case folding, no fuzzy matching).
        - Whitespace inside ``{{ }}`` is stripped before lookup.
        - Empty key (``{{}}``) is left intact and NOT reported as unresolved.
        - Empty ctx: returns (text, [key for each ``{{key}}`` found]).
    """
    unresolved: list[str] = []

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1).strip()
        if not key:
            # Empty key — leave intact, not a valid variable name
            return match.group(0)
        if key in ctx:
            return ctx[key]
        unresolved.append(key)
        return match.group(0)

    rendered = re.sub(r"\{\{([^}]*)\}\}", _replace, text)
    return rendered, unresolved


def install_skills(
    skills_dir: Path,
    target_dir: Path,
    ctx: dict[str, str],
    *,
    dry_run: bool = False,
) -> SkillInstallResult:
    """Install rendered skill copies from skills_dir into target_dir.

    For each skill directory in skills_dir:
    - Mirrors directory structure under ``target_dir/<skill_name>/``.
    - ``.md`` files: rendered via ``render_skill_text()`` and written.
    - Non-``.md`` files: copied verbatim (``shutil.copy2``).
    - Existing symlinks at ``target_dir/<skill_name>``: unlinked and replaced
      with a real directory.
    - Existing real directories: overwritten (idempotent).
    - Directories starting with ``_`` or ``.`` are skipped.

    Delegates per-skill work to ``install_skill_dir()`` to avoid duplication.

    Args:
        skills_dir: Source skills directory (fieldkit/skills/).
        target_dir: Install destination (~/.agents/skills/).
        ctx:        Template context. Empty dict = copy all files verbatim.
        dry_run:    If True, print planned actions without writing.

    Returns:
        ``SkillInstallResult`` with counts of rendered/copied/warned/errors.

    Warnings:
        Unresolved ``{{key}}`` occurrences are printed to stderr::

            warning: unresolved template variable {{key}} in skill/file.md

    Errors:
        ``OSError`` during file write increments ``result.errors``.
        Does not raise — all errors are counted and reported via result.
    """
    result = SkillInstallResult()

    if not skills_dir.exists() or not skills_dir.is_dir():
        result.errors += 1
        return result

    for skill_dir in sorted(skills_dir.iterdir()):
        if not skill_dir.is_dir() or skill_dir.name.startswith(("_", ".")):
            continue
        if not (skill_dir / "SKILL.md").exists():
            print(f"warning: skipping {skill_dir.name}: no SKILL.md found", file=sys.stderr)
            continue
        skill_target = target_dir / skill_dir.name
        sub = install_skill_dir(skill_dir, skill_target, ctx, dry_run=dry_run)
        result.rendered += sub.rendered
        result.copied += sub.copied
        result.warned += sub.warned
        result.errors += sub.errors

    return result


def _handle_existing_skill(target_skill_dir: Path, skill_name: str, *, dry_run: bool, quiet: bool = False) -> None:
    """Unlink an existing symlink at target_skill_dir before installing."""
    if dry_run and not quiet:
        print(f"[dry-run] install skill: {skill_name} → {target_skill_dir}")
    if target_skill_dir.is_symlink():
        if dry_run and not quiet:
            print(f"[dry-run] unlink symlink: {target_skill_dir}")
        else:
            target_skill_dir.unlink()


def _copy_skill_file(
    src_file: Path,
    dst_file: Path,
    skill_name: str,
    rel: Path,
    ctx: dict[str, str],
    result: SkillInstallResult,
) -> None:
    """Copy or render a single skill file into the target directory.

    Mutates *result* in place with rendered/copied/warned/errors counts.
    """
    try:
        dst_file.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"warning: could not create directory {dst_file.parent}: {exc}", file=sys.stderr)
        result.errors += 1
        return

    if src_file.suffix == ".md":
        try:
            raw = src_file.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"warning: could not read {src_file}: {exc}", file=sys.stderr)
            result.errors += 1
            return

        rendered_text, unresolved_keys = render_skill_text(raw, ctx)
        rel_display = f"{skill_name}/{rel}"
        for key in unresolved_keys:
            print(f"warning: unresolved template variable {{{{{key}}}}} in {rel_display}", file=sys.stderr)
            result.warned += 1

        try:
            dst_file.write_text(rendered_text, encoding="utf-8")
            result.rendered += 1
        except OSError as exc:
            print(f"warning: could not write {dst_file}: {exc}", file=sys.stderr)
            result.errors += 1
    else:
        try:
            shutil.copy2(src_file, dst_file)
            result.copied += 1
        except OSError as exc:
            print(f"warning: could not copy {src_file} → {dst_file}: {exc}", file=sys.stderr)
            result.errors += 1


def install_skill_dir(
    skill_dir: Path,
    target_skill_dir: Path,
    ctx: dict[str, str],
    *,
    dry_run: bool = False,
    quiet: bool = False,
) -> SkillInstallResult:
    """Install a single skill directory into target_skill_dir.

    Mirrors the per-skill logic from ``install_skills()`` but operates on a
    single skill directory rather than iterating a parent.  This is used by
    the project-local install command to install individual selected skills.

    Args:
        skill_dir:        Source skill directory (e.g. ``skills/account-pulse/``).
        target_skill_dir: Destination directory for this skill
                          (e.g. ``.opencode/skills/account-pulse/``).
        ctx:              Template context. Empty dict preserves ``{{key}}`` tokens.
        dry_run:          If True, print planned actions without writing.

    Returns:
        ``SkillInstallResult`` with counts of rendered/copied/warned/errors.
    """
    result = SkillInstallResult()

    if not skill_dir.exists() or not skill_dir.is_dir():
        result.errors += 1
        return result

    if not (skill_dir / "SKILL.md").exists():
        print(
            f"warning: skipping {skill_dir.name}: no SKILL.md found (bundle directory?)",
            file=sys.stderr,
        )
        result.errors += 1
        return result

    skill_name = skill_dir.name
    _handle_existing_skill(target_skill_dir, skill_name, dry_run=dry_run, quiet=quiet)

    for src_file in sorted(skill_dir.rglob("*")):
        if not src_file.is_file():
            continue

        rel = src_file.relative_to(skill_dir)
        dst_file = target_skill_dir / rel

        if dry_run:
            action = "render" if src_file.suffix == ".md" else "copy"
            if not quiet:
                print(f"[dry-run] {action}: {skill_name}/{rel}")
            continue

        _copy_skill_file(src_file, dst_file, skill_name, rel, ctx, result)

    return result


def _flat_support_anchor(relative_path: Path) -> str:
    """Return a stable in-document anchor for one bundled Markdown file."""
    normalized = re.sub(r"[^a-z0-9]+", "-", relative_path.as_posix().lower()).strip("-")
    return f"fieldkit-support-{normalized}"


def _flat_sibling_link(candidate: Path, skill_root: Path, fragment: str | None) -> str | None:
    """Map an existing sibling skill document to its installed flat rule."""
    if not candidate.is_relative_to(skill_root.parent) or not candidate.is_file():
        return None
    relative = candidate.relative_to(skill_root.parent)
    if len(relative.parts) < 2:
        return None
    sibling = skill_root.parent / relative.parts[0]
    if sibling == skill_root or not (sibling / "SKILL.md").is_file():
        return None
    support = candidate.relative_to(sibling)
    anchor = fragment or ("" if support == Path("SKILL.md") else f"#{_flat_support_anchor(support)}")
    return f"]({sibling.name}.md{anchor})"


def _render_flat_skill_bundle(skill_dir: Path, ctx: dict[str, str]) -> tuple[str, list[UnresolvedVariable]]:
    """Render a Cursor-compatible one-file skill with local Markdown support included.

    Flat targets cannot retain a skill directory. Every Markdown file shipped in
    the skill bundle is therefore appended to the root rule, and relative local
    Markdown links are redirected to the matching in-document anchor. Links
    into sibling skill bundles point to the sibling flat rule and support anchor.
    Other links retain their original destination.
    """
    root_file = skill_dir / "SKILL.md"
    markdown_files = [root_file, *sorted(path for path in skill_dir.rglob("*.md") if path != root_file)]
    resolved_root = skill_dir.resolve()
    relative_paths = {path.resolve(): path.relative_to(skill_dir) for path in markdown_files}
    unresolved: list[UnresolvedVariable] = []
    rendered_files: list[tuple[Path, str]] = []

    for source_file in markdown_files:
        relative_path = relative_paths[source_file.resolve()]
        rendered_text, unresolved_keys = render_skill_text(source_file.read_text(encoding="utf-8"), ctx)
        unresolved.extend(UnresolvedVariable(key, relative_path.as_posix()) for key in unresolved_keys)

        source_parent = source_file.parent

        def _replace_local_link(match: re.Match[str], source_parent: Path = source_parent) -> str:
            candidate = (source_parent / match.group("path")).resolve()
            if not candidate.is_relative_to(resolved_root) or candidate not in relative_paths:
                return _flat_sibling_link(candidate, resolved_root, match.group("fragment")) or match.group(0)
            relative = relative_paths[candidate]
            anchor = match.group("fragment") or (
                "#" if relative == Path("SKILL.md") else f"#{_flat_support_anchor(relative)}"
            )
            return f"]({anchor})"

        rendered_files.append((relative_path, _LOCAL_MARKDOWN_LINK.sub(_replace_local_link, rendered_text)))

    root_text = rendered_files[0][1]
    support_sections = [
        f'<a id="{_flat_support_anchor(relative_path)}"></a>\n\n'
        f"## Bundled support: `{relative_path.as_posix()}`\n\n{rendered_text}"
        for relative_path, rendered_text in rendered_files[1:]
    ]
    if not support_sections:
        return root_text, unresolved
    return f"{root_text.rstrip()}\n\n---\n\n" + "\n\n---\n\n".join(support_sections) + "\n", unresolved


def install_skill_flat(
    skill_dir: Path,
    target_file: Path,
    ctx: dict[str, str],
    *,
    dry_run: bool = False,
    quiet: bool = False,
) -> SkillInstallResult:
    """Install a single skill as a flat rendered file (Cursor format).

    Renders ``SKILL.md`` and bundled Markdown support files into one complete
    artifact. Relative local Markdown links point to in-document anchors;
    non-Markdown support files remain intentionally excluded from flat targets.

    Args:
        skill_dir:   Source skill directory containing ``SKILL.md``.
        target_file: Destination file path (e.g. ``.cursor/rules/<name>.md``).
        ctx:         Template context. Empty dict preserves ``{{key}}`` tokens verbatim.
        dry_run:     If True, print the planned action without writing.

    Returns:
        ``SkillInstallResult`` with counts:
        - ``rendered=1`` on success (or dry-run).
        - ``errors=1`` if ``SKILL.md`` is absent or cannot be read/written.
        - ``warned`` incremented once per unresolved ``{{key}}`` token.

    Errors:
        ``OSError`` during file write increments ``result.errors``.
        Does not raise — all errors are counted and reported via result.
    """
    result = SkillInstallResult()

    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        print(
            f"warning: {skill_dir.name}: no SKILL.md found",
            file=sys.stderr,
        )
        result.errors += 1
        return result

    try:
        rendered_text, unresolved_variables = _render_flat_skill_bundle(skill_dir, ctx)
    except OSError as exc:
        print(f"warning: could not read Markdown support for {skill_dir.name}: {exc}", file=sys.stderr)
        result.errors += 1
        return result

    # Emit one warning per unresolved token
    for unresolved in unresolved_variables:
        print(
            f"warning: unresolved template variable {{{{{unresolved.key}}}}} in "
            f"{skill_dir.name}/{unresolved.file_path}",
            file=sys.stderr,
        )
        result.warned += 1

    if dry_run:
        if not quiet:
            print(f"[dry-run] render (flat): {skill_dir.name}/SKILL.md → {target_file}")
        result.rendered += 1
        return result

    # Ensure parent directory exists
    try:
        target_file.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(
            f"warning: could not create directory {target_file.parent}: {exc}",
            file=sys.stderr,
        )
        result.errors += 1
        return result

    try:
        target_file.write_text(rendered_text, encoding="utf-8")
        result.rendered += 1
    except OSError as exc:
        print(f"warning: could not write {target_file}: {exc}", file=sys.stderr)
        result.errors += 1

    return result
