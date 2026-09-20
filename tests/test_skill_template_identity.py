"""Tests for fieldkit.skill.template — _load_identity_fields and install_skill_dir branches.

Covers missing keys, flat/nested layouts, territory fallback from accounts.yaml,
and install_skill_dir error paths not covered in test_skill_template.py.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from fieldkit.skill.template import (
    _load_identity_fields,
    install_skill_dir,
)
from tests.conftest import skip_if_root

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_config(tmp_path: Path, data_repo: Path) -> Path:
    """Write a minimal config.yaml pointing to data_repo and return config path."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"data_repo: {data_repo}\n", encoding="utf-8")
    return cfg


def _patch_config(monkeypatch: pytest.MonkeyPatch, cfg_path: Path) -> None:
    """Patch CONFIG_PATH inside fieldkit.config._loader to cfg_path."""
    import fieldkit.config._loader as _impl

    monkeypatch.setattr(_impl, "CONFIG_PATH", cfg_path)


# ---------------------------------------------------------------------------
# _load_identity_fields — no config.yaml
# ---------------------------------------------------------------------------


# ── TestLoadIdentityFieldsNoConfig (flattened) ──────────────────────────────


def test_load_identity_fields_no_config_returns_defaults_when_config_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "nonexistent-config.yaml"
    _patch_config(monkeypatch, missing)

    result = _load_identity_fields()

    assert result == {"territory": "", "salesforce_user_id": ""}


def test_load_identity_fields_no_config_returns_defaults_on_yaml_parse_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(":[invalid yaml:\n  broken: {\n", encoding="utf-8")
    _patch_config(monkeypatch, cfg)

    result = _load_identity_fields()

    assert result == {"territory": "", "salesforce_user_id": ""}


def test_load_identity_fields_no_config_returns_defaults_when_config_not_a_dict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("- item1\n- item2\n", encoding="utf-8")
    _patch_config(monkeypatch, cfg)

    result = _load_identity_fields()

    assert result == {"territory": "", "salesforce_user_id": ""}


def test_load_identity_fields_no_config_returns_defaults_when_no_data_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("name: Alice\n", encoding="utf-8")
    _patch_config(monkeypatch, cfg)

    result = _load_identity_fields()

    assert result == {"territory": "", "salesforce_user_id": ""}


# ---------------------------------------------------------------------------
# _load_identity_fields — identity.yaml absent
# ---------------------------------------------------------------------------


# ── TestLoadIdentityFieldsNoIdentityFile (flattened) ────────────────────────


def test_load_identity_fields_no_identity_file_returns_defaults_when_identity_yaml_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_repo = tmp_path / "fieldkit-data"
    data_repo.mkdir()
    cfg = _write_config(tmp_path, data_repo)
    _patch_config(monkeypatch, cfg)
    # No config/identity.yaml created

    result = _load_identity_fields()

    assert result == {"territory": "", "salesforce_user_id": ""}


# ---------------------------------------------------------------------------
# _load_identity_fields — identity.yaml flat layout
# ---------------------------------------------------------------------------


# ── TestLoadIdentityFieldsFlatLayout (flattened) ────────────────────────────


def test_load_identity_fields_flat_layout_flat_layout_both_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_repo = tmp_path / "fieldkit-data"
    (data_repo / "config").mkdir(parents=True)
    (data_repo / "config" / "identity.yaml").write_text(
        "territory: West\nsalesforce_user_id: 005ABC123456789\n",
        encoding="utf-8",
    )
    cfg = _write_config(tmp_path, data_repo)
    _patch_config(monkeypatch, cfg)

    result = _load_identity_fields()

    assert result["territory"] == "West"
    assert result["salesforce_user_id"] == "005ABC123456789"


def test_load_identity_fields_flat_layout_flat_layout_only_salesforce_user_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_repo = tmp_path / "fieldkit-data"
    (data_repo / "config").mkdir(parents=True)
    (data_repo / "config" / "identity.yaml").write_text(
        "salesforce_user_id: 005XYZ\n",
        encoding="utf-8",
    )
    cfg = _write_config(tmp_path, data_repo)
    _patch_config(monkeypatch, cfg)

    result = _load_identity_fields()

    assert result["salesforce_user_id"] == "005XYZ"
    assert result["territory"] == ""


