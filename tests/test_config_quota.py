"""Round-trip tests for pipeline quota config helpers."""

from pathlib import Path

import pytest
import yaml

import fieldkit.config
import fieldkit.config._loader as config_mod
from fieldkit.config._loader import ConfigError
from fieldkit.config._quota import get_pipeline_quota, write_pipeline_quota

pytestmark = pytest.mark.unit


def _patch_config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point CONFIG_PATH at a temp file for isolation."""
    cfg = tmp_path / "config.yaml"
    monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg)
    return cfg


# ── TestGetPipelineQuota (flattened) ────────────────────────────────────────


def test_get_pipeline_quota_returns_none_when_file_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_config_path(tmp_path, monkeypatch)
    assert get_pipeline_quota() is None


def test_get_pipeline_quota_returns_none_when_pipeline_key_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _patch_config_path(tmp_path, monkeypatch)
    cfg.write_text("user_email: test@example.com\n", encoding="utf-8")  # pii-guard: ignore
    assert get_pipeline_quota() is None


def test_get_pipeline_quota_returns_none_when_quota_key_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _patch_config_path(tmp_path, monkeypatch)
    cfg.write_text("pipeline:\n  other: value\n", encoding="utf-8")
    assert get_pipeline_quota() is None


def test_get_pipeline_quota_returns_none_when_target_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _patch_config_path(tmp_path, monkeypatch)
    cfg.write_text("pipeline:\n  quota:\n    period: 2026-H2\n", encoding="utf-8")
    assert get_pipeline_quota() is None


def test_get_pipeline_quota_returns_none_when_period_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _patch_config_path(tmp_path, monkeypatch)
    cfg.write_text("pipeline:\n  quota:\n    target: 5000000\n", encoding="utf-8")
    assert get_pipeline_quota() is None


# ── TestRoundTrip (flattened) ───────────────────────────────────────────────


def test_round_trip_write_then_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_config_path(tmp_path, monkeypatch)
    write_pipeline_quota(target=5_000_000, period="2026-H2")
    result = get_pipeline_quota()
    assert result == {"target": 5_000_000, "period": "2026-H2"}


def test_round_trip_overwrite_preserves_other_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _patch_config_path(tmp_path, monkeypatch)
    cfg.write_text("user_email: test@example.com\n", encoding="utf-8")  # pii-guard: ignore
    write_pipeline_quota(target=1_000_000, period="2026-H1")
    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert data["user_email"] == "test@example.com"  # pii-guard: ignore
    assert data["pipeline"]["quota"]["target"] == 1_000_000


def test_round_trip_overwrite_updates_existing_quota(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_config_path(tmp_path, monkeypatch)
    write_pipeline_quota(target=3_000_000, period="2025-H2")
    write_pipeline_quota(target=5_000_000, period="2026-H1")
    result = get_pipeline_quota()
    assert result == {"target": 5_000_000, "period": "2026-H1"}


def test_quota_update_preserves_comments_order_and_quotes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _patch_config_path(tmp_path, monkeypatch)
    cfg.write_text(
        "# Operator notes stay with this file\n"
        'display_name: "Example territory"\n'
        "pipeline:\n"
        "  # Keep quota rationale here\n"
        "  quota:\n"
        "    period: '2025-H2'\n"
        "    target: 3000000\n"
        "fieldkit_home: /srv/fieldkit\n",
        encoding="utf-8",
    )

    write_pipeline_quota(target=5_000_000, period="2026-H1")

    updated = cfg.read_text(encoding="utf-8")
    assert "# Operator notes stay with this file" in updated
    assert "# Keep quota rationale here" in updated
    assert 'display_name: "Example territory"' in updated
    assert updated.index("display_name:") < updated.index("pipeline:") < updated.index("fieldkit_home:")
    assert "period: '2026-H1'" in updated
    assert "target: 5000000" in updated


def test_quota_update_preserves_legacy_yaml_boolean_semantics(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _patch_config_path(tmp_path, monkeypatch)
    cfg.write_text("legacy_flag: yes\npipeline: {}\n", encoding="utf-8")
    before = yaml.safe_load(cfg.read_text(encoding="utf-8"))

    write_pipeline_quota(target=5_000_000, period="2026-H1")

    updated = cfg.read_text(encoding="utf-8")
    after = yaml.safe_load(updated)
    assert "legacy_flag: yes" in updated
    assert after["legacy_flag"] is before["legacy_flag"] is True


def test_quota_update_adds_section_without_rewriting_comments(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _patch_config_path(tmp_path, monkeypatch)
    cfg.write_text("# local annotation\nfieldkit_home: /srv/fieldkit\n", encoding="utf-8")

    write_pipeline_quota(target=2_000_000, period="2026-H2")

    updated = cfg.read_text(encoding="utf-8")
    assert updated.startswith("# local annotation\nfieldkit_home: /srv/fieldkit\n")
    assert get_pipeline_quota() == {"target": 2_000_000, "period": "2026-H2"}


def test_quota_update_invalid_yaml_preserves_original(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _patch_config_path(tmp_path, monkeypatch)
    original = "pipeline: [unterminated\n"
    cfg.write_text(original, encoding="utf-8")

    with pytest.raises(ConfigError, match="Could not update pipeline quota"):
        write_pipeline_quota(target=2_000_000, period="2026-H2")

    assert cfg.read_text(encoding="utf-8") == original


def test_quota_update_invalidates_previously_populated_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _patch_config_path(tmp_path, monkeypatch)
    cfg.write_text("pipeline:\n  quota:\n    target: 1000000\n    period: 2025-H2\n", encoding="utf-8")
    assert get_pipeline_quota() == {"target": 1_000_000, "period": "2025-H2"}

    write_pipeline_quota(target=4_000_000, period="2026-H2")

    refreshed = get_pipeline_quota()
    assert refreshed == {"target": 4_000_000, "period": "2026-H2"}


def test_quota_update_invalidates_derived_config_caches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _patch_config_path(tmp_path, monkeypatch)
    cfg.write_text("fieldkit_home: /srv/old\n", encoding="utf-8")
    assert fieldkit.config.get_fieldkit_home() == Path("/srv/old")
    cfg.write_text("fieldkit_home: /srv/new\n", encoding="utf-8")

    write_pipeline_quota(target=4_000_000, period="2026-H2")

    refreshed = fieldkit.config.get_fieldkit_home()
    assert refreshed == Path("/srv/new")


@pytest.mark.parametrize(
    "original",
    [
        ("quota_defaults: &quota\n  target: 1000000\n  period: 2025-H2\npipeline:\n  quota: *quota\n"),
        ("pipeline_defaults: &pipeline\n  quota:\n    target: 1000000\n    period: 2025-H2\npipeline: *pipeline\n"),
    ],
)
def test_quota_update_detaches_owned_mapping_from_unrelated_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, original: str
) -> None:
    cfg = _patch_config_path(tmp_path, monkeypatch)
    cfg.write_text(original, encoding="utf-8")
    before = yaml.safe_load(original)

    write_pipeline_quota(target=4_000_000, period="2026-H2")

    after = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    unrelated_key = "quota_defaults" if "quota_defaults" in before else "pipeline_defaults"
    assert after[unrelated_key] == before[unrelated_key]
    assert after["pipeline"]["quota"] == {"target": 4_000_000, "period": "2026-H2"}


def test_quota_update_detaches_quota_from_pipeline_sibling_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _patch_config_path(tmp_path, monkeypatch)
    original = "pipeline:\n  defaults: &quota\n    target: 1000000\n    period: 2025-H2\n  quota: *quota\n"
    cfg.write_text(original, encoding="utf-8")
    before = yaml.safe_load(original)

    write_pipeline_quota(target=4_000_000, period="2026-H2")

    after = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert after["pipeline"]["defaults"] == before["pipeline"]["defaults"]
    assert after["pipeline"]["quota"] == {"target": 4_000_000, "period": "2026-H2"}


def test_quota_update_preserves_unrelated_alias_under_pipeline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _patch_config_path(tmp_path, monkeypatch)
    original = (
        "shared: &shared\n"
        "  enabled: yes\n"
        "pipeline:\n"
        "  unrelated: *shared\n"
        "  quota:\n"
        "    target: 1000000\n"
        "    period: 2025-H2\n"
    )
    cfg.write_text(original, encoding="utf-8")

    write_pipeline_quota(target=4_000_000, period="2026-H2")

    updated = cfg.read_text(encoding="utf-8")
    assert "shared: &shared" in updated
    assert "unrelated: *shared" in updated


def test_quota_update_invalid_utf8_preserves_original(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _patch_config_path(tmp_path, monkeypatch)
    original = b"fieldkit_home: \xff\n"
    cfg.write_bytes(original)

    with pytest.raises(ConfigError, match="Could not update pipeline quota"):
        write_pipeline_quota(target=2_000_000, period="2026-H2")

    assert cfg.read_bytes() == original


def test_quota_update_refuses_symlink_without_severing_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _patch_config_path(tmp_path, monkeypatch)
    target = tmp_path / "managed-config.yaml"
    original = "# managed elsewhere\nfieldkit_home: /srv/fieldkit\n"
    target.write_text(original, encoding="utf-8")
    cfg.symlink_to(target)

    with pytest.raises(ConfigError, match="Refusing to replace symlinked YAML file"):
        write_pipeline_quota(target=2_000_000, period="2026-H2")

    assert cfg.is_symlink()
    assert target.read_text(encoding="utf-8") == original
