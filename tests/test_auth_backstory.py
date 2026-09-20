"""Tests for Backstory authentication through MCPJungle."""

import subprocess
from collections.abc import Sequence
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

import fieldkit.config._integrations as integrations_config
import fieldkit.config._loader as config_loader
from fieldkit.__main__ import main
from fieldkit.backstory.auth import BACKSTORY_SERVER_URL, BackstoryAuthError, authenticate
from fieldkit.cli_registry import build_registry
from fieldkit.commands.auth.cli import cli as auth_cli
from fieldkit.config import TIMEOUT_INTERACTIVE_AUTH, ConfigError
from fieldkit.config._loader import _has_stable_directory_ancestor

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "config_text",
    [
        "mcp_gateway_url: [",
        "- not-a-mapping\n",
        "mcp_gateway_url:\n  nested: value\n",
    ],
)
def test_authenticate_rejects_invalid_existing_config_before_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    config_text: str,
) -> None:
    """Reject malformed or schema-invalid config before replacing registration."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(config_text, encoding="utf-8")
    monkeypatch.setattr(config_loader, "CONFIG_PATH", config_path)
    runner_called = False

    def run(args: Sequence[str], *, check: bool, timeout: int) -> subprocess.CompletedProcess[bytes]:
        nonlocal runner_called
        runner_called = True
        return subprocess.CompletedProcess(args, 0)

    with pytest.raises(ConfigError, match=r"Config file") as exc_info:
        authenticate(runner=run, stdin_is_tty=lambda: True)

    assert str(exc_info.value)
    assert runner_called is False


@pytest.mark.parametrize(
    ("gateway_env", "expected_url"),
    [
        (None, "http://127.0.0.1:8080"),
        ("http://gateway.example.com:9090/", "http://gateway.example.com:9090"),
    ],
)
def test_authenticate_allows_absent_config_with_canonical_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gateway_env: str | None,
    expected_url: str,
) -> None:
    """Use the canonical fallback only when the config file is truly absent."""
    monkeypatch.setattr(config_loader, "CONFIG_PATH", tmp_path / "missing.yaml")
    if gateway_env is None:
        monkeypatch.delenv("FIELDKIT_MCP_GATEWAY_URL", raising=False)
    else:
        monkeypatch.setenv("FIELDKIT_MCP_GATEWAY_URL", gateway_env)
    observed_args: list[str] = []

    def run(args: Sequence[str], *, check: bool, timeout: int) -> subprocess.CompletedProcess[bytes]:
        observed_args.extend(args)
        return subprocess.CompletedProcess(args, 0)

    result = authenticate(runner=run, stdin_is_tty=lambda: True)

    assert result is None
    assert observed_args[2] == expected_url


def test_get_mcp_gateway_url_returns_configured_url_directly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prefer and normalize the gateway URL stored in config."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("mcp_gateway_url: http://gateway.example.com:8080/\n", encoding="utf-8")
    monkeypatch.setattr(config_loader, "CONFIG_PATH", config_path)

    result = integrations_config.get_mcp_gateway_url()

    assert result == "http://gateway.example.com:8080"


def test_get_mcp_gateway_url_permissive_read_falls_back_after_invalid_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preserve permissive fallback behavior for non-auth gateway consumers."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("mcp_gateway_url:\n  nested: value\n", encoding="utf-8")
    monkeypatch.setattr(config_loader, "CONFIG_PATH", config_path)
    monkeypatch.setenv("FIELDKIT_MCP_GATEWAY_URL", "http://fallback.example.com:9090/")

    result = integrations_config.get_mcp_gateway_url()

    assert result == "http://fallback.example.com:9090"


def test_authenticate_rejects_invalid_config_after_cached_valid_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read auth config afresh so a stale cache cannot select the gateway."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("mcp_gateway_url: http://valid.example.com:8080\n", encoding="utf-8")
    monkeypatch.setattr(config_loader, "CONFIG_PATH", config_path)
    assert config_loader._load_raw_config() == {"mcp_gateway_url": "http://valid.example.com:8080"}
    config_path.write_text("mcp_gateway_url: [", encoding="utf-8")
    runner_called = False

    def run(args: Sequence[str], *, check: bool, timeout: int) -> subprocess.CompletedProcess[bytes]:
        nonlocal runner_called
        runner_called = True
        return subprocess.CompletedProcess(args, 0)

    with pytest.raises(ConfigError, match=r"invalid YAML") as exc_info:
        authenticate(runner=run, stdin_is_tty=lambda: True)

    assert str(exc_info.value)
    assert runner_called is False


def test_authenticate_rejects_dangling_config_symlink_before_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Treat a dangling config symlink as invalid rather than absent."""
    config_path = tmp_path / "config.yaml"
    config_path.symlink_to(tmp_path / "missing-target.yaml")
    monkeypatch.setattr(config_loader, "CONFIG_PATH", config_path)
    runner_called = False

    def run(args: Sequence[str], *, check: bool, timeout: int) -> subprocess.CompletedProcess[bytes]:
        nonlocal runner_called
        runner_called = True
        return subprocess.CompletedProcess(args, 0)

    with pytest.raises(ConfigError, match=r"Could not read config file") as exc_info:
        authenticate(runner=run, stdin_is_tty=lambda: True)

    assert str(exc_info.value)
    assert runner_called is False