def test_load_identity_fields_flat_layout_flat_layout_territory_empty_triggers_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When territory is absent from identity.yaml, fall back to accounts.yaml sf_territory."""
    data_repo = tmp_path / "fieldkit-data"
    cfg_dir = data_repo / "config"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "identity.yaml").write_text("salesforce_user_id: 005ABC\n", encoding="utf-8")
    (cfg_dir / "accounts.yaml").write_text(
        "accounts:\n  acme:\n    sf_territory: North\n",
        encoding="utf-8",
    )
    cfg = _write_config(tmp_path, data_repo)
    _patch_config(monkeypatch, cfg)

    result = _load_identity_fields()

    assert result["territory"] == "North"
    assert result["salesforce_user_id"] == "005ABC"


# ---------------------------------------------------------------------------
# _load_identity_fields — nested layout (identity: {...})
# ---------------------------------------------------------------------------


# ── TestLoadIdentityFieldsNestedLayout (flattened) ──────────────────────────


def test_load_identity_fields_nested_layout_nested_layout_both_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_repo = tmp_path / "fieldkit-data"
    (data_repo / "config").mkdir(parents=True)
    (data_repo / "config" / "identity.yaml").write_text(
        "identity:\n  territory: East\n  salesforce_user_id: 005NESTED\n",
        encoding="utf-8",
    )
    cfg = _write_config(tmp_path, data_repo)
    _patch_config(monkeypatch, cfg)

    result = _load_identity_fields()

    assert result["territory"] == "East"
    assert result["salesforce_user_id"] == "005NESTED"


def test_load_identity_fields_nested_layout_nested_layout_identity_key_not_a_dict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """identity: <scalar> — falls back to treating top-level as lookup dict."""
    data_repo = tmp_path / "fieldkit-data"
    (data_repo / "config").mkdir(parents=True)
    (data_repo / "config" / "identity.yaml").write_text(
        "identity: not-a-dict\nsalesforce_user_id: 005TOP\n",
        encoding="utf-8",
    )
    cfg = _write_config(tmp_path, data_repo)
    _patch_config(monkeypatch, cfg)

    result = _load_identity_fields()

    # identity is not a dict, so lookup uses top-level
    assert result["salesforce_user_id"] == "005TOP"


# ---------------------------------------------------------------------------
# _load_identity_fields — identity.yaml parse errors
# ---------------------------------------------------------------------------


# ── TestLoadIdentityFieldsParseErrors (flattened) ───────────────────────────


def test_load_identity_fields_parse_errors_identity_yaml_parse_error_returns_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_repo = tmp_path / "fieldkit-data"
    (data_repo / "config").mkdir(parents=True)
    (data_repo / "config" / "identity.yaml").write_text(":[bad yaml\n", encoding="utf-8")
    cfg = _write_config(tmp_path, data_repo)
    _patch_config(monkeypatch, cfg)

    result = _load_identity_fields()

    assert result == {"territory": "", "salesforce_user_id": ""}


def test_load_identity_fields_parse_errors_identity_yaml_not_a_dict_returns_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_repo = tmp_path / "fieldkit-data"
    (data_repo / "config").mkdir(parents=True)
    (data_repo / "config" / "identity.yaml").write_text("- a\n- b\n", encoding="utf-8")
    cfg = _write_config(tmp_path, data_repo)
    _patch_config(monkeypatch, cfg)

    result = _load_identity_fields()

    assert result == {"territory": "", "salesforce_user_id": ""}


# ---------------------------------------------------------------------------
# _load_identity_fields — territory fallback from accounts.yaml
# ---------------------------------------------------------------------------


# ── TestLoadIdentityFieldsTerritoryFallback (flattened) ─────────────────────


def test_load_identity_fields_territory_fallback_skips_internal_accounts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Internal accounts (internal: true) must be skipped in territory fallback."""
    data_repo = tmp_path / "fieldkit-data"
    cfg_dir = data_repo / "config"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "identity.yaml").write_text("salesforce_user_id: 005X\n", encoding="utf-8")
    (cfg_dir / "accounts.yaml").write_text(
        "accounts:\n"
        "  internal-team:\n"
        "    internal: true\n"
        "    sf_territory: ShouldNotAppear\n"
        "  external-acme:\n"
        "    sf_territory: Southwest\n",
        encoding="utf-8",
    )
    cfg = _write_config(tmp_path, data_repo)
    _patch_config(monkeypatch, cfg)

    result = _load_identity_fields()

    assert result["territory"] == "Southwest"


