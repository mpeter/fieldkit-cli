"""Skill template rendering and installation.

Provides the shared context builder and renderer used by both the install
command and the eval runner.  This module is the sole location for
``{{key}}`` template logic — no private copies should exist elsewhere.

Import constraint: MUST NOT import from ``fieldkit/`` or ``hooks/``.
Only stdlib, third-party, and other ``lib/`` modules are permitted.
Enforced by tach.
"""

import logging
import re
import shutil
import sys
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
_LOCAL_MARKDOWN_LINK = re.compile(r"\]\((?P<path>[^()\s#]+\.md)(?P<fragment>#[^()\s]+)?\)")


def _load_config_fields() -> dict[str, str]:
    """Read personal fields from config.yaml.

    Returns a dict with keys: name, email, role, company, data_repo.
    Returns {} if config.yaml is absent or unreadable — never raises.

    Note: ``data_repo`` is a legacy key retained for backward compatibility with
    skill templates that reference ``{{ data_repo }}``.  New installs use
    ``fieldkit_home`` instead.  The returned ``data_repo`` value is populated
    from the ``data_repo`` key in config.yaml (deprecated) and will be empty
    for installs that have migrated to ``fieldkit_home``.
    """
    # Import here to avoid circular imports and to keep the dependency explicit.
    import fieldkit.config._loader as _cfg_impl

    CONFIG_PATH = _cfg_impl.CONFIG_PATH

    if not CONFIG_PATH.exists():
        return {}

    try:
        import yaml

        raw = CONFIG_PATH.read_text(encoding="utf-8")
        data = yaml.safe_load(raw)
    except Exception:  # noqa: BLE001  — any parse/IO failure → empty
        log.debug("Failed to load config fields from %s", CONFIG_PATH, exc_info=True)
        return {}

    if not isinstance(data, dict):
        return {}

    def _str(key: str) -> str:
        val = data.get(key)
        return str(val) if val is not None else ""

    # data_repo: expand user path for display purposes.
    # DEPRECATED — new installs use 'fieldkit_home'; this key is read only for
    # backward compatibility with skill templates that reference {{ data_repo }}.
    data_repo_raw = data.get("data_repo", "")
    data_repo = str(Path(str(data_repo_raw)).expanduser()) if data_repo_raw else ""

    return {
        "name": _str("name"),
        "email": _str("email"),
        "role": _str("role"),
        "company": _str("company"),
        "data_repo": data_repo,
    }


def _read_identity_yaml(identity_path: Path) -> dict[str, str]:
    """Parse identity.yaml and return {territory, salesforce_user_id}.

    Handles both flat and nested (``identity:`` key) layouts.
    Returns empty strings for missing keys — never raises.
    """
    import yaml

    defaults: dict[str, str] = {"territory": "", "salesforce_user_id": ""}
    try:
        identity_raw = identity_path.read_text(encoding="utf-8")
        identity_data = yaml.safe_load(identity_raw)
    except Exception:  # noqa: BLE001
        log.debug("Failed to read identity.yaml at %s", identity_path, exc_info=True)
        return defaults

    if not isinstance(identity_data, dict):
        return defaults

    # historic regression: support both flat and nested (``identity:`` key) layouts.
    nested = identity_data.get("identity")
    lookup: dict[str, object] = nested if isinstance(nested, dict) else identity_data

    def _str(key: str) -> str:
        val = lookup.get(key)
        return str(val) if val is not None else ""

    return {"territory": _str("territory"), "salesforce_user_id": _str("salesforce_user_id")}


def _territory_from_accounts(data_repo_raw: str) -> str:
    """historic regression: fall back to sf_territory from the first non-internal account in accounts.yaml."""
    import yaml

    try:
        accounts_path = Path(str(data_repo_raw)).expanduser() / "config" / "accounts.yaml"
        if not accounts_path.exists():
            return ""
        accounts_raw = accounts_path.read_text(encoding="utf-8")
        accounts_data = yaml.safe_load(accounts_raw)
        if not isinstance(accounts_data, dict):
            return ""
        raw_accounts = accounts_data.get("accounts", {})
        if not isinstance(raw_accounts, dict):
            return ""
        for _slug, info in raw_accounts.items():
            if not isinstance(info, dict) or info.get("internal"):
                continue
            sf_terr = info.get("sf_territory")
            if sf_terr and isinstance(sf_terr, str):
                return sf_terr
    except Exception:  # noqa: BLE001
        log.debug("Failed to resolve territory from accounts.yaml for %s", data_repo_raw, exc_info=True)
    return ""


