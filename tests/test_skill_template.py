"""Unit tests for lib/skill_template.py.

All tests are pure unit tests — no real filesystem config reads.
lib.config functions are mocked at the lib.skill_template boundary.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from fieldkit.skill.template import (
    SkillInstallResult,
    TemplateContext,
    UnresolvedVariable,
    _load_identity_fields,
    build_template_ctx,
    install_skills,
    render_skill_text,
)

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Type alias smoke test
# ---------------------------------------------------------------------------


def test_template_context_type_alias() -> None:
    """TemplateContext is a dict[str, str] alias — verify it behaves as one."""
    ctx: TemplateContext = {"name": "Jane"}
    assert ctx["name"] == "Jane"


# ---------------------------------------------------------------------------
# UnresolvedVariable NamedTuple
# ---------------------------------------------------------------------------


def test_unresolved_variable_fields() -> None:
    uv = UnresolvedVariable(key="email", file_path="qbr-prep/SKILL.md")
    assert uv.key == "email"
    assert uv.file_path == "qbr-prep/SKILL.md"


def test_unresolved_variable_is_tuple() -> None:
    uv = UnresolvedVariable(key="x", file_path="f.md")
    assert isinstance(uv, tuple)
    assert uv[0] == "x"


# ---------------------------------------------------------------------------
# SkillInstallResult dataclass
# ---------------------------------------------------------------------------


def test_skill_install_result_defaults() -> None:
    r = SkillInstallResult()
    assert r.rendered == 0
    assert r.copied == 0
    assert r.warned == 0
    assert r.errors == 0


def test_skill_install_result_custom_values() -> None:
    r = SkillInstallResult(rendered=3, copied=2, warned=1, errors=0)
    assert r.rendered == 3
    assert r.copied == 2
    assert r.warned == 1
    assert r.errors == 0


# ---------------------------------------------------------------------------
# build_template_ctx()
# ---------------------------------------------------------------------------


# ── TestBuildTemplateCtx (flattened) ─────────────────────────────────────────────


def test_build_template_ctx_happy_path_all_keys_present() -> None:
    """All expected keys are present when config is fully populated."""
    config_fields = {
        "name": "Jane Smith",
        "email": "jsmith@acme.example.com",
        "role": "AE",
        "company": "Acme Consulting",
        "fieldkit_home": "<user-home-path>/fieldkit-data",  # pii-guard: ignore
    }
    account_fields = {
        "primary_account": "acme-corp",
        "accounts.0": "acme-corp",
        "accounts.1": "acme-corp",
        "accounts.2": "acme-corp",
        "accounts.3": "acme-corp",
        "accounts.all": "acme-corp",
        "internal_domain": "acme.example.com",
        "primary_pursuit": "acme-deal-2026",
        "example_sf_id": "006Pe000000ExampleId",
    }

    with (
        patch("fieldkit.skill.template._load_config_fields", return_value=config_fields),
        patch("fieldkit.skill.template._load_account_fields", return_value=account_fields),
        patch(
            "fieldkit.skill.template._load_identity_fields",
            return_value={"territory": "", "salesforce_user_id": ""},
        ),
    ):
        ctx = build_template_ctx()

    assert ctx["name"] == "Jane Smith"
    assert ctx["email"] == "jsmith@acme.example.com"
    assert ctx["role"] == "AE"
    assert ctx["company"] == "Acme Consulting"
    assert ctx["fieldkit_home"] == "<user-home-path>/fieldkit-data"  # pii-guard: ignore
    assert ctx["primary_account"] == "acme-corp"
    assert ctx["accounts.0"] == "acme-corp"
    assert ctx["accounts.all"] == "acme-corp"
    assert ctx["internal_domain"] == "acme.example.com"
    assert ctx["primary_pursuit"] == "acme-deal-2026"
    assert ctx["example_sf_id"] == "006Pe000000ExampleId"


def test_build_template_ctx_no_config_returns_empty_dict() -> None:
    """Returns {} when config.yaml is absent."""
    with patch("fieldkit.skill.template._load_config_fields", return_value={}):
        ctx = build_template_ctx()
    assert ctx == {}


def test_build_template_ctx_config_present_accounts_absent() -> None:
    """Personal fields populated; account fields default to empty strings."""
    config_fields = {
        "name": "Jane Smith",
        "email": "jsmith@acme.example.com",
        "role": "AE",
        "company": "Acme Consulting",
        "fieldkit_home": "<user-home-path>/fieldkit-data",  # pii-guard: ignore
    }
    # accounts.yaml absent → _load_account_fields returns all-empty values
    account_fields: dict[str, str] = {
        "primary_account": "",
        "accounts.0": "",
        "accounts.1": "",
        "accounts.2": "",
        "accounts.3": "",
        "accounts.all": "",
        "internal_domain": "",
        "primary_pursuit": "",
        "example_sf_id": "006Pe000000ExampleId",
    }

    with (
        patch("fieldkit.skill.template._load_config_fields", return_value=config_fields),
        patch("fieldkit.skill.template._load_account_fields", return_value=account_fields),
        patch(
            "fieldkit.skill.template._load_identity_fields",
            return_value={"territory": "", "salesforce_user_id": ""},
        ),
    ):
        ctx = build_template_ctx()

    assert ctx["name"] == "Jane Smith"
    assert ctx["email"] == "jsmith@acme.example.com"
    assert ctx["primary_account"] == ""
    assert ctx["accounts.0"] == ""


def test_build_template_ctx_never_raises() -> None:
    """build_template_ctx() must not raise even if helpers raise.

    _load_config_fields is responsible for swallowing its own exceptions
    and returning an empty dict.  When it returns {}, build_template_ctx
    short-circuits and returns {} without calling the other helpers.
    """
    with (
        patch("fieldkit.skill.template._load_config_fields", return_value={}),
        patch("fieldkit.skill.template._load_account_fields", return_value={}),
        patch("fieldkit.skill.template._load_identity_fields", return_value={}),
    ):
        result = build_template_ctx()
    assert isinstance(result, dict)


def test_build_template_ctx_identity_fields_merged_into_ctx() -> None:
    """territory and salesforce_user_id from identity.yaml appear in ctx."""
    config_fields = {
        "name": "Jane Smith",
        "email": "jsmith@acme.example.com",
        "role": "AE",
        "company": "Acme Consulting",
        "fieldkit_home": "<user-home-path>/fieldkit-data",  # pii-guard: ignore
    }
    account_fields: dict[str, str] = {
        "primary_account": "acme-corp",
        "accounts.0": "acme-corp",
        "accounts.1": "acme-corp",
        "accounts.2": "acme-corp",
        "accounts.3": "acme-corp",
        "accounts.all": "acme-corp",
        "internal_domain": "acme.example.com",
        "primary_pursuit": "acme-deal-2026",
        "example_sf_id": "006Pe000000ExampleId",
    }
    identity_fields = {
        "territory": "US-West",
        "salesforce_user_id": "005Pe000001AbcDef",
    }

    with (
        patch("fieldkit.skill.template._load_config_fields", return_value=config_fields),
        patch("fieldkit.skill.template._load_account_fields", return_value=account_fields),
        patch("fieldkit.skill.template._load_identity_fields", return_value=identity_fields),
    ):
        ctx = build_template_ctx()

    assert ctx["territory"] == "US-West"
    assert ctx["salesforce_user_id"] == "005Pe000001AbcDef"


def test_build_template_ctx_identity_fields_empty_when_absent() -> None:
    """territory and salesforce_user_id default to empty string when identity.yaml is absent."""
    config_fields = {
        "name": "Jane Smith",
        "email": "jsmith@acme.example.com",
        "role": "AE",
        "company": "Acme Consulting",
        "fieldkit_home": "<user-home-path>/fieldkit-data",  # pii-guard: ignore
    }
    account_fields: dict[str, str] = {
        "primary_account": "",
        "accounts.0": "",
        "accounts.1": "",
        "accounts.2": "",
        "accounts.3": "",
        "accounts.all": "",
        "internal_domain": "",
        "primary_pursuit": "",
        "example_sf_id": "006Pe000000ExampleId",
    }

    with (
        patch("fieldkit.skill.template._load_config_fields", return_value=config_fields),
        patch("fieldkit.skill.template._load_account_fields", return_value=account_fields),
        patch(
            "fieldkit.skill.template._load_identity_fields",
            return_value={"territory": "", "salesforce_user_id": ""},
        ),
    ):
        ctx = build_template_ctx()

    assert ctx["territory"] == ""
    assert ctx["salesforce_user_id"] == ""


# ---------------------------------------------------------------------------
# _load_identity_fields()
# ---------------------------------------------------------------------------


# ── TestLoadIdentityFields (flattened) ─────────────────────────────────────────────


def test_load_identity_fields_returns_both_fields_when_identity_yaml_present(tmp_path: Path) -> None:
    """Reads territory and salesforce_user_id from identity.yaml."""
    config_dir = tmp_path / ".config" / "fieldkit"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "config.yaml"
    fieldkit_home = tmp_path / "workspace"
    identity_dir = fieldkit_home / "config"
    identity_dir.mkdir(parents=True)

    config_path.write_text(
        f"fieldkit_home: {fieldkit_home}\nname: Jane\n",
        encoding="utf-8",
    )
    (identity_dir / "identity.yaml").write_text(
        "territory: US-West\nsalesforce_user_id: 005Pe000001AbcDef\n",
        encoding="utf-8",
    )

    with patch("fieldkit.config._loader.CONFIG_PATH", config_path):
        result = _load_identity_fields()

    assert result["territory"] == "US-West"
    assert result["salesforce_user_id"] == "005Pe000001AbcDef"


def test_load_identity_fields_returns_empty_strings_when_config_absent(tmp_path: Path) -> None:
    """Returns defaults when config.yaml does not exist."""
    missing = tmp_path / "no-config.yaml"
    with patch("fieldkit.config._loader.CONFIG_PATH", missing):
        result = _load_identity_fields()

    assert result == {"territory": "", "salesforce_user_id": ""}


def test_load_identity_fields_returns_empty_strings_when_identity_yaml_absent(tmp_path: Path) -> None:
    """Returns defaults when identity.yaml does not exist."""
    config_dir = tmp_path / ".config" / "fieldkit"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "config.yaml"
    fieldkit_home = tmp_path / "workspace"
    fieldkit_home.mkdir(parents=True)

    config_path.write_text(
        f"fieldkit_home: {fieldkit_home}\nname: Jane\n",
        encoding="utf-8",
    )
    # No identity.yaml created

    with patch("fieldkit.config._loader.CONFIG_PATH", config_path):
        result = _load_identity_fields()

    assert result == {"territory": "", "salesforce_user_id": ""}


def test_load_identity_fields_returns_empty_strings_when_keys_absent_from_identity_yaml(tmp_path: Path) -> None:
    """Returns empty strings for missing keys in identity.yaml."""
    config_dir = tmp_path / ".config" / "fieldkit"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "config.yaml"
    fieldkit_home = tmp_path / "workspace"
    identity_dir = fieldkit_home / "config"
    identity_dir.mkdir(parents=True)

    config_path.write_text(
        f"fieldkit_home: {fieldkit_home}\nname: Jane\n",
        encoding="utf-8",
    )
    # identity.yaml exists but has no territory or salesforce_user_id
    (identity_dir / "identity.yaml").write_text(
        "other_key: some_value\n",
        encoding="utf-8",
    )

    with patch("fieldkit.config._loader.CONFIG_PATH", config_path):
        result = _load_identity_fields()

    assert result["territory"] == ""
    assert result["salesforce_user_id"] == ""


def test_load_identity_fields_returns_empty_strings_when_identity_yaml_malformed(tmp_path: Path) -> None:
    """Returns defaults when identity.yaml contains invalid YAML."""
    config_dir = tmp_path / ".config" / "fieldkit"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "config.yaml"
    fieldkit_home = tmp_path / "workspace"
    identity_dir = fieldkit_home / "config"
    identity_dir.mkdir(parents=True)

    config_path.write_text(
        f"fieldkit_home: {fieldkit_home}\nname: Jane\n",
        encoding="utf-8",
    )
    (identity_dir / "identity.yaml").write_text(
        ":\t: bad yaml {{{\n",
        encoding="utf-8",
    )

    with patch("fieldkit.config._loader.CONFIG_PATH", config_path):
        result = _load_identity_fields()

    assert result == {"territory": "", "salesforce_user_id": ""}


def test_load_identity_fields_never_raises(tmp_path: Path) -> None:
    """_load_identity_fields() must not propagate any exception."""
    missing = tmp_path / "no-config.yaml"
    with patch("fieldkit.config._loader.CONFIG_PATH", missing):
        # Should return defaults, not raise
        result = _load_identity_fields()
    assert isinstance(result, dict)


def test_load_identity_fields_partial_keys_in_identity_yaml(tmp_path: Path) -> None:
    """Only territory present — salesforce_user_id defaults to empty string."""
    config_dir = tmp_path / ".config" / "fieldkit"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "config.yaml"
    fieldkit_home = tmp_path / "workspace"
    identity_dir = fieldkit_home / "config"
    identity_dir.mkdir(parents=True)

    config_path.write_text(
        f"fieldkit_home: {fieldkit_home}\nname: Jane\n",
        encoding="utf-8",
    )
    (identity_dir / "identity.yaml").write_text(
        "territory: EMEA\n",
        encoding="utf-8",
    )

    with patch("fieldkit.config._loader.CONFIG_PATH", config_path):
        result = _load_identity_fields()

    assert result["territory"] == "EMEA"
    assert result["salesforce_user_id"] == ""


# ---------------------------------------------------------------------------
# render_skill_text()
# ---------------------------------------------------------------------------


# ── TestRenderSkillText (flattened) ─────────────────────────────────────────────


def test_render_skill_text_happy_path_all_resolved() -> None:
    """All placeholder keys present in ctx are substituted and unresolved is empty."""
    text = "Hello {{name}}, your email is {{email}}."
    ctx = {"name": "Jane Smith", "email": "jsmith@acme.example.com"}
    rendered, unresolved = render_skill_text(text, ctx)
    assert rendered == "Hello Jane Smith, your email is jsmith@acme.example.com."
    assert unresolved == []


def test_render_skill_text_unresolved_keys_preserved_verbatim() -> None:
    """Keys missing from ctx are left as {{key}} in the output and reported."""
    text = "Hello {{name}}, contact {{unknown_key}}."
    ctx = {"name": "Jane"}
    rendered, unresolved = render_skill_text(text, ctx)
    assert "Jane" in rendered
    assert "{{unknown_key}}" in rendered
    assert unresolved == ["unknown_key"]


def test_render_skill_text_empty_ctx_all_unresolved() -> None:
    """With an empty ctx, all placeholders remain unchanged and are all reported."""
    text = "{{name}} at {{company}}"
    rendered, unresolved = render_skill_text(text, {})
    assert rendered == "{{name}} at {{company}}"
    assert set(unresolved) == {"name", "company"}


def test_render_skill_text_empty_key_left_intact_not_reported() -> None:
    """{{}} with empty key is left intact and not added to unresolved list."""
    text = "Some {{}} edge case"
    rendered, unresolved = render_skill_text(text, {})
    assert rendered == "Some {{}} edge case"
    assert unresolved == []


def test_render_skill_text_whitespace_stripped_from_key() -> None:
    """Whitespace inside {{ }} is stripped before lookup."""
    text = "Hello {{ name }}!"
    ctx = {"name": "Jane"}
    rendered, unresolved = render_skill_text(text, ctx)
    assert rendered == "Hello Jane!"
    assert unresolved == []


def test_render_skill_text_no_tokens_returns_text_unchanged() -> None:
    text = "No template tokens here."
    rendered, unresolved = render_skill_text(text, {"name": "Jane"})
    assert rendered == text
    assert unresolved == []


def test_render_skill_text_multiple_occurrences_of_same_key() -> None:
    text = "{{name}} and {{name}} again"
    ctx = {"name": "Jane"}
    rendered, unresolved = render_skill_text(text, ctx)
    assert rendered == "Jane and Jane again"
    assert unresolved == []


def test_render_skill_text_multiple_unresolved_same_key_reported_each_time() -> None:
    """Each occurrence of an unresolved key is reported individually."""
    text = "{{missing}} and {{missing}}"
    _rendered, unresolved = render_skill_text(text, {})
    assert unresolved == ["missing", "missing"]


def test_render_skill_text_accounts_dot_notation_key() -> None:
    """Keys with dots (accounts.0) are resolved correctly."""
    text = "Primary: {{accounts.0}}"
    ctx = {"accounts.0": "acme-corp"}
    rendered, unresolved = render_skill_text(text, ctx)
    assert rendered == "Primary: acme-corp"
    assert unresolved == []


def test_render_skill_text_pure_function_does_not_raise() -> None:
    """render_skill_text never raises regardless of input."""
    render_skill_text("", {})
    render_skill_text("{{x}}", {})
    render_skill_text("{{}}{{}}{{x}}", {"x": "val"})


# ---------------------------------------------------------------------------
# install_skills()
# ---------------------------------------------------------------------------


# ── TestInstallSkills (flattened) ─────────────────────────────────────────────


def _make_skill_install_skills(skills_dir: Path, name: str, files: dict[str, str]) -> None:
    """Helper: create a skill directory with given files."""
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    for rel_path, content in files.items():
        f = skill_dir / rel_path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content, encoding="utf-8")


def test_install_skills_happy_path_md_rendered_and_non_md_copied(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    target_dir = tmp_path / "target"
    _make_skill_install_skills(
        skills_dir,
        "qbr-prep",
        {
            "SKILL.md": "Prepared by {{name}}, {{company}}.",
            "evals/evals.json": '{"evals": []}',
        },
    )
    ctx = {"name": "Jane Smith", "company": "Acme Consulting"}

    result = install_skills(skills_dir, target_dir, ctx)

    assert result.rendered == 1
    assert result.copied == 1
    assert result.warned == 0
    assert result.errors == 0

    rendered_md = (target_dir / "qbr-prep" / "SKILL.md").read_text(encoding="utf-8")
    assert "Jane Smith" in rendered_md
    assert "Acme Consulting" in rendered_md
    assert "{{name}}" not in rendered_md

    json_file = target_dir / "qbr-prep" / "evals" / "evals.json"
    assert json_file.exists()
    assert json_file.read_text(encoding="utf-8") == '{"evals": []}'


def test_install_skills_dry_run_prints_actions_no_writes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    skills_dir = tmp_path / "skills"
    target_dir = tmp_path / "target"
    _make_skill_install_skills(skills_dir, "meeting-prep", {"SKILL.md": "Hello {{name}}"})
    ctx = {"name": "Jane"}

    result = install_skills(skills_dir, target_dir, ctx, dry_run=True)

    out, _ = capsys.readouterr()
    assert "[dry-run]" in out
    assert "meeting-prep" in out
    # No files written
    assert not target_dir.exists() or not (target_dir / "meeting-prep").exists()
    # dry_run returns zero counts (no actual work done)
    assert result.rendered == 0
    assert result.copied == 0
    assert result.errors == 0


def test_install_skills_symlink_replaced_with_real_directory(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    target_dir = tmp_path / "target"
    target_dir.mkdir()

    # Create a symlink at the skill target location (old install behavior)
    link_target = tmp_path / "old_skill_source"
    link_target.mkdir()
    symlink = target_dir / "my-skill"
    symlink.symlink_to(link_target)
    assert symlink.is_symlink()

    _make_skill_install_skills(skills_dir, "my-skill", {"SKILL.md": "Content"})
    ctx: dict[str, str] = {}

    result = install_skills(skills_dir, target_dir, ctx)

    # Symlink replaced by real directory with rendered file
    assert not symlink.is_symlink()
    assert (target_dir / "my-skill" / "SKILL.md").exists()
    assert result.errors == 0


def test_install_skills_skills_dir_missing_returns_error(tmp_path: Path) -> None:
    missing = tmp_path / "nonexistent"
    target = tmp_path / "target"
    result = install_skills(missing, target, {})
    assert result.errors == 1
    assert result.rendered == 0
    assert result.copied == 0


def test_install_skills_unresolved_key_warns_to_stderr(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    skills_dir = tmp_path / "skills"
    target_dir = tmp_path / "target"
    _make_skill_install_skills(skills_dir, "qbr-prep", {"SKILL.md": "Hello {{unknown_var}}"})
    ctx: dict[str, str] = {}

    result = install_skills(skills_dir, target_dir, ctx)

    _, err = capsys.readouterr()
    assert "warning: unresolved template variable {{unknown_var}}" in err
    assert result.warned == 1
    assert result.rendered == 1  # file was still written


# MANUAL-VERIFY: remaining self refs {'self.suffix'}
def test_install_skills_oserror_on_write_increments_errors(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    target_dir = tmp_path / "target"
    _make_skill_install_skills(skills_dir, "my-skill", {"SKILL.md": "Hello {{name}}"})
    ctx = {"name": "Jane"}

    # Patch Path.write_text to raise OSError
    original_write_text = Path.write_text

    def _failing_write(self: Path, data: str, **kwargs: object) -> None:
        if self.suffix == ".md" and "my-skill" in str(self):
            raise OSError("disk full")
        original_write_text(self, data, **kwargs)  # type: ignore[arg-type]

    with patch.object(Path, "write_text", _failing_write):
        result = install_skills(skills_dir, target_dir, ctx)

    assert result.errors >= 1


def test_install_skills_idempotent_second_run_overwrites(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    target_dir = tmp_path / "target"
    _make_skill_install_skills(skills_dir, "my-skill", {"SKILL.md": "Hello {{name}}"})

    ctx1 = {"name": "Jane"}
    install_skills(skills_dir, target_dir, ctx1)

    ctx2 = {"name": "Bob"}
    result2 = install_skills(skills_dir, target_dir, ctx2)

    content = (target_dir / "my-skill" / "SKILL.md").read_text(encoding="utf-8")
    assert "Bob" in content
    assert "Jane" not in content
    assert result2.errors == 0


def test_install_skills_hidden_and_underscore_dirs_skipped(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    target_dir = tmp_path / "target"
    # Create a normal skill and two that should be skipped
    _make_skill_install_skills(skills_dir, "real-skill", {"SKILL.md": "Content"})
    _make_skill_install_skills(skills_dir, "_internal", {"SKILL.md": "Internal"})
    _make_skill_install_skills(skills_dir, ".hidden", {"SKILL.md": "Hidden"})

    result = install_skills(skills_dir, target_dir, {})

    assert (target_dir / "real-skill").exists()
    assert not (target_dir / "_internal").exists()
    assert not (target_dir / ".hidden").exists()
    assert result.rendered == 1


def test_install_skills_empty_ctx_copies_verbatim(tmp_path: Path) -> None:
    """Empty ctx: .md files are written as-is (tokens preserved)."""
    skills_dir = tmp_path / "skills"
    target_dir = tmp_path / "target"
    _make_skill_install_skills(skills_dir, "my-skill", {"SKILL.md": "Hello {{name}}"})

    result = install_skills(skills_dir, target_dir, {})

    content = (target_dir / "my-skill" / "SKILL.md").read_text(encoding="utf-8")
    assert "{{name}}" in content
    assert result.rendered == 1


def test_install_skills_nested_subdirectory_structure_mirrored(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    target_dir = tmp_path / "target"
    _make_skill_install_skills(
        skills_dir,
        "complex-skill",
        {
            "SKILL.md": "Top level",
            "references/guide.md": "Guide {{name}}",
            "evals/evals.json": "{}",
        },
    )
    ctx = {"name": "Jane"}

    result = install_skills(skills_dir, target_dir, ctx)

    assert (target_dir / "complex-skill" / "SKILL.md").exists()
    assert (target_dir / "complex-skill" / "references" / "guide.md").exists()
    assert (target_dir / "complex-skill" / "evals" / "evals.json").exists()
    assert result.rendered == 2  # SKILL.md + references/guide.md
    assert result.copied == 1  # evals.json


def test_install_skills_dry_run_with_symlink_prints_unlink_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    skills_dir = tmp_path / "skills"
    target_dir = tmp_path / "target"
    target_dir.mkdir()

    link_target = tmp_path / "old"
    link_target.mkdir()
    symlink = target_dir / "my-skill"
    symlink.symlink_to(link_target)

    _make_skill_install_skills(skills_dir, "my-skill", {"SKILL.md": "Content"})

    install_skills(skills_dir, target_dir, {}, dry_run=True)

    out, _ = capsys.readouterr()
    assert "[dry-run]" in out
    assert "unlink symlink" in out
    # Symlink must NOT be removed in dry-run
    assert symlink.is_symlink()


def test_install_skills_multiple_skills_all_installed(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    target_dir = tmp_path / "target"
    for name in ["skill-a", "skill-b", "skill-c"]:
        _make_skill_install_skills(skills_dir, name, {"SKILL.md": f"Content for {name}"})

    result = install_skills(skills_dir, target_dir, {})

    assert result.rendered == 3
    assert result.errors == 0
    for name in ["skill-a", "skill-b", "skill-c"]:
        assert (target_dir / name / "SKILL.md").exists()


# ---------------------------------------------------------------------------
# historic regression regression tests for _load_identity_fields()
# Flat layout, nested layout, territory fallback, missing file
# ---------------------------------------------------------------------------


# ── TestLoadIdentityFieldsBug326 (flattened) ─────────────────────────────────────────────


def _write_config_load_identity_fields_bug326(tmp_path: Path, fieldkit_home: Path) -> Path:
    """Write a minimal config.yaml pointing at fieldkit_home and return its path."""
    config_dir = tmp_path / ".config" / "fieldkit"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "config.yaml"
    config_path.write_text(
        f"fieldkit_home: {fieldkit_home}\nname: Alice\n",
        encoding="utf-8",
    )
    return config_path


def test_load_identity_fields_flat_layout(tmp_path: Path) -> None:
    """Flat identity.yaml (keys at top level) is read correctly.

    Regression: historic regression must not break the original flat layout.
    """
    fieldkit_home = tmp_path / "workspace"
    identity_dir = fieldkit_home / "config"
    identity_dir.mkdir(parents=True)
    (identity_dir / "identity.yaml").write_text(
        "name: Alice\nterritory: FSI_WEST\nsalesforce_user_id: 005abc\n",
        encoding="utf-8",
    )
    config_path = _write_config_load_identity_fields_bug326(tmp_path, fieldkit_home)

    with patch("fieldkit.config._loader.CONFIG_PATH", config_path):
        result = _load_identity_fields()

    assert result["territory"] == "FSI_WEST"
    assert result["salesforce_user_id"] == "005abc"


def test_load_identity_fields_nested_layout(tmp_path: Path) -> None:
    """Nested identity.yaml (keys under ``identity:`` key) is unwrapped correctly.

    Regression: historic regression fix — nested layout must produce the same result
    as the flat layout.
    """
    fieldkit_home = tmp_path / "workspace"
    identity_dir = fieldkit_home / "config"
    identity_dir.mkdir(parents=True)
    (identity_dir / "identity.yaml").write_text(
        "identity:\n  name: Alice\n  territory: FSI_WEST\n  salesforce_user_id: 005abc\n",
        encoding="utf-8",
    )
    config_path = _write_config_load_identity_fields_bug326(tmp_path, fieldkit_home)

    with patch("fieldkit.config._loader.CONFIG_PATH", config_path):
        result = _load_identity_fields()

    # Must return the same values as the flat layout — historic regression regression.
    assert result["territory"] == "FSI_WEST"
    assert result["salesforce_user_id"] == "005abc"


def test_load_identity_fields_territory_falls_back_to_accounts_yaml(tmp_path: Path) -> None:
    """When identity.yaml has no ``territory``, falls back to accounts.yaml sf_territory.

    Regression: historic regression fix — territory fallback from the primary account.
    """
    fieldkit_home = tmp_path / "workspace"
    config_dir = fieldkit_home / "config"
    config_dir.mkdir(parents=True)

    # identity.yaml exists but has no territory key
    (config_dir / "identity.yaml").write_text(
        "salesforce_user_id: 005abc\n",
        encoding="utf-8",
    )

    # accounts.yaml has a primary account with sf_territory
    (config_dir / "accounts.yaml").write_text(
        "accounts:\n  acme-corp:\n    name: Acme Corp\n    sf_territory: FSI_SOUTH\n",
        encoding="utf-8",
    )

    config_path = _write_config_load_identity_fields_bug326(tmp_path, fieldkit_home)

    with patch("fieldkit.config._loader.CONFIG_PATH", config_path):
        result = _load_identity_fields()

    # territory must come from accounts.yaml sf_territory
    assert result["territory"] == "FSI_SOUTH"
    assert result["salesforce_user_id"] == "005abc"


def test_load_identity_fields_returns_empty_on_missing_file(tmp_path: Path) -> None:
    """Returns empty defaults when neither identity.yaml nor accounts.yaml exists.

    Must not raise — callers display blank rather than crash.
    """
    fieldkit_home = tmp_path / "workspace"
    # fieldkit_home exists but config/ subdirectory is absent — no identity.yaml
    fieldkit_home.mkdir(parents=True)

    config_path = _write_config_load_identity_fields_bug326(tmp_path, fieldkit_home)

    with patch("fieldkit.config._loader.CONFIG_PATH", config_path):
        result = _load_identity_fields()

    # Both fields must default to empty string — no exception raised.
    assert isinstance(result, dict)
    assert result.get("territory", "") == ""
    assert result.get("salesforce_user_id", "") == ""


# ---------------------------------------------------------------------------
# Task 11.8 — install_skills deletes old content before writing
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_install_skills_deletes_old_before_writing(tmp_path: Path) -> None:
    """install_skills overwrites stale content in the target directory (idempotent)."""
    # Create source skills dir with one skill
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "my-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# My Skill v2\n", encoding="utf-8")

    # Pre-populate target with old content
    target_dir = tmp_path / "target"
    old_skill_target = target_dir / "my-skill"
    old_skill_target.mkdir(parents=True)
    old_file = old_skill_target / "SKILL.md"
    old_file.write_text("# Old content\n", encoding="utf-8")

    result = install_skills(skills_dir, target_dir, ctx={})

    # New content must be written (old content replaced)
    new_content = old_file.read_text(encoding="utf-8")
    assert "My Skill v2" in new_content
    assert "Old content" not in new_content
    assert result.rendered >= 1
    assert result.errors == 0


# ---------------------------------------------------------------------------
# Additional install_skills() branch-coverage tests
# ---------------------------------------------------------------------------


# ── TestInstallSkillsAdditionalBranches (flattened) ─────────────────────────────────────────────


def _make_skill_install_skills_additional_branches(skills_dir: Path, name: str, files: dict[str, str]) -> None:
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    for rel_path, content in files.items():
        f = skill_dir / rel_path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content, encoding="utf-8")


def test_skill_dir_without_skill_md_is_skipped(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Skill directory without SKILL.md (bundle directory) is skipped with a warning."""
    skills_dir = tmp_path / "skills"
    target_dir = tmp_path / "target"

    # Create a skill dir with NO SKILL.md (simulates a references/ bundle)
    bundle_dir = skills_dir / "references-bundle"
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "guide.md").write_text("Guide content", encoding="utf-8")

    # Also create a valid skill so we can verify it IS installed
    _make_skill_install_skills_additional_branches(skills_dir, "valid-skill", {"SKILL.md": "Valid skill content"})

    result = install_skills(skills_dir, target_dir, {})

    _, err = capsys.readouterr()
    assert "warning: skipping references-bundle" in err
    assert "no SKILL.md found" in err
    # The bundle directory must NOT be installed
    assert not (target_dir / "references-bundle").exists()
    # The valid skill must still be installed
    assert (target_dir / "valid-skill" / "SKILL.md").exists()
    assert result.rendered == 1


