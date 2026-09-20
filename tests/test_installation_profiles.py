"""Subprocess contracts for isolated dependency-profile command surfaces."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PROFILE_RUNNER = r"""
import contextlib
import importlib.abc
import importlib.util
import io
import os
from pathlib import Path
import sys

profile = sys.argv[1]
argv = sys.argv[2:]
profile_roots = {
    "google": ("google.auth", "google.oauth2", "google_auth_httplib2", "google_auth_oauthlib", "googleapiclient"),
    "llm": ("agentplatform", "litellm", "openai", "vertexai"),
    "web": ("fastapi", "uvicorn"),
    "chrome-auth": ("cryptography", "secretstorage"),
}
enabled = set(profile_roots) if profile == "all" else ({profile} if profile != "base" else set())

class ProfileImportBlocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        for owner, roots in profile_roots.items():
            if owner not in enabled and any(fullname == root or fullname.startswith(f"{root}.") for root in roots):
                raise ModuleNotFoundError(f"profile-isolated import blocked: {fullname}", name=fullname)
        return None

sys.meta_path.insert(0, ProfileImportBlocker())

import fieldkit.__main__ as dispatcher

dispatcher.load_dotenv_safe = lambda **_kwargs: False
from fieldkit.__main__ import _COMMANDS, main
from fieldkit.config.optional_dependencies import require_optional_profile

if argv == ["__probe__"]:
    roots = profile_roots[profile]
    require_optional_profile(f"profile smoke {profile}", profile, roots)
    raise SystemExit(0)
if argv == ["__chrome__"]:
    from fieldkit.cli_exit import handle_cli_exception
    from fieldkit.shadowbot.auth import acquire_from_chrome

    try:
        acquire_from_chrome()
    except Exception as exc:
        raise SystemExit(handle_cli_exception(exc)) from None
    raise SystemExit(0)
if argv == ["__ingest_transcript__"]:
    from fieldkit.cli_exit import handle_cli_exception
    from fieldkit.commands.ingest.run import _run_processing_loop

    try:
        _run_processing_loop([object()], conn=object(), pipeline_version="1.0.0", interactive=False)
    except Exception as exc:
        raise SystemExit(handle_cli_exception(exc)) from None
    raise SystemExit(0)
if argv[0] == "__llm_command__":
    import fieldkit.config as config
    import fieldkit.config._loader as config_loader

    root = Path(argv[1])
    command = argv[2]
    config_path = root / "config" / "config.yaml"
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    config_path.parent.mkdir(parents=True)
    config_path.write_text(f"fieldkit_home: {workspace}\n", encoding="utf-8")
    config.CONFIG_PATH = config_path
    config_loader.CONFIG_PATH = config_path
    config.clear_config_caches()
    command_argv = {
        "brief": ["brief", "generate", "--pipeline-only"],
        "pipeline": ["pipeline", "--data-root", str(workspace)],
    }[command]
    raise SystemExit(main(command_argv))
if argv == ["__all__"]:
    for command in sorted(_COMMANDS):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = main([command, "--help"])
        if result != 0:
            print(f"{command}: exit {result}", file=sys.stderr)
            raise SystemExit(result)
    raise SystemExit(0)
if argv[0] == "__doctor_config__":
    import fieldkit.config as config
    import fieldkit.config._loader as config_loader

    root = Path(argv[1])
    scenario = argv[2]
    config_path = root / "config" / "config.yaml"
    workspace = root / "workspace"
    os.environ["HOME"] = str(root / "home")
    os.environ["FIELDKIT_DATA_DIR"] = str(root / "data")
    for name in (
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_OAUTH_CLIENT_ID",
        "GOOGLE_OAUTH_CLIENT_SECRET",
        "SHADOWBOT_ACCESS_TOKEN",
        "SLACK_BOT_TOKEN",
        "SLACK_USER_TOKEN",
    ):
        os.environ.pop(name, None)
    if scenario != "empty":
        config_path.parent.mkdir(parents=True)
        config_text = f"fieldkit_home: {workspace}\n"
        if scenario == "enabled-no-auth":
            config_text += "sf_org_url: https://example.my.salesforce.com\n"
        elif scenario == "google-token-missing":
            config_text += f"gmail_token: {root / 'missing-google-token.json'}\n"
        elif scenario == "shadowbot-incomplete":
            config_text += "shadowbot:\n  assistant_id: example-assistant\n"
        config_path.write_text(config_text, encoding="utf-8")

    config.CONFIG_PATH = config_path
    config_loader.CONFIG_PATH = config_path
    config.clear_config_caches()

    def reject_network(event, args):
        if event in {"socket.connect", "socket.getaddrinfo"}:
            raise RuntimeError(f"isolated doctor attempted network access: {event}")

    sys.addaudithook(reject_network)
    raise SystemExit(main(["doctor"]))