def _load_identity_fields() -> dict[str, str]:
    """Read territory and salesforce_user_id from <data_repo>/config/identity.yaml.

    Returns a dict with keys: territory, salesforce_user_id.
    Returns empty strings for both if identity.yaml is absent, unreadable,
    or the keys are missing — never raises.

    identity.yaml may use either a flat structure (keys at top level) or a
    nested structure (keys under an ``identity:`` mapping).  Both are handled.

    territory fallback: if identity.yaml does not contain a ``territory`` key,
    falls back to the ``sf_territory`` field of the primary account in
    accounts.yaml (the first non-internal account in the accounts list).
    """
    import fieldkit.config._loader as _cfg_impl

    CONFIG_PATH = _cfg_impl.CONFIG_PATH
    defaults: dict[str, str] = {"territory": "", "salesforce_user_id": ""}

    if not CONFIG_PATH.exists():
        return defaults

    try:
        import yaml

        raw = CONFIG_PATH.read_text(encoding="utf-8")
        data = yaml.safe_load(raw)
    except Exception:  # noqa: BLE001
        log.debug("Failed to load identity fields from %s", CONFIG_PATH, exc_info=True)
        return defaults

    if not isinstance(data, dict):
        return defaults

    data_repo_raw = data.get("fieldkit_home", "") or data.get("data_repo", "")
    if not data_repo_raw:
        return defaults

    identity_path = Path(str(data_repo_raw)).expanduser() / "config" / "identity.yaml"
    if not identity_path.exists():
        return defaults

    fields = _read_identity_yaml(identity_path)

    # historic regression: territory fallback — if identity.yaml does not have the field,
    # read sf_territory from the primary (first non-internal) account.
    if not fields["territory"]:
        fields["territory"] = _territory_from_accounts(data_repo_raw)

    return fields


def _load_account_fields() -> dict[str, str]:
    """Read account fields from accounts.yaml via lib.config helpers.

    Returns a dict with keys: primary_account, accounts.0-3, accounts.all,
    internal_domain, primary_pursuit, example_sf_id.
    All values default to "" on any failure — never raises.
    """
    from fieldkit.config import get_account_names, get_accounts_root, get_internal_domains

    try:
        slugs = get_account_names()
    except Exception:  # noqa: BLE001
        log.debug("Failed to load account names", exc_info=True)
        slugs = []

    try:
        domains = get_internal_domains()
    except Exception:  # noqa: BLE001
        log.debug("Failed to load internal domains", exc_info=True)
        domains = []

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

    # primary_pursuit: stem of the first pursuit file for the primary account
    ctx["primary_pursuit"] = ""
    if primary:
        try:
            pursuits_dir = get_accounts_root() / primary / "pursuits"
            if pursuits_dir.is_dir():
                md_files = sorted(pursuits_dir.glob("*.md"))
                if md_files:
                    ctx["primary_pursuit"] = md_files[0].stem
        except Exception:  # noqa: BLE001
            log.debug("Failed to resolve primary_pursuit for account %s", primary, exc_info=True)

    return ctx


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_template_ctx() -> dict[str, str]:
    """Build template variable context from config.yaml, accounts.yaml, and identity.yaml.

    Returns a dict mapping ``{{key}}`` names to their resolved string values.
    Missing config keys produce empty strings — never raises.

    Available variables:
        name, email, role, company, data_repo       (from config.yaml)
        primary_account, accounts.0-3,
        accounts.all, internal_domain,
        primary_pursuit, example_sf_id              (from accounts.yaml)
        territory, salesforce_user_id               (from <data_repo>/config/identity.yaml)

    Returns:
        Empty dict if config.yaml is absent or unreadable.
    """
    config_fields = _load_config_fields()
    if not config_fields:
        # config.yaml absent — return empty dict per spec FR-3 / contract
        return {}

    account_fields = _load_account_fields()
    identity_fields = _load_identity_fields()

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


def _render_flat_skill_bundle(skill_dir: Path, ctx: dict[str, str]) -> tuple[str, list[UnresolvedVariable]]:
    """Render a Cursor-compatible one-file skill with local Markdown support included.

    Flat targets cannot retain a skill directory. Every Markdown file shipped in
    the skill bundle is therefore appended to the root rule, and relative local
    Markdown links are redirected to the matching in-document anchor. Links
    outside the bundle retain their original destination.
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
                return match.group(0)
            anchor = _flat_support_anchor(relative_paths[candidate])
            return f"](#{anchor})"

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