def test_authenticate_rejects_config_below_dangling_directory_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject config beneath a dangling directory symlink before launch."""
    config_parent = tmp_path / "config-link"
    config_parent.symlink_to(tmp_path / "missing-directory", target_is_directory=True)
    monkeypatch.setattr(config_loader, "CONFIG_PATH", config_parent / "config.yaml")
    runner_called = False

    def run(args: Sequence[str], *, check: bool, timeout: int) -> subprocess.CompletedProcess[bytes]:
        nonlocal runner_called
        runner_called = True
        return subprocess.CompletedProcess(args, 0)

    with pytest.raises(ConfigError, match=r"Could not read config file") as exc_info:
        authenticate(runner=run, stdin_is_tty=lambda: True)

    assert str(exc_info.value)
    assert runner_called is False


def test_authenticate_rejects_config_below_non_directory_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject config whose parent path resolves to a regular file."""
    parent = tmp_path / "not-a-directory"
    parent.write_text("plain file", encoding="utf-8")
    monkeypatch.setattr(config_loader, "CONFIG_PATH", parent / "config.yaml")
    runner_called = False

    def run(args: Sequence[str], *, check: bool, timeout: int) -> subprocess.CompletedProcess[bytes]:
        nonlocal runner_called
        runner_called = True
        return subprocess.CompletedProcess(args, 0)

    with pytest.raises(ConfigError, match=r"Could not read config file") as exc_info:
        authenticate(runner=run, stdin_is_tty=lambda: True)

    assert str(exc_info.value)
    assert runner_called is False


def test_authenticate_rejects_non_utf8_config_before_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Map undecodable config to a safe config error before launch."""
    config_path = tmp_path / "config.yaml"
    config_path.write_bytes(b"mcp_gateway_url: \xff\n")
    monkeypatch.setattr(config_loader, "CONFIG_PATH", config_path)
    runner_called = False

    def run(args: Sequence[str], *, check: bool, timeout: int) -> subprocess.CompletedProcess[bytes]:
        nonlocal runner_called
        runner_called = True
        return subprocess.CompletedProcess(args, 0)

    with pytest.raises(ConfigError, match=r"Could not read config file") as exc_info:
        authenticate(runner=run, stdin_is_tty=lambda: True)

    assert str(exc_info.value)
    assert runner_called is False


def test_stable_directory_ancestor_accepts_truly_absent_config(tmp_path: Path) -> None:
    """Recognize a missing leaf beneath a stable directory as true absence."""
    result = _has_stable_directory_ancestor(tmp_path / "missing" / "config.yaml")

    assert result is True


def test_stable_directory_ancestor_rejects_dangling_config_symlink(tmp_path: Path) -> None:
    """Distinguish a dangling leaf symlink from a truly absent config."""
    config_path = tmp_path / "config.yaml"
    config_path.symlink_to(tmp_path / "missing-target.yaml")

    result = _has_stable_directory_ancestor(config_path)

    assert result is False


def test_stable_directory_ancestor_rejects_missing_ancestry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail closed when ancestry cannot be established after a missing leaf."""

    def report_missing(_path: Path) -> object:
        raise FileNotFoundError

    monkeypatch.setattr(Path, "lstat", report_missing)

    result = _has_stable_directory_ancestor(tmp_path / "config.yaml")

    assert result is False


