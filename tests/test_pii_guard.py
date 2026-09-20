"""Tests for hooks/pii_guard.py — account slug detection (historic regression) and existing checks.

This file is explicitly exempted from pii_guard checks (see _SKIP_FILES in pii_guard.py)
because it exercises detection of real account slugs by naming them as test inputs.
"""

import os
import re
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOKS_DIR = ROOT / "hooks"
if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))

from hooks.pii_guard import (  # noqa: E402
    _FIXTURE_ACCOUNT_SLUGS,
    _build_account_slug_pattern,
    _check_commit_msg_files,
    _check_line,
    _is_safe_email,
    _load_seed_slugs,
    main,
)

pytestmark = pytest.mark.unit

# A synthetic stand-in for "a real customer slug" — never a real customer name,
# and deliberately NOT one of the approved fixtures. Commit-msg tests below patch
# the compiled detection pattern to recognize it so blocking is deterministic
# regardless of whether accounts.yaml is present in the test environment.
_A_REAL_SLUG = "realcustomer-inc"
_A_REAL_SLUG_RE = re.compile(rf"\b({_A_REAL_SLUG})\b", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Account slug detection (historic regression)
# ---------------------------------------------------------------------------

# Approved synthetic fixture slugs — these must NEVER be flagged (R23). Sourced
# from the module so the two never drift. This file is in _SKIP_FILES so
# pii_guard does not scan it.
_FIXTURE_SLUGS = sorted(_FIXTURE_ACCOUNT_SLUGS)


# ── TestAccountSlugDetection (flattened) ────────────────────────────────────


def test_account_slug_detection_placeholder_is_safe() -> None:
    violations = _check_line("e.g. `/account-snapshot <account-slug>`", 1, "skill.md")
    assert violations == []


def test_account_slug_detection_inline_ignore_exempts_line() -> None:
    line = "accounts/acme-corp/pursuits/  # pii-guard: ignore"
    violations = _check_line(line, 1, "skill.md")
    assert violations == []


def test_account_slug_detection_no_false_positive_on_provision() -> None:
    violations = _check_line("provision the cluster", 1, "README.md")
    assert not any("account slug" in v for v in violations)


@pytest.mark.parametrize("slug", _FIXTURE_SLUGS)
def test_account_slug_detection_approved_fixtures_are_never_flagged(slug: str) -> None:
    """Approved synthetic fixtures are stand-ins, not real customers (R23)."""
    violations = _check_line(f"accounts/{slug}/pursuits/deal.md is the example", 1, "skill.md")
    assert not any("account slug" in v for v in violations), f"fixture {slug!r} must not be flagged"


# ---------------------------------------------------------------------------
# historic regression: _build_account_slug_pattern loads slugs from accounts.yaml
# ---------------------------------------------------------------------------


# ── TestBuildAccountSlugPattern (flattened) ─────────────────────────────────


def test_build_account_slug_pattern_never_matches_when_config_unavailable() -> None:
    """Without either runtime source, account detection is visibly degraded."""
    with (
        patch("hooks.pii_guard._load_seed_slugs", return_value=(frozenset(), True)),
        patch("fieldkit.config.get_account_names", side_effect=Exception("no config")),
    ):
        state = _build_account_slug_pattern()
    assert state.pattern.search(_A_REAL_SLUG) is None, "no real slug should match without config"
    assert state.warnings == ("pii-guard: no runtime account slugs are active; other PII checks remain enabled.",)
    for slug in _FIXTURE_SLUGS:
        assert state.pattern.search(slug) is None, f"fixture {slug!r} must never match"


def test_build_account_slug_pattern_accounts_yaml_slugs_added_to_pattern() -> None:
    """Slugs returned by get_account_names are included in the compiled pattern."""
    with (
        patch("hooks.pii_guard._load_seed_slugs", return_value=(frozenset(), True)),
        patch("fieldkit.config.get_account_names", return_value=["acme-real", "bigbank"]),
    ):
        state = _build_account_slug_pattern()
    assert state.pattern.search("acme-real"), "accounts.yaml slug 'acme-real' should be in pattern"
    assert state.pattern.search("bigbank"), "accounts.yaml slug 'bigbank' should be in pattern"
    assert state.warnings


def test_build_account_slug_pattern_fixtures_never_matched_from_runtime_sources() -> None:
    """Fixtures remain exempt when either runtime source lists them."""
    with (
        patch("hooks.pii_guard._load_seed_slugs", return_value=(frozenset({"acme-corp"}), False)),
        patch("fieldkit.config.get_account_names", return_value=["acme-bank", "acme-real"]),  # pii-guard: ignore
    ):
        state = _build_account_slug_pattern()
    assert state.pattern.search("acme-real"), "real slug 'acme-real' should be in pattern"
    assert state.pattern.search("acme-corp") is None, "seeded fixture must be subtracted"  # pii-guard: ignore
    assert state.pattern.search("acme-bank") is None, "fixture 'acme-bank' must be subtracted"  # pii-guard: ignore


def test_build_account_slug_pattern_only_fixtures_degrades_to_never_match() -> None:
    """When accounts.yaml holds only fixtures, the pattern matches nothing."""
    with (
        patch("hooks.pii_guard._load_seed_slugs", return_value=(frozenset(), True)),
        patch("fieldkit.config.get_account_names", return_value=_FIXTURE_SLUGS),
    ):
        state = _build_account_slug_pattern()
    for slug in _FIXTURE_SLUGS:
        assert state.pattern.search(slug) is None, f"fixture {slug!r} must not match"
    assert state.pattern.search("anything at all") is None, "empty slug set must never match"


def test_build_account_slug_pattern_duplicate_slugs_do_not_break_pattern() -> None:
    """A slug returned twice from accounts.yaml does not cause a regex error."""
    with (
        patch("hooks.pii_guard._load_seed_slugs", return_value=(frozenset(), True)),
        patch("fieldkit.config.get_account_names", return_value=["acme-real", "acme-real"]),
    ):
        state = _build_account_slug_pattern()
    assert state.pattern.search("acme-real")


# ---------------------------------------------------------------------------
# Existing checks — regression coverage
# ---------------------------------------------------------------------------


# ── TestHomePathDetection (flattened) ───────────────────────────────────────


def test_home_path_detection_blocks_home_path() -> None:
    line = "/" + "home" + "/alice" + "/work/fieldkit"
    violations = _check_line(line, 1, "docs.md")
    assert any("home-dir path" in v for v in violations)


def test_home_path_detection_allows_generic_home() -> None:
    violations = _check_line("default: /home/", 1, "docs.md")
    assert not any("home-dir path" in v for v in violations)


# ── TestEmailDetection (flattened) ──────────────────────────────────────────


def test_email_detection_blocks_real_email() -> None:
    address = "realname@realcorp" + ".invalid"
    violations = _check_line(f"contact {address} for details", 1, "docs.md")
    assert any("personal email" in v for v in violations)


def test_email_detection_allows_example_com() -> None:
    violations = _check_line("send to alice@example.com", 1, "docs.md")  # pii-guard: ignore
    assert not any("personal email" in v for v in violations)


def test_email_detection_allows_your_email_placeholder() -> None:
    violations = _check_line("set YOUR_EMAIL=user@internal.example.com", 1, "docs.md")  # pii-guard: ignore
    assert violations == []


def test_email_detection_does_not_trust_human_like_local_part() -> None:
    """A plausible address needs an RFC-reserved domain to be considered a fixture."""
    assert _is_safe_email("alice", "realcorp.io") is False
    assert _is_safe_email("alice", "fixture.example.com") is True


# ── TestInlineIgnore (flattened) ────────────────────────────────────────────


def test_inline_ignore_inline_ignore_suppresses_home_path() -> None:
    line = "/" + "home" + "/alice" + "/secret  # pii-guard: ignore"
    violations = _check_line(line, 1, "docs.md")
    assert violations == []


def test_inline_ignore_inline_ignore_suppresses_email() -> None:
    violations = _check_line("alice@realcorp-example.example.com  # pii-guard: ignore", 1, "docs.md")
    assert violations == []


# ── TestCommitMsgMode (historic regression) ────────────────────────────────────────────


def test_commit_msg_blocks_real_account_slug(tmp_path: Path) -> None:
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text(f"fix(ingest): route {_A_REAL_SLUG} transcripts correctly\n", encoding="utf-8")

    # _check_commit_msg_files uses the import-time compiled pattern; patch it so a
    # real slug is detected regardless of whether accounts.yaml is present here.
    with patch("hooks.pii_guard._KNOWN_ACCOUNT_SLUGS_RE", _A_REAL_SLUG_RE):
        assert _check_commit_msg_files([str(msg)]) == 1


@pytest.mark.parametrize("slug", _FIXTURE_SLUGS)
def test_commit_msg_allows_approved_fixture_slug(tmp_path: Path, slug: str) -> None:
    """The help text points authors at these fixtures — they must not be blocked."""
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text(f"docs: use {slug} as the example account in the walkthrough\n", encoding="utf-8")

    assert _check_commit_msg_files([str(msg)]) == 0


def test_commit_msg_blocks_personal_email(tmp_path: Path) -> None:
    msg = tmp_path / "COMMIT_EDITMSG"
    address = "realname@realcorp" + ".invalid"
    msg.write_text(f"chore: ping {address} about rollout\n", encoding="utf-8")

    assert _check_commit_msg_files([str(msg)]) == 1


def test_commit_msg_allows_clean_message(tmp_path: Path) -> None:
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text("fix(watch): correct stall-alert transition detection\n\nCloses #123\n", encoding="utf-8")

    assert _check_commit_msg_files([str(msg)]) == 0


def test_commit_msg_ignores_git_comment_lines(tmp_path: Path) -> None:
    msg = tmp_path / "COMMIT_EDITMSG"
    # The slug only appears on a git comment line — must not trip the guard even
    # though the pattern would otherwise detect it.
    msg.write_text(f"chore: tidy config\n\n# On branch feature/{_A_REAL_SLUG}\n", encoding="utf-8")

    with patch("hooks.pii_guard._KNOWN_ACCOUNT_SLUGS_RE", _A_REAL_SLUG_RE):
        assert _check_commit_msg_files([str(msg)]) == 0


def test_commit_msg_ignores_verbose_diff_below_scissors(tmp_path: Path) -> None:
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text(
        "chore: update fixtures\n\n"
        "# ------------------------ >8 ------------------------\n"
        f'# Do not modify below\n+    account = "{_A_REAL_SLUG}"\n',
        encoding="utf-8",
    )

    with patch("hooks.pii_guard._KNOWN_ACCOUNT_SLUGS_RE", _A_REAL_SLUG_RE):
        assert _check_commit_msg_files([str(msg)]) == 0


def test_commit_msg_allows_account_slug_placeholder(tmp_path: Path) -> None:
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text("docs: redact customer name to <account-slug> in example\n", encoding="utf-8")

    assert _check_commit_msg_files([str(msg)]) == 0


def test_main_dispatches_commit_msg_flag(tmp_path: Path) -> None:
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text(f"fix: {_A_REAL_SLUG} routing\n", encoding="utf-8")

    with patch("hooks.pii_guard._build_account_slug_pattern") as build_pattern:
        build_pattern.return_value.pattern = _A_REAL_SLUG_RE
        build_pattern.return_value.warnings = ()
        assert main(["--commit-msg", str(msg)]) == 1


def _write_seed(tmp_path: Path, text: str, *, mode: int = 0o600) -> Path:
    seed = tmp_path / "pii-guard-seeds.json"
    seed.write_text(text, encoding="utf-8")
    seed.chmod(mode)
    return seed


def test_load_seed_slugs_returns_missing_state(tmp_path: Path) -> None:
    with patch("fieldkit.config.get_fieldkit_data", return_value=tmp_path):
        slugs, missing = _load_seed_slugs()
    assert slugs == frozenset()
    assert missing is True


def test_load_seed_slugs_reads_normalizes_and_deduplicates(tmp_path: Path) -> None:
    _write_seed(tmp_path, '["realcustomer-inc", "REALCUSTOMER-INC", "big_bank"]')
    with patch("fieldkit.config.get_fieldkit_data", return_value=tmp_path):
        slugs, missing = _load_seed_slugs()
    assert slugs == {"realcustomer-inc", "big_bank"}
    assert missing is False


def test_load_seed_slugs_accepts_empty_array(tmp_path: Path) -> None:
    _write_seed(tmp_path, "[]")
    with patch("fieldkit.config.get_fieldkit_data", return_value=tmp_path):
        slugs, missing = _load_seed_slugs()
    assert slugs == frozenset()
    assert missing is False


@pytest.mark.parametrize(
    "contents",
    ["not-json", "{}", '["valid-slug", ""]', '["valid-slug", 42]', '["bad slug"]'],
)
def test_load_seed_slugs_rejects_invalid_schema(tmp_path: Path, contents: str) -> None:
    _write_seed(tmp_path, contents)
    with (
        patch("fieldkit.config.get_fieldkit_data", return_value=tmp_path),
        pytest.raises(RuntimeError, match="runtime seed schema is invalid"),
    ):
        _load_seed_slugs()


def test_load_seed_slugs_rejects_insecure_permissions(tmp_path: Path) -> None:
    _write_seed(tmp_path, "[]", mode=0o640)
    with (
        patch("fieldkit.config.get_fieldkit_data", return_value=tmp_path),
        pytest.raises(RuntimeError, match="permissions must be 0600"),
    ):
        _load_seed_slugs()


def test_load_seed_slugs_rejects_symlink_without_reading_target(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text('["never-read"]', encoding="utf-8")
    target.chmod(0o600)
    (tmp_path / "pii-guard-seeds.json").symlink_to(target)
    with (
        patch("fieldkit.config.get_fieldkit_data", return_value=tmp_path),
        pytest.raises(RuntimeError, match="file type is unsafe"),
    ):
        _load_seed_slugs()


def test_load_seed_slugs_rejects_non_regular_file(tmp_path: Path) -> None:
    (tmp_path / "pii-guard-seeds.json").mkdir()
    with (
        patch("fieldkit.config.get_fieldkit_data", return_value=tmp_path),
        pytest.raises(RuntimeError, match="file type is unsafe"),
    ):
        _load_seed_slugs()


def test_load_seed_slugs_rejects_fifo_without_blocking(tmp_path: Path) -> None:
    seed = tmp_path / "pii-guard-seeds.json"
    os.mkfifo(seed, mode=0o600)
    with (
        patch("fieldkit.config.get_fieldkit_data", return_value=tmp_path),
        pytest.raises(RuntimeError, match="file type is unsafe"),
    ):
        _load_seed_slugs()


def test_load_seed_slugs_rejects_unreadable_file(tmp_path: Path) -> None:
    with (
        patch("fieldkit.config.get_fieldkit_data", return_value=tmp_path),
        patch("hooks.pii_guard.os.open", side_effect=PermissionError("denied")),
        pytest.raises(RuntimeError, match="runtime seed file is unreadable"),
    ):
        _load_seed_slugs()


def test_load_seed_slugs_rejects_wrong_owner(tmp_path: Path) -> None:
    _write_seed(tmp_path, "[]")
    real_fstat = os.fstat

    def wrong_owner(descriptor: int) -> os.stat_result:
        metadata = list(real_fstat(descriptor))
        metadata[4] = metadata[4] + 1
        return os.stat_result(metadata)

    with (
        patch("fieldkit.config.get_fieldkit_data", return_value=tmp_path),
        patch("hooks.pii_guard.os.fstat", side_effect=wrong_owner),
        pytest.raises(RuntimeError, match="runtime seed owner is invalid"),
    ):
        _load_seed_slugs()


def test_load_seed_slugs_rejects_oversized_file(tmp_path: Path) -> None:
    _write_seed(tmp_path, '["' + "a" * (64 * 1024) + '"]')
    with (
        patch("fieldkit.config.get_fieldkit_data", return_value=tmp_path),
        pytest.raises(RuntimeError, match="exceeds the size limit"),
    ):
        _load_seed_slugs()


def test_build_account_slug_pattern_includes_seed_file_slug() -> None:
    with (
        patch("hooks.pii_guard._load_seed_slugs", return_value=(frozenset({_A_REAL_SLUG}), False)),
        patch("fieldkit.config.get_account_names", side_effect=Exception("no config")),
    ):
        state = _build_account_slug_pattern()
    assert state.pattern.search(_A_REAL_SLUG)
    assert state.warnings == ()


def test_main_blocks_invalid_seed_without_disclosing_input(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    sensitive_value = "private-customer-value"
    _write_seed(tmp_path, f'{{"slug": "{sensitive_value}"}}')
    with patch("fieldkit.config.get_fieldkit_data", return_value=tmp_path):
        result = main(["--commit-msg", str(tmp_path / "missing-message")])
    diagnostic = capsys.readouterr().err
    assert result == 1
    assert "runtime seed schema is invalid" in diagnostic
    assert sensitive_value not in diagnostic


def test_main_blocks_staged_mode_for_invalid_seed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_seed(tmp_path, "{}")
    with patch("fieldkit.config.get_fieldkit_data", return_value=tmp_path):
        result = main([])
    assert result == 1
    assert "runtime seed schema is invalid" in capsys.readouterr().err


def test_main_warns_when_no_runtime_account_slugs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch("fieldkit.config.get_fieldkit_data", return_value=tmp_path),
        patch("fieldkit.config.get_account_names", return_value=[]),
    ):
        result = main(["--commit-msg", str(tmp_path / "missing-message")])
    assert result == 0
    assert "no runtime account slugs are active" in capsys.readouterr().err


def test_commit_msg_blocks_slug_from_seed_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_seed(tmp_path, f'["{_A_REAL_SLUG}"]')
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text(f"fix: route {_A_REAL_SLUG} data correctly\n", encoding="utf-8")
    with (
        patch("fieldkit.config.get_fieldkit_data", return_value=tmp_path),
        patch("fieldkit.config.get_account_names", side_effect=Exception("no config")),
    ):
        assert main(["--commit-msg", str(msg)]) == 1
    diagnostic = capsys.readouterr().err
    assert "real account slug detected" in diagnostic
    assert _A_REAL_SLUG not in diagnostic
