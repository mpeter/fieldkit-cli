"""Tests for lib.config.dotenv.load_dotenv_safe()."""

import os
import warnings
from pathlib import Path

import pytest

from fieldkit.config.dotenv import load_dotenv_safe

pytestmark = pytest.mark.unit


def test_loads_valid_env_vars(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """load_dotenv_safe() correctly sets env vars from a .env file."""
    # Enable only this explicit temporary fixture, preserving the caller's guard afterward.
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "0")
    env_file = tmp_path / ".env"
    env_file.write_text("FIELDKIT_TEST_VAR=hello_world\n", encoding="utf-8")

    os.environ.pop("FIELDKIT_TEST_VAR", None)
    try:
        result = load_dotenv_safe(dotenv_path=env_file)
        assert result is True
        assert os.environ.get("FIELDKIT_TEST_VAR") == "hello_world"
    finally:
        os.environ.pop("FIELDKIT_TEST_VAR", None)


def test_suppresses_export_prefix_warning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """load_dotenv_safe() suppresses UserWarning for 'export KEY=val' lines."""
    # Enable only this explicit temporary fixture, preserving the caller's guard afterward.
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "0")
    env_file = tmp_path / ".env"
    env_file.write_text("export FIELDKIT_TEST_EXPORT=42\n", encoding="utf-8")

    os.environ.pop("FIELDKIT_TEST_EXPORT", None)
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = load_dotenv_safe(dotenv_path=env_file)

        assert result is True
        # No UserWarning from dotenv should have leaked out
        dotenv_warnings = [
            w for w in caught if issubclass(w.category, UserWarning) and "dotenv" in str(w.filename).lower()
        ]
        assert dotenv_warnings == [], f"Unexpected dotenv warnings: {dotenv_warnings}"
    finally:
        os.environ.pop("FIELDKIT_TEST_EXPORT", None)


def test_loads_export_prefix_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """load_dotenv_safe() still loads the value from 'export KEY=val' lines."""
    # Enable only this explicit temporary fixture, preserving the caller's guard afterward.
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "0")
    env_file = tmp_path / ".env"
    env_file.write_text("export FIELDKIT_TEST_EXPORT2=99\n", encoding="utf-8")

    os.environ.pop("FIELDKIT_TEST_EXPORT2", None)
    try:
        result = load_dotenv_safe(dotenv_path=env_file)
        assert result is True
        # python-dotenv does load export-prefixed vars (after stripping the prefix)
        val = os.environ.get("FIELDKIT_TEST_EXPORT2")
        assert val == "99"
    finally:
        os.environ.pop("FIELDKIT_TEST_EXPORT2", None)


def test_returns_false_for_missing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """load_dotenv_safe() returns False when no .env file exists."""
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "0")
    result = load_dotenv_safe(dotenv_path=tmp_path / "nonexistent.env")
    assert result is False