def test_load_identity_fields_territory_fallback_accounts_yaml_absent_territory_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No accounts.yaml → territory stays empty string."""
    data_repo = tmp_path / "fieldkit-data"
    cfg_dir = data_repo / "config"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "identity.yaml").write_text("salesforce_user_id: 005Y\n", encoding="utf-8")
    # No accounts.yaml
    cfg = _write_config(tmp_path, data_repo)
    _patch_config(monkeypatch, cfg)

    result = _load_identity_fields()

    assert result["territory"] == ""


def test_load_identity_fields_territory_fallback_accounts_yaml_no_sf_territory_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Accounts without sf_territory key are skipped."""
    data_repo = tmp_path / "fieldkit-data"
    cfg_dir = data_repo / "config"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "identity.yaml").write_text("salesforce_user_id: 005Z\n", encoding="utf-8")
    (cfg_dir / "accounts.yaml").write_text(
        "accounts:\n  acme:\n    stage: discover\n",
        encoding="utf-8",
    )
    cfg = _write_config(tmp_path, data_repo)
    _patch_config(monkeypatch, cfg)

    result = _load_identity_fields()

    assert result["territory"] == ""


def test_load_identity_fields_territory_fallback_accounts_yaml_non_dict_account_info_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Account info that is not a dict must be skipped without crash."""
    data_repo = tmp_path / "fieldkit-data"
    cfg_dir = data_repo / "config"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "identity.yaml").write_text("salesforce_user_id: 005Z\n", encoding="utf-8")
    (cfg_dir / "accounts.yaml").write_text(
        "accounts:\n  acme: null\n  globalpay:\n    sf_territory: Southeast\n",
        encoding="utf-8",
    )
    cfg = _write_config(tmp_path, data_repo)
    _patch_config(monkeypatch, cfg)

    result = _load_identity_fields()

    assert result["territory"] == "Southeast"


def test_load_identity_fields_territory_fallback_accounts_not_a_dict_does_not_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_repo = tmp_path / "fieldkit-data"
    cfg_dir = data_repo / "config"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "identity.yaml").write_text("salesforce_user_id: 005Z\n", encoding="utf-8")
    (cfg_dir / "accounts.yaml").write_text("accounts:\n  - list\n  - format\n", encoding="utf-8")
    cfg = _write_config(tmp_path, data_repo)
    _patch_config(monkeypatch, cfg)

    result = _load_identity_fields()

    assert result["territory"] == ""


# ---------------------------------------------------------------------------
# install_skill_dir — edge cases and error paths
# ---------------------------------------------------------------------------


def _make_skill_dir(tmp_path: Path, name: str = "my-skill") -> Path:
    """Create a minimal valid skill directory."""
    skill_dir = tmp_path / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# {{name}}\nHello {{email}}", encoding="utf-8")
    return skill_dir


# ── TestInstallSkillDir (flattened) ─────────────────────────────────────────


def test_install_skill_dir_nonexistent_skill_dir_returns_error(tmp_path: Path) -> None:
    missing = tmp_path / "nonexistent-skill"
    target = tmp_path / "target"
    result = install_skill_dir(missing, target, {})
    assert result.errors == 1
    assert result.rendered == 0


def test_install_skill_dir_no_skill_md_returns_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    skill_dir = tmp_path / "bad-skill"
    skill_dir.mkdir()
    (skill_dir / "README.md").write_text("no skill here", encoding="utf-8")
    target = tmp_path / "target"

    result = install_skill_dir(skill_dir, target, {})

    assert result.errors == 1
    captured = capsys.readouterr()
    assert "SKILL.md" in captured.err


def test_install_skill_dir_dry_run_prints_plan_no_write(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    skill_dir = _make_skill_dir(tmp_path)
    target = tmp_path / "target"

    install_skill_dir(skill_dir, target, {"name": "Alice"}, dry_run=True)

    assert not target.exists()
    captured = capsys.readouterr()
    assert "[dry-run]" in captured.out


def test_install_skill_dir_dry_run_non_md_file_prints_copy(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    skill_dir = _make_skill_dir(tmp_path)
    (skill_dir / "helper.py").write_text("print('hi')", encoding="utf-8")
    target = tmp_path / "target"

    install_skill_dir(skill_dir, target, {}, dry_run=True)

    captured = capsys.readouterr()
    assert "copy" in captured.out


def test_install_skill_dir_renders_md_file_with_context(tmp_path: Path) -> None:
    skill_dir = _make_skill_dir(tmp_path)
    target = tmp_path / "target"

    result = install_skill_dir(skill_dir, target, {"name": "Alice", "email": "alice@example.com"})  # pii-guard: ignore

    assert result.rendered == 1
    assert result.errors == 0
    rendered = (target / "SKILL.md").read_text(encoding="utf-8")
    assert "Alice" in rendered
    assert "alice@example.com" in rendered  # pii-guard: ignore


def test_install_skill_dir_unresolved_key_increments_warned(tmp_path: Path) -> None:
    skill_dir = _make_skill_dir(tmp_path)  # contains {{name}} and {{email}}
    target = tmp_path / "target"

    result = install_skill_dir(skill_dir, target, {})  # empty context

    assert result.warned == 2  # {{name}} and {{email}} unresolved
    assert result.rendered == 1  # file still written


def test_install_skill_dir_copies_non_md_file_verbatim(tmp_path: Path) -> None:
    skill_dir = _make_skill_dir(tmp_path)
    (skill_dir / "script.sh").write_text("#!/bin/bash\necho hi", encoding="utf-8")
    target = tmp_path / "target"

    result = install_skill_dir(skill_dir, target, {"name": "Alice", "email": "x"})

    assert result.copied == 1
    assert (target / "script.sh").exists()
    assert (target / "script.sh").read_text(encoding="utf-8") == "#!/bin/bash\necho hi"


def test_install_skill_dir_replaces_existing_symlink(tmp_path: Path) -> None:
    skill_dir = _make_skill_dir(tmp_path)
    target = tmp_path / "target"

    # Create a symlink at the target location
    real_dir = tmp_path / "real-dir"
    real_dir.mkdir()
    target.symlink_to(real_dir)
    assert target.is_symlink()

    result = install_skill_dir(skill_dir, target, {"name": "A", "email": "b"})

    assert not target.is_symlink()
    assert target.is_dir()
    assert result.errors == 0


def test_install_skill_dir_nested_subdirectory_files_installed(tmp_path: Path) -> None:
    skill_dir = _make_skill_dir(tmp_path)
    sub = skill_dir / "references"
    sub.mkdir()
    (sub / "guide.md").write_text("Guide: {{name}}", encoding="utf-8")
    target = tmp_path / "target"

    result = install_skill_dir(skill_dir, target, {"name": "Bob"})

    assert result.rendered == 2  # SKILL.md + references/guide.md
    assert (target / "references" / "guide.md").exists()
    assert "Bob" in (target / "references" / "guide.md").read_text(encoding="utf-8")


def test_install_skill_dir_mkdir_oserror_increments_errors(tmp_path: Path) -> None:
    """OSError during dst parent mkdir increments errors and skips file."""
    skill_dir = _make_skill_dir(tmp_path)
    target = tmp_path / "target"

    original_mkdir = Path.mkdir

    def _failing_mkdir(self: Path, **kwargs: object) -> None:
        if "target" in str(self):
            raise OSError("Permission denied")
        original_mkdir(self, **kwargs)

    with patch.object(Path, "mkdir", _failing_mkdir):
        result = install_skill_dir(skill_dir, target, {"name": "Alice", "email": "x"})

    assert result.errors >= 1


@skip_if_root
def test_install_skill_dir_write_text_oserror_increments_errors(tmp_path: Path) -> None:
    """OSError during write_text increments errors."""
    skill_dir = _make_skill_dir(tmp_path)
    target = tmp_path / "target"
    target.mkdir()
    skill_md = target / "SKILL.md"
    skill_md.write_text("existing", encoding="utf-8")
    skill_md.chmod(0o444)  # read-only to force write failure

    try:
        result = install_skill_dir(skill_dir, target, {"name": "Alice", "email": "x"})
        assert result.errors >= 1
    finally:
        skill_md.chmod(0o644)


def test_install_skill_dir_dry_run_symlink_printed_not_unlinked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    skill_dir = _make_skill_dir(tmp_path)
    target = tmp_path / "target"
    real_dir = tmp_path / "real-dir"
    real_dir.mkdir()
    target.symlink_to(real_dir)

    install_skill_dir(skill_dir, target, {}, dry_run=True)

    assert target.is_symlink()  # not unlinked in dry-run
    captured = capsys.readouterr()
    assert "symlink" in captured.out
