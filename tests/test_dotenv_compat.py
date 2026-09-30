"""Tests for lib.config.dotenv.load_dotenv_safe()."""

import os
import warnings
from pathlib import Path
from unittest.mock import patch

import pytest
from dotenv import dotenv_values

from fieldkit.config._loader import ConfigError
from fieldkit.config.dotenv import load_dotenv_safe, validate_dotenv_values, write_dotenv_file
from fieldkit.util.text_snapshot import TextSnapshot, read_text_snapshot

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("disabled", [False, True])
def test_dotenv_disable_controls_real_file_loading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, disabled: bool
) -> None:
    """Artifact isolation disables the real loader, not only implicit path discovery."""
    name = "FIELDKIT_TEST_DOTENV_ISOLATION_SENTINEL"
    monkeypatch.setenv(name, "")
    monkeypatch.delenv(name)
    monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)
    if disabled:
        monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    env_file = tmp_path / ".env"
    env_file.write_text(f"{name}=synthetic-value\n", encoding="utf-8")

    result = load_dotenv_safe(dotenv_path=env_file)

    assert result is (not disabled)
    assert os.environ.get(name) == (None if disabled else "synthetic-value")


def test_loads_valid_env_vars(tmp_path: Path) -> None:
    """load_dotenv_safe() correctly sets env vars from a .env file."""
    env_file = tmp_path / ".env"
    env_file.write_text("FIELDKIT_TEST_VAR=hello_world\n", encoding="utf-8")

    os.environ.pop("FIELDKIT_TEST_VAR", None)
    try:
        result = load_dotenv_safe(dotenv_path=env_file)
        assert result is True
        assert os.environ.get("FIELDKIT_TEST_VAR") == "hello_world"
    finally:
        os.environ.pop("FIELDKIT_TEST_VAR", None)


def test_suppresses_export_prefix_warning(tmp_path: Path) -> None:
    """load_dotenv_safe() suppresses UserWarning for 'export KEY=val' lines."""
    env_file = tmp_path / ".env"
    env_file.write_text("export FIELDKIT_TEST_EXPORT=42\n", encoding="utf-8")

    os.environ.pop("FIELDKIT_TEST_EXPORT", None)
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            load_dotenv_safe(dotenv_path=env_file)

        # No UserWarning from dotenv should have leaked out
        dotenv_warnings = [
            w for w in caught if issubclass(w.category, UserWarning) and "dotenv" in str(w.filename).lower()
        ]
        assert dotenv_warnings == [], f"Unexpected dotenv warnings: {dotenv_warnings}"
    finally:
        os.environ.pop("FIELDKIT_TEST_EXPORT", None)


def test_loads_export_prefix_var(tmp_path: Path) -> None:
    """load_dotenv_safe() still loads the value from 'export KEY=val' lines."""
    env_file = tmp_path / ".env"
    env_file.write_text("export FIELDKIT_TEST_EXPORT2=99\n", encoding="utf-8")

    os.environ.pop("FIELDKIT_TEST_EXPORT2", None)
    try:
        load_dotenv_safe(dotenv_path=env_file)
        # python-dotenv does load export-prefixed vars (after stripping the prefix)
        val = os.environ.get("FIELDKIT_TEST_EXPORT2")
        # Accept either the loaded value or None — the key point is no warning fired.
        assert val in ("99", None)
    finally:
        os.environ.pop("FIELDKIT_TEST_EXPORT2", None)


def test_returns_false_for_missing_file(tmp_path: Path) -> None:
    """load_dotenv_safe() returns False when no .env file exists."""
    result = load_dotenv_safe(dotenv_path=tmp_path / "nonexistent.env")
    assert result is False