def test_stable_directory_ancestor_rejects_lstat_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail closed when inspecting the candidate config path is forbidden."""

    def fail_lstat(_path: Path) -> object:
        raise PermissionError

    monkeypatch.setattr(Path, "lstat", fail_lstat)

    result = _has_stable_directory_ancestor(tmp_path / "config.yaml")

    assert result is False


def test_stable_directory_ancestor_rejects_symlink_to_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject a missing config whose apparent directory is a file symlink."""
    target = tmp_path / "plain-file"
    target.write_text("not a directory", encoding="utf-8")
    ancestor = tmp_path / "config-link"
    ancestor.symlink_to(target)
    config_path = ancestor / "config.yaml"
    original_lstat = Path.lstat

    def report_leaf_missing(path: Path) -> object:
        if path == config_path:
            raise FileNotFoundError
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", report_leaf_missing)

    result = _has_stable_directory_ancestor(config_path)

    assert result is False


def test_stable_directory_ancestor_rejects_non_directory_ancestor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject a missing config beneath an existing non-directory ancestor."""
    ancestor = tmp_path / "plain-file"
    ancestor.write_text("not a directory", encoding="utf-8")
    config_path = ancestor / "config.yaml"
    original_lstat = Path.lstat

    def report_leaf_missing(path: Path) -> object:
        if path == config_path:
            raise FileNotFoundError
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", report_leaf_missing)

    result = _has_stable_directory_ancestor(config_path)

    assert result is False


def test_stable_directory_ancestor_rejects_recheck_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail closed if a stable ancestor cannot be verified a second time."""
    config_path = tmp_path / "config.yaml"
    original_lstat = Path.lstat
    ancestor_checks = 0

    def fail_ancestor_recheck(path: Path) -> object:
        nonlocal ancestor_checks
        if path == config_path:
            raise FileNotFoundError
        if path == tmp_path:
            ancestor_checks += 1
            if ancestor_checks == 2:
                raise PermissionError
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", fail_ancestor_recheck)

    result = _has_stable_directory_ancestor(config_path)

    assert result is False