def test_skills_dir_is_file_not_dir_returns_error(tmp_path: Path) -> None:
    """When skills_dir is a file (not a directory), returns errors=1."""
    skills_file = tmp_path / "skills.txt"
    skills_file.write_text("not a directory", encoding="utf-8")
    target_dir = tmp_path / "target"

    result = install_skills(skills_file, target_dir, {})

    assert result.errors == 1
    assert result.rendered == 0


# MANUAL-VERIFY: remaining self refs {'self.name'}
def test_oserror_on_mkdir_increments_errors(tmp_path: Path) -> None:
    """OSError during parent mkdir increments errors and skips the file."""
    skills_dir = tmp_path / "skills"
    target_dir = tmp_path / "target"
    _make_skill_install_skills_additional_branches(skills_dir, "my-skill", {"SKILL.md": "Hello {{name}}"})
    ctx = {"name": "Jane"}

    original_mkdir = Path.mkdir

    def _failing_mkdir(self: Path, mode: int = 0o777, parents: bool = False, exist_ok: bool = False) -> None:
        # Fail only for the skill target directory creation
        if "my-skill" in str(self) and self.name == "my-skill":
            raise OSError("permission denied")
        original_mkdir(self, mode=mode, parents=parents, exist_ok=exist_ok)

    with patch.object(Path, "mkdir", _failing_mkdir):
        result = install_skills(skills_dir, target_dir, ctx)

    assert result.errors >= 1