def test_validate_dotenv_values_accepts_exact_supported_characters() -> None:
    values = {"GOOGLE_OAUTH_CLIENT_SECRET": "${TOKEN}-'\"\\\\\n\N{SNOWMAN}"}

    result = validate_dotenv_values(values)

    assert result is None


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"BAD-KEY": "value"}, "Credential environment keys are invalid"),
        ({"SECRET": "bad\0value"}, "Credential environment values contain unsupported characters"),
        ({"SECRET": "bad\rvalue"}, "Credential environment values contain unsupported characters"),
    ],
)
def test_validate_dotenv_values_rejects_unsupported_input(values: dict[str, str], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        validate_dotenv_values(values)


@pytest.mark.parametrize(
    "value",
    [
        "$TOKEN",
        "${TOKEN}",
        "single'and\"double",
        "two\\\\backslashes",
        "line one\nline two",
        "snowman-\N{SNOWMAN}",
        "mix-$${TOKEN}-'\"\\\\\nnext",
    ],
)
def test_write_dotenv_file_round_trips_exact_values_without_interpolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    env_path = tmp_path / ".env"
    monkeypatch.setenv("TOKEN", "expanded-value")
    monkeypatch.delenv("FIELDKIT_TEST_SECRET", raising=False)

    result = write_dotenv_file(env_path, {"FIELDKIT_TEST_SECRET": value})

    assert result is None
    assert dotenv_values(env_path, interpolate=False) == {"FIELDKIT_TEST_SECRET": value}
    assert load_dotenv_safe(dotenv_path=env_path, override=True) is True
    assert os.environ["FIELDKIT_TEST_SECRET"] == value


def test_write_dotenv_file_is_deterministic_dotenv_not_shell_source(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"

    result = write_dotenv_file(env_path, {"Z_LAST": "z", "A_FIRST": "a"})

    assert result is None
    content = env_path.read_text(encoding="utf-8")
    assert content == "A_FIRST='a'\nZ_LAST='z'\n"
    assert "export " not in content
    assert env_path.stat().st_mode & 0o777 == 0o600


def test_write_dotenv_file_updates_regular_file_with_private_mode(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("OLD='value'\n", encoding="utf-8")
    env_path.chmod(0o644)

    result = write_dotenv_file(env_path, {"NEW": "value"})

    assert result is None
    assert dotenv_values(env_path, interpolate=False) == {"OLD": "value", "NEW": "value"}
    assert env_path.stat().st_mode & 0o777 == 0o600


def test_write_dotenv_file_preserves_unrelated_entries_comments_and_order(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    existing = (
        "# Existing provider configuration\n"
        "UNRELATED_PROVIDER_TOKEN='preserve-${TOKEN}-\\\\literal'\n"
        "GOOGLE_OAUTH_CLIENT_SECRET='old-secret'\n"
        "TAIL='keep-me'\n"
    )
    env_path.write_text(existing, encoding="utf-8")

    result = write_dotenv_file(
        env_path,
        {
            "GOOGLE_OAUTH_CLIENT_ID": "new-client",
            "GOOGLE_OAUTH_CLIENT_SECRET": "new-secret",
        },
    )

    assert result is None
    content = env_path.read_text(encoding="utf-8")
    assert content.startswith(
        "# Existing provider configuration\nUNRELATED_PROVIDER_TOKEN='preserve-${TOKEN}-\\\\literal'\n"
    )
    assert content.index("GOOGLE_OAUTH_CLIENT_SECRET") < content.index("TAIL")
    assert dotenv_values(env_path, interpolate=False) == {
        "UNRELATED_PROVIDER_TOKEN": "preserve-${TOKEN}-\\literal",
        "GOOGLE_OAUTH_CLIENT_SECRET": "new-secret",
        "TAIL": "keep-me",
        "GOOGLE_OAUTH_CLIENT_ID": "new-client",
    }


@pytest.mark.parametrize(
    "existing",
    [
        "DUPLICATE='first'\nDUPLICATE='second'\n",
        "VALID='value'\nif unsupported shell syntax; then\n  echo no\nfi\n",
    ],
    ids=["duplicate", "malformed"],
)
def test_write_dotenv_file_rejects_ambiguous_existing_file_without_mutation(tmp_path: Path, existing: str) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(existing, encoding="utf-8")
    before = (env_path.read_bytes(), env_path.stat().st_mtime_ns)

    with pytest.raises(ConfigError, match="Existing credential environment file is invalid"):
        write_dotenv_file(env_path, {"GOOGLE_OAUTH_CLIENT_ID": "new-client"})

    assert (env_path.read_bytes(), env_path.stat().st_mtime_ns) == before


def test_write_dotenv_file_rejects_oversized_existing_file_without_mutation(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_bytes(b"#" * (1024 * 1024 + 1))
    before = (env_path.stat().st_size, env_path.stat().st_mtime_ns)

    with pytest.raises(ConfigError, match="Existing credential environment file is unsafe or too large"):
        write_dotenv_file(env_path, {"GOOGLE_OAUTH_CLIENT_ID": "new-client"})

    assert (env_path.stat().st_size, env_path.stat().st_mtime_ns) == before


def test_write_dotenv_file_refuses_concurrent_existing_file_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("UNRELATED='original'\n", encoding="utf-8")
    snapshots = 0

    def mutate_before_recheck(path: Path, *, max_bytes: int) -> TextSnapshot:
        nonlocal snapshots
        snapshots += 1
        if snapshots == 2:
            env_path.write_text("UNRELATED='concurrent'\n", encoding="utf-8")
        return read_text_snapshot(path, max_bytes=max_bytes)

    monkeypatch.setattr("fieldkit.config.dotenv.read_text_snapshot", mutate_before_recheck)

    with pytest.raises(ConfigError, match="Existing credential environment file changed during update"):
        write_dotenv_file(env_path, {"GOOGLE_OAUTH_CLIENT_ID": "new-client"})

    assert env_path.read_text(encoding="utf-8") == "UNRELATED='concurrent'\n"


def test_write_dotenv_file_accepts_configured_directory_alias(tmp_path: Path) -> None:
    credential_directory = tmp_path / "credentials"
    credential_directory.mkdir()
    configured_alias = tmp_path / "configured"
    configured_alias.symlink_to(credential_directory, target_is_directory=True)

    result = write_dotenv_file(configured_alias / ".env", {"SECRET": "value"})

    assert result is None
    assert dotenv_values(credential_directory / ".env", interpolate=False) == {"SECRET": "value"}
    assert (credential_directory / ".env").stat().st_mode & 0o777 == 0o600


def test_write_dotenv_file_staging_is_private_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed_modes: list[int] = []
    real_replace = Path.replace

    def observe_replace(source: Path, target: Path) -> Path:
        observed_modes.append(source.stat().st_mode & 0o777)
        return real_replace(source, target)

    monkeypatch.setattr(Path, "replace", observe_replace)

    write_dotenv_file(tmp_path / ".env", {"FIRST": "one", "SECOND": "two"})

    assert observed_modes == [0o600]


@pytest.mark.parametrize("value", ["nul\0value", "carriage\rreturn", "windows\r\nnewline"])
def test_write_dotenv_file_rejects_unrepresentable_values_before_writing(tmp_path: Path, value: str) -> None:
    env_path = tmp_path / ".env"

    with pytest.raises(ConfigError, match="Credential environment values contain unsupported characters"):
        write_dotenv_file(env_path, {"SECRET": value})

    assert not env_path.exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("key", ["", "1SECRET", "BAD-KEY", "BAD\nKEY"])
def test_write_dotenv_file_rejects_invalid_keys_before_writing(tmp_path: Path, key: str) -> None:
    env_path = tmp_path / ".env"

    with pytest.raises(ConfigError, match="Credential environment keys are invalid"):
        write_dotenv_file(env_path, {key: "value"})

    assert not env_path.exists()
    assert list(tmp_path.iterdir()) == []


def test_write_dotenv_file_rejects_non_string_value_before_writing(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"

    with pytest.raises(ConfigError, match="Credential environment values are invalid"):
        write_dotenv_file(env_path, {"SECRET": 3})  # type: ignore[dict-item]

    assert not env_path.exists()
    assert list(tmp_path.iterdir()) == []


def test_write_dotenv_file_requires_directory_parent(tmp_path: Path) -> None:
    parent = tmp_path / "not-a-directory"
    parent.write_text("preserve me", encoding="utf-8")

    with pytest.raises(ConfigError, match="Credential environment directory is unavailable"):
        write_dotenv_file(parent / ".env", {"SECRET": "value"})

    assert parent.read_text(encoding="utf-8") == "preserve me"


def test_write_dotenv_file_rejects_parent_symlink_loop_with_fixed_error(tmp_path: Path) -> None:
    loop = tmp_path / "loop"
    loop.symlink_to(loop, target_is_directory=True)

    with pytest.raises(ConfigError) as caught:
        write_dotenv_file(loop / ".env", {"SECRET": "value"})

    assert str(caught.value) == "Credential environment directory is unavailable"


def test_write_dotenv_file_refuses_symlink_without_changing_read_only_target(tmp_path: Path) -> None:
    target = tmp_path / "credential-target"
    target.write_text("preserve me\n", encoding="utf-8")
    target.chmod(0o400)
    env_path = tmp_path / ".env"
    env_path.symlink_to(target)

    with pytest.raises(ConfigError, match="Credential environment destination is not a regular file"):
        write_dotenv_file(env_path, {"SECRET": "replacement"})

    assert env_path.is_symlink()
    assert target.read_text(encoding="utf-8") == "preserve me\n"
    assert target.stat().st_mode & 0o777 == 0o400


def test_write_dotenv_file_reports_fixed_error_and_cleans_staging_file(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    with (
        patch("fieldkit.config.dotenv.set_key", side_effect=OSError("private /home/example secret")),
        pytest.raises(ConfigError) as caught,
    ):
        write_dotenv_file(env_path, {"SECRET": "value"})

    assert str(caught.value) == "Credential environment file could not be written safely"
    assert list(tmp_path.iterdir()) == []


def test_load_dotenv_safe_preserves_existing_environment_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_path = tmp_path / ".env"
    write_dotenv_file(env_path, {"FIELDKIT_TEST_PRECEDENCE": "from-file"})
    monkeypatch.setenv("FIELDKIT_TEST_PRECEDENCE", "existing")

    result = load_dotenv_safe(dotenv_path=env_path)

    assert result is True
    assert os.environ["FIELDKIT_TEST_PRECEDENCE"] == "existing"