if argv[0] == "__offline_first_run__":
    import fieldkit.config as config
    import fieldkit.config._loader as config_loader

    root = Path(argv[1])
    config_path = root / "config" / "config.yaml"
    workspace = root / "workspace"
    config.CONFIG_PATH = config_path
    config_loader.CONFIG_PATH = config_path
    config.clear_config_caches()
    for name in (
        "GOOGLE_OAUTH_CLIENT_ID",
        "GOOGLE_OAUTH_CLIENT_SECRET",
        "SHADOWBOT_ACCESS_TOKEN",
        "SLACK_BOT_TOKEN",
        "SLACK_USER_TOKEN",
    ):
        os.environ.pop(name, None)

    def reject_network(event, args):
        if event in {"socket.connect", "socket.getaddrinfo"}:
            raise RuntimeError(f"offline first run attempted network access: {event}")

    sys.addaudithook(reject_network)
    commands = (
        ["init", "--minimal", str(workspace)],
        ["doctor"],
        ["skill", "list", "--json"],
    )
    for command in commands:
        result = main(command)
        if result != 0:
            print(f"{' '.join(command)}: exit {result}", file=sys.stderr)
            raise SystemExit(result)
    raise SystemExit(0)
raise SystemExit(main(argv))
"""


def _run_profile(profile: str, *args: str, disable_llm: bool = True) -> subprocess.CompletedProcess[str]:
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(_REPO_ROOT / "src"),
    }
    if disable_llm:
        environment["NO_LLM"] = "1"
    with tempfile.TemporaryDirectory(prefix="fieldkit-profile-") as run_dir:
        environment["HOME"] = str(Path(run_dir) / "home")
        environment["FIELDKIT_DATA_DIR"] = str(Path(run_dir) / "data")
        return subprocess.run(
            [sys.executable, "-c", _PROFILE_RUNNER, profile, *args],
            cwd=run_dir,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )


@pytest.mark.parametrize(
    ("profile", "command"),
    [
        ("base", ("--help",)),
        ("base", ("auth", "--help")),
        ("base", ("doctor", "--help")),
        ("base", ("gmail", "--help")),
        ("base", ("ingest", "--help")),
        ("base", ("ingest", "status", "--help")),
        ("base", ("ingest", "status", "--json")),
        ("base", ("commands", "--json")),
        ("base", ("skill", "eval", "--behavioral", "--skill", "brief", "--json")),
        ("google", ("meeting", "--help")),
        ("web", ("web", "--help")),
        ("chrome-auth", ("__probe__",)),
    ],
)
def test_profile_success_surface(profile: str, command: tuple[str, ...]) -> None:
    result = _run_profile(profile, *command)

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("command", "profile"),
    [
        (("auth", "google", "--help"), "google"),
        (("gmail", "sync", "--help"), "google"),
        (("__ingest_transcript__",), "google"),
        (("meeting", "--help"), "google"),
        (("web", "--help"), "web"),
        (("__chrome__",), "chrome-auth"),
    ],
)
def test_base_profile_reports_actionable_missing_profile(command: tuple[str, ...], profile: str) -> None:
    result = _run_profile("base", *command)

    assert result.returncode == 3
    assert f"requires the '{profile}' optional profile" in result.stderr
    assert f"pip install 'fieldkit-cli[{profile}]'" in result.stderr
    assert f"uv tool install 'fieldkit-cli[{profile}]'" in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("mode", ["--behavioral", "--calibrate", "--routing"])
def test_base_profile_reports_actionable_missing_llm_for_live_eval_modes(mode: str) -> None:
    args = ("--skill", "brief") if mode == "--behavioral" else ()
    result = _run_profile("base", "skill", "eval", mode, *args, "--json", disable_llm=False)

    assert result.returncode == 3
    assert "requires the 'llm' optional profile" in result.stderr
    assert "pip install 'fieldkit-cli[llm]'" in result.stderr
    assert "uv tool install 'fieldkit-cli[llm]'" in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("command", ["brief", "pipeline"])
def test_base_profile_reports_actionable_missing_llm_for_llm_consumers(tmp_path: Path, command: str) -> None:
    result = _run_profile("base", "__llm_command__", str(tmp_path), command, disable_llm=False)

    assert result.returncode == 3
    assert "requires the 'llm' optional profile" in result.stderr
    assert "pip install 'fieldkit-cli[llm]'" in result.stderr
    assert "uv tool install 'fieldkit-cli[llm]'" in result.stderr
    assert "Traceback" not in result.stderr


def test_base_profile_registry_keeps_optional_commands_discoverable() -> None:
    """A base install advertises optional commands without importing dependencies."""
    result = _run_profile("base", "commands", "--json")

    payload = json.loads(result.stdout)
    names = {entry["full_name"] for entry in payload}
    assert result.returncode == 0
    assert {"auth google", "meeting link", "web serve"} <= names


def test_base_profile_registry_preserves_all_profile_command_metadata() -> None:
    """Dependency availability cannot change the machine-readable CLI contract."""
    base_result = _run_profile("base", "commands", "--json")
    all_result = _run_profile("all", "commands", "--json")

    assert base_result.returncode == 0, base_result.stderr
    assert all_result.returncode == 0, all_result.stderr
    assert json.loads(base_result.stdout) == json.loads(all_result.stdout)


def test_base_profile_version_features_preserve_auth_and_gmail_subcommands() -> None:
    """Feature introspection retains base siblings when optional leaves are absent."""
    result = _run_profile("base", "version", "--features", "--json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    groups = {entry["group"]: entry for entry in payload["cli"]["groups"]}
    assert {entry["name"] for entry in groups["auth"]["subcommands"]} == {
        "backstory",
        "google",
        "sf",
        "shadowbot",
    }
    assert {entry["name"] for entry in groups["gmail"]["subcommands"]} == {
        "account-tags",
        "backstory-gap",
        "decay",
        "enrich-pursuits",
        "query",
        "sync",
    }


def test_all_profile_discovers_every_top_level_command() -> None:
    """The complete profile can load help for every top-level command group."""
    result = _run_profile("all", "__all__")

    assert result.returncode == 0, result.stderr


def test_base_profile_supports_offline_first_success(tmp_path: Path) -> None:
    """A fresh base install reaches useful offline output without network access."""
    result = _run_profile("base", "__offline_first_run__", str(tmp_path))

    assert result.returncode == 0, result.stderr
    assert "Initialized offline workspace" in result.stdout
    assert "optional" in result.stdout
    assert '"name"' in result.stdout


@pytest.mark.parametrize("scenario", ["empty", "partial"])
def test_base_doctor_accepts_unconfigured_integrations(tmp_path: Path, scenario: str) -> None:
    result = _run_profile("base", "__doctor_config__", str(tmp_path), scenario)

    assert result.returncode == 0, result.stderr
    assert "optional" in result.stdout


def test_base_doctor_preserves_auth_failure_for_enabled_integration(tmp_path: Path) -> None:
    result = _run_profile("base", "__doctor_config__", str(tmp_path), "enabled-no-auth")

    assert result.returncode == 2, result.stderr
    assert "sf: AUTH REQUIRED" in result.stdout


def test_base_doctor_reports_missing_explicit_google_token_as_auth_required(tmp_path: Path) -> None:
    result = _run_profile("base", "__doctor_config__", str(tmp_path), "google-token-missing")

    assert result.returncode == 2, result.stderr
    assert "google: AUTH REQUIRED" in result.stdout


def test_base_doctor_rejects_incomplete_shadowbot_configuration(tmp_path: Path) -> None:
    result = _run_profile("base", "__doctor_config__", str(tmp_path), "shadowbot-incomplete")

    assert result.returncode == 3, result.stderr
    assert "shadowbot: CONFIG ERROR" in result.stdout