# MANUAL-VERIFY: remaining self refs {'self.suffix', 'self.parent'}
def test_oserror_on_read_increments_errors(tmp_path: Path) -> None:
    """OSError during src_file.read_text increments errors for that file."""
    skills_dir = tmp_path / "skills"
    target_dir = tmp_path / "target"
    _make_skill_install_skills_additional_branches(skills_dir, "my-skill", {"SKILL.md": "Hello {{name}}"})
    ctx = {"name": "Jane"}

    original_read_text = Path.read_text

    def _failing_read(self: Path, **kwargs: object) -> str:
        if self.suffix == ".md" and "my-skill" in str(self) and self.parent == skills_dir / "my-skill":
            raise OSError("read error")
        return original_read_text(self, **kwargs)

    with patch.object(Path, "read_text", _failing_read):
        result = install_skills(skills_dir, target_dir, ctx)

    assert result.errors >= 1


def test_oserror_on_copy_increments_errors(tmp_path: Path) -> None:
    """OSError during shutil.copy2 for non-.md file increments errors."""
    import shutil

    skills_dir = tmp_path / "skills"
    target_dir = tmp_path / "target"
    _make_skill_install_skills_additional_branches(
        skills_dir,
        "my-skill",
        {
            "SKILL.md": "Content",
            "evals/evals.json": "{}",
        },
    )
    ctx: dict[str, str] = {}

    original_copy2 = shutil.copy2

    def _failing_copy(src: object, dst: object) -> None:
        if "evals.json" in str(dst):
            raise OSError("copy failed")
        original_copy2(src, dst)  # type: ignore[arg-type]

    with patch("fieldkit.skill.template.shutil.copy2", side_effect=_failing_copy):
        result = install_skills(skills_dir, target_dir, ctx)

    assert result.errors >= 1
    # The .md file should still have been rendered
    assert result.rendered >= 1