def test_stable_directory_ancestor_rejects_replacement_during_recheck(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Detect an ancestor replaced between the initial and confirming checks."""
    ancestor = tmp_path / "config"
    ancestor.mkdir()
    config_path = ancestor / "config.yaml"
    original_ancestor = tmp_path / "original-config"
    original_lstat = Path.lstat
    ancestor_checks = 0

    def replace_ancestor_before_recheck(path: Path) -> object:
        nonlocal ancestor_checks
        if path == ancestor:
            ancestor_checks += 1
            if ancestor_checks == 2:
                ancestor.rename(original_ancestor)
                ancestor.mkdir()
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", replace_ancestor_before_recheck)

    result = _has_stable_directory_ancestor(config_path)

    assert result is False


def test_authenticate_rejects_noninteractive_stdin_before_launch() -> None:
    """Require interactive stdin before issuing a destructive registration command."""
    runner_called = False

    def run(args: Sequence[str], *, check: bool, timeout: int) -> subprocess.CompletedProcess[bytes]:
        nonlocal runner_called
        runner_called = True
        return subprocess.CompletedProcess(args, 0)

    with pytest.raises(BackstoryAuthError, match=r"interactive terminal.*Rerun") as exc_info:
        authenticate(runner=run, stdin_is_tty=lambda: False)

    assert str(exc_info.value)
    assert runner_called is False


def test_authenticate_allows_redirected_output_with_interactive_stdin() -> None:
    """Allow redirected output when browser consent can use interactive stdin."""
    observed_timeout: int | None = None

    def run(args: Sequence[str], *, check: bool, timeout: int) -> subprocess.CompletedProcess[bytes]:
        nonlocal observed_timeout
        observed_timeout = timeout
        return subprocess.CompletedProcess(args, 0)

    with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
        result = authenticate(runner=run, stdin_is_tty=lambda: True)

    assert result is None
    assert observed_timeout == 900


def test_authenticate_uses_secret_free_native_oauth_command(monkeypatch: pytest.MonkeyPatch) -> None:
    """Launch only the canonical secret-free MCPJungle OAuth arguments."""
    observed: dict[str, object] = {}
    observed_args: list[str] = []

    def run(args: Sequence[str], *, check: bool, timeout: int) -> subprocess.CompletedProcess[bytes]:
        observed_args.extend(args)
        observed.update(args=list(args), check=check, timeout=timeout)
        return subprocess.CompletedProcess(args, 0)

    def get_gateway_url(*, strict: bool = False) -> str:
        assert strict is True
        return "http://gateway.example.com:8080"

    monkeypatch.setattr("fieldkit.backstory.auth.get_mcp_gateway_url", get_gateway_url)

    result = authenticate(runner=run, stdin_is_tty=lambda: True)

    assert result is None
    assert observed == {
        "args": [
            "mcpjungle",
            "--registry",
            "http://gateway.example.com:8080",
            "register",
            "--name",
            "backstory",
            "--description",
            "People.ai Backstory sales intelligence",
            "--url",
            BACKSTORY_SERVER_URL,
            "--force",
        ],
        "check": False,
        "timeout": TIMEOUT_INTERACTIVE_AUTH,
    }
    command = " ".join(observed_args)
    assert "token" not in command.lower()
    assert "secret" not in command.lower()
    assert "code" not in command.lower()


@pytest.mark.parametrize(
    ("side_effect", "match"),
    [
        (FileNotFoundError(), "not installed"),
        (PermissionError("private executable path"), "could not be started"),
        (subprocess.TimeoutExpired(["mcpjungle"], TIMEOUT_INTERACTIVE_AUTH), "timed out"),
        (KeyboardInterrupt(), "cancelled"),
    ],
)
def test_authenticate_maps_process_failures(side_effect: BaseException, match: str) -> None:
    """Translate launch, timeout, and cancellation failures into auth errors."""

    def run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise side_effect

    with pytest.raises(BackstoryAuthError, match=match) as exc_info:
        authenticate(runner=run, stdin_is_tty=lambda: True)

    assert str(exc_info.value)


def test_authenticate_maps_nonzero_completion() -> None:
    """Treat a nonzero MCPJungle completion as failed authorization."""

    def run(args: Sequence[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args, 1)

    with pytest.raises(BackstoryAuthError, match="did not complete") as exc_info:
        authenticate(runner=run, stdin_is_tty=lambda: True)

    assert str(exc_info.value)


def test_auth_command_success() -> None:
    """Report the browser handoff and active status around successful auth."""
    runner = CliRunner()

    with patch("fieldkit.commands.auth.backstory.authenticate") as mock_authenticate:
        result = runner.invoke(auth_cli, ["backstory"], catch_exceptions=False)

    assert result.exit_code == 0
    assert "complete authorization in the browser" in result.stderr
    assert "Backstory auth active" in result.stdout
    mock_authenticate.assert_called_once_with()


def test_auth_backstory_is_declared_external() -> None:
    """Reauthorization replaces a live MCPJungle registration without --confirm."""
    entries = {entry.full_name: entry for entry in build_registry()}
    entry = entries["auth backstory"]
    assert entry.write_class == "external"
    assert entry.write_class_source == "declared"
    assert entry.confirm_exempt, "auth backstory must document why it does not expose --confirm"


def test_auth_failure_maps_to_exit_two_through_root_dispatcher(capsys: pytest.CaptureFixture[str]) -> None:
    """Map sanitized Backstory auth failures to the documented auth exit code."""

    def fail_to_launch() -> None:
        def run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
            raise PermissionError("private executable path")

        authenticate(runner=run, stdin_is_tty=lambda: True)

    with patch("fieldkit.commands.auth.backstory.authenticate", new=fail_to_launch):
        exit_code = main(["auth", "backstory"])

    assert exit_code == 2
    stderr = capsys.readouterr().err
    assert "Auth error" in stderr
    assert "Traceback" not in stderr
    assert "private executable path" not in stderr


def test_invalid_encoding_maps_to_exit_three_without_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Map invalid config encoding to data error without leaking a traceback."""
    config_path = tmp_path / "config.yaml"
    config_path.write_bytes(b"mcp_gateway_url: \xff\n")
    monkeypatch.setattr(config_loader, "CONFIG_PATH", config_path)

    def authenticate_interactively() -> None:
        authenticate(runner=lambda *_args, **_kwargs: pytest.fail("runner must not launch"), stdin_is_tty=lambda: True)

    with patch("fieldkit.commands.auth.backstory.authenticate", new=authenticate_interactively):
        exit_code = main(["auth", "backstory"])

    assert exit_code == 3
    stderr = capsys.readouterr().err
    assert "Config error" in stderr
    assert "Traceback" not in stderr


def test_auth_help_explains_gateway_refresh_ownership() -> None:
    """Tell operators that MCPJungle owns refresh and reruns replace registration."""
    result = CliRunner().invoke(auth_cli, ["backstory", "--help"])

    assert result.exit_code == 0
    assert "MCPJungle stores and refreshes" in result.output
    assert "Rerun this command" in result.output
