"""Subscription adapter and opaque-token bootstrap security contracts."""

import importlib
import importlib.util
import json
import signal
import stat
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit
ACCESS = "fictional-access-value"
REFRESH = "fictional-refresh-value"
ACCOUNT = "fictional-account"


@pytest.fixture
def adapter(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ModuleType:
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    module = importlib.import_module("openshell_codex")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(module.os, "environ", {"PATH": "/example/bin"})
    auth = tmp_path / ".codex/auth.json"
    auth.parent.mkdir()
    auth.write_text(
        json.dumps({"tokens": {"access_token": ACCESS, "refresh_token": REFRESH, "account_id": ACCOUNT}}),
        encoding="utf-8",
    )
    manifest = module.workspace.Manifest(
        "fieldkit-cli", "worker:1", "policy.yaml", "git-head", (), {}, None, False, "/sandbox/workspace"
    )
    monkeypatch.setattr(module.workspace, "load_manifest", lambda *args: manifest)
    return module


@pytest.fixture
def approved_profile() -> dict[str, object]:
    return {
        "id": "fieldkit-codex",
        "scope": "workspace",
        "endpoints": [
            {"host": "chatgpt.com", "port": 443, "protocol": "rest", "access": "read-write", "enforcement": "enforce"}
        ],
        "binaries": ["/usr/local/bin/codex"],
        "credentials": [
            {
                "env_vars": ["CODEX_AUTH_ACCESS_TOKEN"],
                "auth_style": "bearer",
                "header_name": "authorization",
                "required": True,
            }
        ],
    }


@pytest.mark.parametrize(
    "field,value", [("scope", "global"), ("binaries", ["/bin/curl"]), ("endpoints", []), ("refresh", {"enabled": True})]
)
def test_rejects_unapproved_profile(
    adapter: ModuleType, approved_profile: dict[str, object], field: str, value: object
) -> None:
    approved_profile[field] = value
    with pytest.raises(ValueError, match="approved token-only"):
        adapter.verify_profile(approved_profile)


def test_profile_is_verified_before_provider_creation(
    adapter: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, b"{}", b"")

    monkeypatch.setattr(adapter.workspace, "run", fake_run)
    with pytest.raises(ValueError, match="approved token-only"):
        adapter.execute(tmp_path, [], "result", ["exec", "example"], 20)
    assert len(calls) == 1
    assert "create" not in calls[0]


@pytest.mark.parametrize("worker_outcome", [0, 7, RuntimeError("worker failed"), KeyboardInterrupt()])
def test_provider_cleanup_and_secret_transport(
    adapter: ModuleType,
    approved_profile: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    worker_outcome: int | BaseException,
) -> None:
    calls: list[tuple[list[str], dict[str, str] | None]] = []
    worker_calls: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        env = kwargs.get("env")
        calls.append((args, dict(env) if isinstance(env, dict) else None))
        return subprocess.CompletedProcess(
            args, 0, json.dumps(approved_profile).encode() if "export" in args else b"", b""
        )

    def worker(args: list[str]) -> int:
        worker_calls.append(args)
        if isinstance(worker_outcome, BaseException):
            raise worker_outcome
        return worker_outcome

    monkeypatch.setattr(adapter.workspace, "run", fake_run)
    monkeypatch.setattr(adapter.workspace, "main", worker)
    if isinstance(worker_outcome, BaseException):
        with pytest.raises(type(worker_outcome)) as captured:
            adapter.execute(tmp_path, ["selected.txt"], "result", ["exec", "example"], 20)
        assert captured.value is worker_outcome
    else:
        assert adapter.execute(tmp_path, ["selected.txt"], "result", ["exec", "example"], 20) == worker_outcome
    create_args, create_env = calls[1]
    assert create_env is not None
    assert create_env["CODEX_AUTH_ACCESS_TOKEN"] == ACCESS
    assert REFRESH not in create_env.values()
    assert "--credential" in create_args
    assert calls[-1][0][-2] == "delete"
    assert calls[-1][1] is None
    assert calls[-1][0][-1] == create_args[create_args.index("--name") + 1]
    all_args = [arg for args, _ in calls for arg in args] + worker_calls[0]
    assert all(ACCESS not in arg and REFRESH not in arg for arg in all_args)
    assert f"CODEX_ACCOUNT_ID={ACCOUNT}" in worker_calls[0]
    assert "/usr/local/bin/codex-with-provider" in worker_calls[0]


@pytest.mark.parametrize("deletion_fails", [False, True])
def test_sigterm_during_provider_delete_finishes_delete(
    adapter: ModuleType,
    approved_profile: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    deletion_fails: bool,
) -> None:
    deletion_finished: list[bool] = []

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        if "delete" in args:
            signal.raise_signal(signal.SIGTERM)
            deletion_finished.append(True)
            if deletion_fails:
                raise RuntimeError("deletion rejected")
        return subprocess.CompletedProcess(
            args, 0, json.dumps(approved_profile).encode() if "export" in args else b"", b""
        )

    monkeypatch.setattr(adapter.workspace, "run", fake_run)
    monkeypatch.setattr(adapter.workspace, "main", lambda args: 0)
    previous = signal.getsignal(signal.SIGTERM)
    expected = RuntimeError if deletion_fails else KeyboardInterrupt
    with pytest.raises(expected) as captured:
        adapter.execute(tmp_path, [], "result", ["exec", "example"], 20)
    if deletion_fails:
        assert "provider cleanup failed" in str(captured.value)
    assert deletion_finished == [True]
    assert signal.getsignal(signal.SIGTERM) == previous


def test_provider_cleanup_failure_reports_primary_outcome(
    adapter: ModuleType, approved_profile: dict[str, object], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        if "delete" in args:
            raise RuntimeError("delete failed")
        return subprocess.CompletedProcess(
            args, 0, json.dumps(approved_profile).encode() if "export" in args else b"", b""
        )

    monkeypatch.setattr(adapter.workspace, "run", fake_run)
    monkeypatch.setattr(adapter.workspace, "main", lambda args: 7)
    with pytest.raises(RuntimeError, match=r"cleanup failed.*worker exit 7"):
        adapter.execute(tmp_path, [], "result", ["exec", "example"], 20)


@pytest.mark.parametrize("scope,snapshot", [("fieldkit-home", "git-head"), ("fieldkit-cli", "selected-files")])
def test_refuses_non_cli_manifest(
    adapter: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, scope: str, snapshot: str
) -> None:
    manifest = adapter.workspace.Manifest(
        scope, "worker:1", "policy.yaml", snapshot, (), {}, None, False, "/sandbox/workspace"
    )
    monkeypatch.setattr(adapter.workspace, "load_manifest", lambda *args: manifest)

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("non-CLI manifest attempted a credential operation")

    monkeypatch.setattr(adapter.workspace, "run", forbidden)
    with pytest.raises(ValueError, match="CLI source workspace"):
        adapter.execute(tmp_path, [], "result", ["exec", "example"], 20)


@pytest.fixture
def bootstrap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ModuleType:
    path = Path(__file__).resolve().parents[1] / ".devcontainer/codex_with_provider.py"
    spec = importlib.util.spec_from_file_location("codex_with_provider_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("CODEX_ACCOUNT_ID", ACCOUNT)
    monkeypatch.setenv("CODEX_AUTH_ACCESS_TOKEN", "openshell:resolve:fictional-opaque-handle")
    monkeypatch.setattr(module.sys, "argv", ["codex-with-provider", "exec", "example"])
    return module


def test_bootstrap_writes_only_opaque_auth_with_protected_permissions(
    bootstrap: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(bootstrap.os, "execv", lambda executable, args: calls.append((executable, args)))
    assert bootstrap.main() is None
    path = tmp_path / ".codex/auth.json"
    auth = json.loads(path.read_text(encoding="utf-8"))
    assert auth["auth_mode"] == "chatgptAuthTokens"
    assert auth["OPENAI_API_KEY"] is None
    assert auth["last_refresh"].endswith("Z")
    assert auth["tokens"]["id_token"].endswith(".placeholder")
    assert auth["tokens"]["access_token"] == "openshell:resolve:fictional-opaque-handle"
    assert auth["tokens"]["refresh_token"] == ""
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert calls == [("/usr/local/bin/codex", ["codex", "exec", "example"])]
    assert ACCESS not in path.read_text(encoding="utf-8") and REFRESH not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("token", [ACCESS, "", "Bearer fictional"])
def test_bootstrap_rejects_raw_bearers(
    bootstrap: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, token: str
) -> None:
    monkeypatch.setenv("CODEX_AUTH_ACCESS_TOKEN", token)
    with pytest.raises(ValueError, match="opaque OpenShell bearer"):
        bootstrap.main()
    assert not (tmp_path / ".codex").exists()


@pytest.mark.parametrize("link_directory", [False, True])
def test_bootstrap_rejects_symlink_auth_paths(
    bootstrap: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, link_directory: bool
) -> None:
    target = tmp_path / "outside"
    target.mkdir()
    sentinel = target / "auth.json"
    sentinel.write_text("unchanged", encoding="utf-8")
    if link_directory:
        (tmp_path / ".codex").symlink_to(target, target_is_directory=True)
    else:
        (tmp_path / ".codex").mkdir()
        (tmp_path / ".codex/auth.json").symlink_to(sentinel)
    monkeypatch.setattr(bootstrap.os, "execv", lambda *args: pytest.fail("symlink path executed Codex"))
    with pytest.raises((OSError, ValueError)) as captured:
        bootstrap.main()
    assert captured.value
    assert sentinel.read_text(encoding="utf-8") == "unchanged"


def test_failed_provider_creation_still_attempts_delete(
    adapter: ModuleType, approved_profile: dict[str, object], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(args)
        if "create" in args:
            raise RuntimeError("creation failed")
        return subprocess.CompletedProcess(
            args, 0, json.dumps(approved_profile).encode() if "export" in args else b"", b""
        )

    monkeypatch.setattr(adapter.workspace, "run", fake_run)
    with pytest.raises(RuntimeError, match="creation failed"):
        adapter.execute(tmp_path, [], "result", ["exec", "example"], 20)
    assert calls[-1][-2] == "delete"
    assert calls[-1][-1] == calls[-2][calls[-2].index("--name") + 1]


def test_missing_login_metadata_prevents_provider_creation(
    adapter: ModuleType, approved_profile: dict[str, object], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".codex/auth.json").write_text(json.dumps({"tokens": {"access_token": ACCESS}}), encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, json.dumps(approved_profile).encode(), b"")

    monkeypatch.setattr(adapter.workspace, "run", fake_run)
    with pytest.raises(ValueError, match="routing metadata"):
        adapter.execute(tmp_path, [], "result", ["exec", "example"], 20)
    assert len(calls) == 1
    assert "create" not in calls[0]


def test_bootstrap_narrows_existing_auth_permissions(
    bootstrap: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    directory = tmp_path / ".codex"
    directory.mkdir(mode=0o700)
    directory.chmod(0o777)
    path = directory / "auth.json"
    path.write_text("old", encoding="utf-8")
    path.chmod(0o644)
    monkeypatch.setattr(bootstrap.os, "execv", lambda *args: None)
    assert bootstrap.main() is None
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert json.loads(path.read_text(encoding="utf-8"))["tokens"]["refresh_token"] == ""


def test_bootstrap_requires_account_routing_metadata(
    bootstrap: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("CODEX_ACCOUNT_ID")
    with pytest.raises(ValueError, match="routing metadata"):
        bootstrap.main()
    assert not (tmp_path / ".codex").exists()