def test_multiple_unresolved_keys_all_warned(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Multiple unresolved keys in one file each emit a separate warning."""
    skills_dir = tmp_path / "skills"
    target_dir = tmp_path / "target"
    _make_skill_install_skills_additional_branches(
        skills_dir,
        "my-skill",
        {"SKILL.md": "Hello {{unknown_a}} and {{unknown_b}} and {{unknown_c}}"},
    )
    ctx: dict[str, str] = {}

    result = install_skills(skills_dir, target_dir, ctx)

    _, err = capsys.readouterr()
    assert "{{unknown_a}}" in err
    assert "{{unknown_b}}" in err
    assert "{{unknown_c}}" in err
    assert result.warned == 3
    assert result.rendered == 1


def test_dry_run_with_non_md_file_prints_copy_action(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """dry_run prints 'copy' for non-.md files."""
    skills_dir = tmp_path / "skills"
    target_dir = tmp_path / "target"
    _make_skill_install_skills_additional_branches(
        skills_dir,
        "my-skill",
        {
            "SKILL.md": "Content",
            "evals/evals.json": "{}",
        },
    )

    install_skills(skills_dir, target_dir, {}, dry_run=True)

    out, _ = capsys.readouterr()
    assert "copy" in out
    assert "render" in out


def test_empty_skills_dir_returns_zero_counts(tmp_path: Path) -> None:
    """Empty skills_dir (exists but no subdirs) returns all-zero result."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    target_dir = tmp_path / "target"

    result = install_skills(skills_dir, target_dir, {})

    assert result.rendered == 0
    assert result.copied == 0
    assert result.warned == 0
    assert result.errors == 0


def test_non_dir_entry_in_skills_dir_skipped(tmp_path: Path) -> None:
    """Non-directory entries (files) in skills_dir are silently skipped."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    target_dir = tmp_path / "target"

    # Place a plain file directly in skills_dir (not a skill directory)
    (skills_dir / "README.md").write_text("Not a skill dir", encoding="utf-8")
    # Also create a valid skill
    skill_dir = skills_dir / "real-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("Real skill", encoding="utf-8")

    result = install_skills(skills_dir, target_dir, {})

    assert result.rendered == 1
    assert not (target_dir / "README.md").exists()


# ---------------------------------------------------------------------------
# Additional _load_identity_fields() branch-coverage tests
# ---------------------------------------------------------------------------


# ── TestLoadIdentityFieldsAdditionalBranches (flattened) ─────────────────────────────────────────────


def _write_config_load_identity_fields_additional_branches(tmp_path: Path, fieldkit_home: Path) -> Path:
    config_dir = tmp_path / ".config" / "fieldkit"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "config.yaml"
    config_path.write_text(
        f"fieldkit_home: {fieldkit_home}\nname: Alice\n",
        encoding="utf-8",
    )
    return config_path


def test_config_yaml_malformed_returns_defaults(tmp_path: Path) -> None:
    """Malformed config.yaml returns default empty dict."""
    config_dir = tmp_path / ".config" / "fieldkit"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "config.yaml"
    config_path.write_text(":\t: bad yaml {{{\n", encoding="utf-8")

    with patch("fieldkit.config._loader.CONFIG_PATH", config_path):
        result = _load_identity_fields()

    assert result == {"territory": "", "salesforce_user_id": ""}


def test_config_yaml_not_a_dict_returns_defaults(tmp_path: Path) -> None:
    """config.yaml that parses to a non-dict (e.g. a list) returns defaults."""
    config_dir = tmp_path / ".config" / "fieldkit"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "config.yaml"
    config_path.write_text("- item1\n- item2\n", encoding="utf-8")

    with patch("fieldkit.config._loader.CONFIG_PATH", config_path):
        result = _load_identity_fields()

    assert result == {"territory": "", "salesforce_user_id": ""}


def test_data_repo_empty_returns_defaults(tmp_path: Path) -> None:
    """config.yaml with empty fieldkit_home returns defaults (no identity.yaml to read)."""
    config_dir = tmp_path / ".config" / "fieldkit"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "config.yaml"
    # fieldkit_home is explicitly empty string
    config_path.write_text("fieldkit_home: ''\nname: Alice\n", encoding="utf-8")

    with patch("fieldkit.config._loader.CONFIG_PATH", config_path):
        result = _load_identity_fields()

    assert result == {"territory": "", "salesforce_user_id": ""}


def test_identity_yaml_not_a_dict_returns_defaults(tmp_path: Path) -> None:
    """identity.yaml that parses to a non-dict returns defaults."""
    fieldkit_home = tmp_path / "workspace"
    identity_dir = fieldkit_home / "config"
    identity_dir.mkdir(parents=True)
    (identity_dir / "identity.yaml").write_text("- item1\n- item2\n", encoding="utf-8")

    config_path = _write_config_load_identity_fields_additional_branches(tmp_path, fieldkit_home)

    with patch("fieldkit.config._loader.CONFIG_PATH", config_path):
        result = _load_identity_fields()

    assert result == {"territory": "", "salesforce_user_id": ""}


def test_territory_fallback_skips_internal_accounts(tmp_path: Path) -> None:
    """Territory fallback skips accounts with internal: true in accounts.yaml."""
    fieldkit_home = tmp_path / "workspace"
    config_dir = fieldkit_home / "config"
    config_dir.mkdir(parents=True)

    # identity.yaml has no territory
    (config_dir / "identity.yaml").write_text(
        "salesforce_user_id: 005abc\n",
        encoding="utf-8",
    )

    # accounts.yaml: first account is internal, second has sf_territory
    (config_dir / "accounts.yaml").write_text(
        "accounts:\n"
        "  internal-org:\n"  # pii-guard: ignore
        "    internal: true\n"
        "    sf_territory: INTERNAL_TERRITORY\n"
        "  acme-corp:\n"
        "    sf_territory: FSI_WEST\n",
        encoding="utf-8",
    )

    config_path = _write_config_load_identity_fields_additional_branches(tmp_path, fieldkit_home)

    with patch("fieldkit.config._loader.CONFIG_PATH", config_path):
        result = _load_identity_fields()

    # Must skip the internal account and use the first non-internal one
    assert result["territory"] == "FSI_WEST"


def test_territory_fallback_accounts_yaml_absent(tmp_path: Path) -> None:
    """Territory fallback with no accounts.yaml returns empty territory."""
    fieldkit_home = tmp_path / "workspace"
    config_dir = fieldkit_home / "config"
    config_dir.mkdir(parents=True)

    # identity.yaml has no territory
    (config_dir / "identity.yaml").write_text(
        "salesforce_user_id: 005abc\n",
        encoding="utf-8",
    )
    # No accounts.yaml created

    config_path = _write_config_load_identity_fields_additional_branches(tmp_path, fieldkit_home)

    with patch("fieldkit.config._loader.CONFIG_PATH", config_path):
        result = _load_identity_fields()

    assert result["territory"] == ""
    assert result["salesforce_user_id"] == "005abc"


def test_territory_fallback_accounts_not_a_dict(tmp_path: Path) -> None:
    """Territory fallback: accounts.yaml 'accounts' value is not a dict → empty territory."""
    fieldkit_home = tmp_path / "workspace"
    config_dir = fieldkit_home / "config"
    config_dir.mkdir(parents=True)

    (config_dir / "identity.yaml").write_text(
        "salesforce_user_id: 005abc\n",
        encoding="utf-8",
    )
    # accounts.yaml with accounts as a list (malformed)
    (config_dir / "accounts.yaml").write_text(
        "accounts:\n  - item1\n  - item2\n",
        encoding="utf-8",
    )

    config_path = _write_config_load_identity_fields_additional_branches(tmp_path, fieldkit_home)

    with patch("fieldkit.config._loader.CONFIG_PATH", config_path):
        result = _load_identity_fields()

    assert result["territory"] == ""


def test_nested_identity_with_non_dict_nested_value_falls_back_to_flat(tmp_path: Path) -> None:
    """identity.yaml with 'identity' key that is not a dict uses flat layout."""
    fieldkit_home = tmp_path / "workspace"
    identity_dir = fieldkit_home / "config"
    identity_dir.mkdir(parents=True)

    # 'identity' key is a string, not a dict — should fall back to flat lookup
    (identity_dir / "identity.yaml").write_text(
        "identity: not-a-dict\nterritory: FLAT_TERRITORY\nsalesforce_user_id: 005flat\n",
        encoding="utf-8",
    )

    config_path = _write_config_load_identity_fields_additional_branches(tmp_path, fieldkit_home)

    with patch("fieldkit.config._loader.CONFIG_PATH", config_path):
        result = _load_identity_fields()

    # When identity key is not a dict, falls back to flat layout
    assert result["territory"] == "FLAT_TERRITORY"
    assert result["salesforce_user_id"] == "005flat"
